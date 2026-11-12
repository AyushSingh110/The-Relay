# Phase 03 Implementation Report — PDF Ingestion

> **Phase ID:** `p03`
> **Date:** `2026-11-12 09:16 IST`
> **Status:** `Completed`. Verified in Conda env `the-relay` (Python 3.12.16, PyMuPDF `2.1`): 209 passed, ruff clean. **0 Groq calls.** Network was used only for arXiv.
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P03 and the owner's prompt, ingest exactly `1706.03762v7`, `2410.00037v1` and `2507.07955v2` from their version-specific arXiv URLs:
- validate each PDF and record its arXiv ID, version, SHA-256, size and page count in the tracked manifest;
- extract text with PyMuPDF, keeping page numbers and a sensible reading order (multi-column aware);
- tag sections, references and metadata;
- chunk deterministically, with paper, version, page range and a stable ID on every chunk;
- test all of it with fixtures.

Out of scope: BM25 or dense indexes, generation, and any Groq usage.

## 2. Implementation Summary

`src/relay/ingestion/` has four stages, which the pipeline (`pipeline.py`) runs in order:

**1. Fetch and validate (`arxiv_fetcher.py`)**
- **Registry.** `PAPERS` pins the three exact versions. Each URL is `https://arxiv.org/pdf/<id>v<ver>`.
- **Validation.** A PDF is accepted only if all of these hold:
  - it has the `%PDF-` signature;
  - its size is between 1 KB and 60 MB;
  - it opens in PyMuPDF and is not encrypted;
  - it has at least one page;
  - **arXiv's own version stamp `arXiv:<id>v<ver>` is on page 1** (proving the exact version);
  - the expected title is on page 1.
- **Writes and checksums.** Invalid downloads are never written. Cached PDFs are re-validated on every run. arXiv versions are immutable, so a SHA-256 that differs from the manifest raises an error rather than overwriting.

**2. Parse (`pdf_parser.py`)**
- **Inputs.** Block-level text from PyMuPDF (`get_text("dict")`) with bounding boxes. Page numbers are 1-based.
- **Dropped, with counts per reason:**
  - rotated text (the arXiv sidebar stamp);
  - running headers (top-margin text repeated on 3 or more pages);
  - bottom-margin page numbers;
  - figure glyph text (letter-spaced labels, byte hex dumps, axis-tick runs);
  - "figure word soup" (pages made mostly of one-word blocks);
  - images.
- **Reading order.**
  - A page counts as two-column when paragraph-sized blocks (≥ 15 words) sit wholly on each side of the centre line.
  - Two-column pages are read band by band: full-width blocks split bands, and within a band the left column is read before the right.
  - Other pages are read in rows (vertical overlap), with horizontally overlapping blocks stacked. Lines inside a block are ordered the same way.
- **Text repair:**
  - super- and subscripts are marked (`10^20`, `d_model`, `d_k`);
  - end-of-line hyphenation is repaired using the paper's own vocabulary;
  - NFKC normalisation (ligatures, math letters) and whitespace collapse.
- **Sections.** Taken from the **PDF outline (TOC)**, matched to the heading block on the listed page or the next one, preferring numbered matches. The TOC lists titles without numbers; the number comes from the page text. Also:
  - text-detected `Abstract` / `Acknowledg…` / `References`;
  - a per-paper `extra_headings` list for headings missing from the outline (Attention's "Attention Visualizations").
- **Section kinds:** `front_matter`, `abstract`, `body`, `acknowledgements`, `references` (`is_reference=true`), and `appendix` (any outline section after References).
- **Page flags** mark extracted-but-less-reliable pages: `contains_table`, `math_heavy`, `low_text`, `figure_text_removed`, `two_column_reordered`.

**3. Chunk (`chunker.py`)**
- **Method.** Word windows within a section. Chunks never cross sections, so section and `is_reference` metadata is exact. Heading text is metadata, not chunk text.
- **ID.** `chunk_id = {paper_id}_p{page_start}_c{idx}`.
- **Metadata on each chunk:** `paper_id`, `arxiv_id`, `version`, `short_name`, section id/number/title/path/kind, `is_reference`, `page_start`, `page_end`, word offsets and `word_count`.
- **Presets** (sizes from ARCH §6 [PROPOSED]; overlap ~15 % [PROPOSED]): `small_fast` 180 / 30, `balanced_rerank` 330 / 50, `large_wide` 600 / 90 words.

**4. Write outputs (`pipeline.py` + `scripts/ingest_papers.py`)**
- `data/processed/<paper_id>/{blocks.jsonl, sections.json, pages.json}`.
- `data/processed/chunks/<preset>.jsonl`.
- The tracked `data/papers/manifest.json`: provenance plus a deterministic extraction summary with SHA-256 digests of the blocks and of each chunk set.
- **CLI modes:** `--offline`, `--check` (offline; exit 1 on manifest drift) and `--refresh`.

**P02-LIVE findings preserved, settings unchanged.** `max_completion_tokens=256` was accepted but never reached. The 3.0 characters/token fallback may underestimate very short prompts because of fixed message overhead. Both are recorded in `ENGINEERING_LOG.md`; P03 touched no provider setting.

## 3. Files Created

- `src/relay/ingestion/__init__.py`: package marker.
- `src/relay/ingestion/errors.py`: `IngestionError`, `PaperFetchError`, `PaperValidationError`, `ManifestError`.
- `src/relay/ingestion/arxiv_fetcher.py`:
  - `PAPERS` registry;
  - validation;
  - download;
  - `PaperRecord` / `CorpusManifest`;
  - manifest I/O.
- `src/relay/ingestion/pdf_parser.py`: extraction, cleanup, reading order, hyphenation, sections, page flags.
- `src/relay/ingestion/chunker.py`: presets, `Chunk`, `chunk_paper`, JSONL rendering and digests.
- `src/relay/ingestion/pipeline.py`: `run_ingest` and the extraction summary.
- `scripts/ingest_papers.py`: CLI.
- `tests/test_ingestion.py`: 46 tests.
- `data/papers/manifest.json` (**tracked**, 31,639 bytes).
- **Gitignored, regenerable:**
  - `data/papers/{1706.03762v7, 2410.00037v1, 2507.07955v2}.pdf`;
  - the 12 files under `data/processed/`.
- `reports/phase-03-pdf-ingestion.md`: this report.

## 4. Files Modified

- `.gitignore`: added `data/processed/`, next to the existing `data/papers/*.pdf` and `data/indexes/`. These hold extracted text and chunks: regenerable working data, and full paper text that shouldn't be redistributed. The manifest stays tracked.
- `README.md`: an "Paper ingestion" section with the commands.
- `reports/phase-02-live-discovery.md`, `reports/phase-02-live-baseline.md`: Owner Approval → `Owner Approved (2026-11-12)`.
- `ENGINEERING_LOG.md`: a P02-LIVE approval entry and the P03 entry.
- **No P01/P02 module changed.** `pyproject.toml` is unchanged (pymupdf and httpx were already dependencies) and nothing was installed.

## 5. Technical Decisions

1. **`paper_id` = versioned arXiv ID** (`1706.03762v7`). It is unambiguous and keeps "other version" traps detectable. `short_name` (`attention` / `moshi` / `hnet`) is metadata.
2. **Sections come from the PDF outline, not fonts.** I inspected the real PDFs first: bold flags are inconsistent (missing on numbered headings in Attention and H-Net), while all three papers carry an outline. Outline entries are matched to page text. A numbered match beats an unnumbered one, because Figure 2 in Attention repeats the subsection titles as labels.
3. **Paper-specific `extra_headings`.** The only one is Attention's "Attention Visualizations" (pages 13–15, after References, not in the outline). Without it those pages would be mis-tagged as references.
4. **Hyphenation is vocabulary-based and fully logged.**
   - Merge when the joined word occurs elsewhere in the paper.
   - Keep the hyphen when the hyphenated form occurs, when the next line starts with a non-lowercase letter (`Berkeley-Parser`, `Mamba-2`), or when both halves are standalone words. The second half must be at least 4 letters, or a connector word on either side (`sequence-aligned`, `English-to`, `state-of-the-art`).
   - Otherwise merge, and list the result as "unverified" (§8.4).
5. **Super/subscript markers** use span size (≤ 80 % of the line's dominant size) and baseline shift. Without them, table exponents read as `1020` instead of `10^20`, a numeric trap for grounding.
6. **Figure filters.** These blocks are dropped and counted because they are figure noise, not prose. Blocks containing `=` are always kept, after the first version of the filter wrongly matched one equation and one table header (§9).
7. **Chunks never cross sections, and IDs count per start page.** Reference chunks are therefore pure, so P04 can down-weight them exactly.
8. **NFKC normalisation** maps ligatures and math-alphanumeric letters (`𝑑` → `d`), so text is searchable by plain queries. Answers will quote this normalised text, so verification stays consistent.
9. **Reproducibility.** The manifest has no run timestamps; only each paper's first `fetched_at` is kept. All outputs are sorted, and digests are recorded.

## 6. Commands Actually Executed

Git Bash with `PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe`. `.env` was not read.

```bash
$PY -c "import pymupdf; print(pymupdf.__version__)"                 # 2.1
# One networked fetch (arXiv only), via relay.ingestion.arxiv_fetcher.ensure_paper:
#   1706.03762v7 downloaded 2215244 B, 15 pages, stamp arXiv:1706.03762v7
#   2410.00037v1 downloaded 4694312 B, 66 pages, stamp arXiv:2410.00037v1
#   2507.07955v2 downloaded 1375282 B, 39 pages, stamp arXiv:2507.07955v2
git check-ignore -v data/papers/1706.03762v7.pdf data/papers/manifest.json
#   .gitignore:21:data/papers/*.pdf  (PDF ignored; manifest NOT ignored)
# Layout inspection scripts (TOC, block bboxes, fonts) -> scratch/inspect*.txt
$PY scripts/ingest_papers.py --offline   # x2 per iteration; final pair = run5/run6, exit 0
cmp data/papers/manifest.json scratch/manifest_run5.json             # IDENTICAL
cmp scratch/processed_run5.sha scratch/processed_run6.sha            # IDENTICAL (12 files)
$PY scripts/ingest_papers.py --check     # "manifest_unchanged": true, exit 0
git check-ignore -v data/processed/chunks/small_fast.jsonl           # .gitignore:22:data/processed/
$PY -m pytest -q -p no:cacheprovider tests/test_ingestion.py         # 46 passed in 1.03s
$PY -m pytest -q -p no:cacheprovider                                  # 209 passed in 3.93s
$PY -m ruff check .                                                   # All checks passed!
```

**Verification commands for the owner (PowerShell):**

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
python -m pytest -v tests/test_ingestion.py
python -m pytest -q
python -m ruff check .
python scripts/ingest_papers.py --check; $LASTEXITCODE        # expect manifest_unchanged true, 0
python scripts/ingest_papers.py --offline; $LASTEXITCODE      # regenerates data/processed, 0
git check-ignore -v data/papers/1706.03762v7.pdf data/processed/chunks/small_fast.jsonl
git status --short
Get-FileHash data/papers/*.pdf -Algorithm SHA256
```

## 7. Test Results

These are from the final runs, after the last code edit:
- `python -m pytest -q`: **209 passed, 0 failed, 0 skipped** (3.93 s; `filterwarnings=error`, so no warnings).
- `tests/test_ingestion.py`: **46 passed**. That includes the real-corpus test, which ran because the PDFs are present; it skips on a fresh clone.
- Collected per file: ingestion 46, http_provider 75, probe_groq 15, schemas 29, foundation 21, run_store/aggregation 23.
- `python -m ruff check .`: **All checks passed!**

What the ingestion tests cover:
- **Validation:** the registry's exact versions and URLs; a valid PDF; 6 rejection cases (not a PDF, too small, corrupt, no stamp, **wrong version**, no title); encrypted PDF.
- **Fetching** (MockTransport):
  - a download is written and recorded, and the cache is reused with no second request;
  - a checksum change against the manifest is an error;
  - HTTP 404 and an HTML content type are rejected;
  - invalid downloads are not written;
  - the manifest round trip is deterministic.
- **Reading order:**
  - **a synthetic 2-column page reads title → left column → right column → full-width footer**;
  - single-column rows read left to right;
  - the stamp, running headers and page numbers are dropped, while a header on too few pages is kept;
  - the glyph filter keeps equations;
  - the word-soup filter removes soup and leaves pages with few short blocks alone.
- **Text repair:** super/subscript marking; leading footnote markers; 8 hyphenation cases; the vocabulary rules; ligature and math-letter normalisation.
- **Sections:**
  - **outline sections with numbers, Abstract, Acknowledgements, References and an appendix**;
  - **a figure label repeating a title is not taken as a heading**;
  - **reference tagging**;
  - an appendix without the extra heading;
  - a missing outline heading is reported;
  - parsing determinism.
- **Chunk boundaries:** overlap, the section end, page spans, no headings in chunk text, reference propagation; stable, unique IDs; short and empty sections; preset validation.
- **Pipeline:** reproducible across runs, offline re-run with zero requests, offline without a PDF fails.
- **Real corpus:** the files match the manifest checksums; Attention's `3.2.2 Multi-Head Attention` is detected, References start on page 10, and the page-8 table has `28.4` and `10^20`.

## 8. Actual Output and Generated Artifacts

### 8.1 Corpus manifest (validated)

| paper_id | arXiv / version | SHA-256 | Size (bytes) | Pages | Stamp on p1 | PDF producer |
| :--- | :--- | :--- | -: | -: | :--- | :--- |
| `1706.03762v7` | 1706.03762 / 7 | `bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697` | 2,215,244 | 15 | `arXiv:1706.03762v7` | pdfTeX-1.40.25 |
| `2410.00037v1` | 2410.00037 / 1 | `a687144cc77bea86b0c60ae800ccb1d3e0816033fa5c7960388fe29a0dbfcfac` | 4,694,312 | 66 | `arXiv:2410.00037v1` | pdfTeX-1.40.25 |
| `2507.07955v2` | 2507.07955 / 2 | `04a2b32e6e82853cfc23c5db132750874a86cde99219ec6f664128f4aa83122f` | 1,375,282 | 39 | `arXiv:2507.07955v2` | pikepdf 8.15.1 (arXiv GenPDF) |

`fetched_at`: 2026-11-12T03:32:59Z, 03:33:03Z and 03:33:04Z.

### 8.2 Extraction results

| Paper | Words | Blocks | Sections | Outline issues | References pages | Pages without text | Layout |
| :--- | -: | -: | -: | :--- | :--- | :--- | :--- |
| Attention | 5,716 | 244 | 27 | none | 10–12 | none | all single-column |
| Moshi | 28,860 | 798 | 48 | none | 42–52 | none | all single-column |
| H-Net | 20,341 | 643 | 38 | none | 25–30 | none | single-column; page 15 has two side-by-side figure panels (glyph text removed) |

- **Every outline entry was matched to a numbered heading in all three papers.** That gives:
  - **Attention:** sections 1–7, including `3.2.1`–`3.2.3` and `3.3`–`3.5`; Abstract; Acknowledgements; References; and the appendix "Attention Visualizations".
  - **Moshi:** 1–7, 3.x.x, 4.x, 5.1–5.8, 6.1–6.4; Acknowledgments; References; Appendix A–F with F.1–F.3.
  - **H-Net:** 1–5 including 2.1.1–2.2.3; Acknowledgements; References; Appendix A (A.1.1–A.3) to F.
- **Dropped blocks** (by design; counts):
  - Attention: rotated 283 (sidebar stamp plus rotated visualisation words), page numbers 14, images 3.
  - Moshi: running headers 66, page numbers 66, rotated 31, figure word soup 98, figure glyph text 3, images 9.
  - H-Net: rotated 105, page numbers 39, figure glyph 58, word soup 27, images 48.
- **Determinism digests (block lists):**
  - Attention `beaeae59ec75fac02b0e13f7e23857c75f3be6c8a4ef315b6183cbca4e351e89`
  - Moshi `c2d15444dc71c7f3585490ec3adb399a2cb6825dfc7dec2e12296c02476b4eea`
  - H-Net `707767b37e39d4dafbec4fc0606af144333a412c864ddffb4591d03e3863b8ca`

### 8.3 Chunk counts (presets [PROPOSED])

| Preset (words / overlap) | Attention | Moshi | H-Net | **Total** | of which reference chunks | Chunk-set SHA-256 |
| :--- | -: | -: | -: | -: | :--- | :--- |
| `small_fast` 180/30 | 49 | 207 | 146 | **402** | 7 / 28 / 23 | `0a7f20df070cfe259eb1babb029d3c261799be69c9ddf0ec9a20094ab6aaaaff` |
| `balanced_rerank` 330/50 | 32 | 119 | 84 | **235** | 4 / 15 / 12 | `7187584e4fabaa9be13595144b0b7db8d728c3af82c1d8bdb77a086f07a0cf18` |
| `large_wide` 600/90 | 27 | 77 | 60 | **164** | 2 / 8 / 7 | `c6d36338ebaa8243dc19f12a231a1b1fe02a1fbfdb62c2c597688199394d8d1e` |

Chunk sizes and page spans (final outputs):

| Preset | Words min / median / max | Chunks < 40 words | Pages per chunk: 1 / 2 / 3 |
| :--- | :-: | :-: | :-: |
| `small_fast` | 9 / 180 / 180 | 10 | 283 / 117 / 2 |
| `balanced_rerank` | 9 / 330 / 330 | 6 | 121 / 111 / 3 |
| `large_wide` | 9 / 337 / 600 | 6 | 66 / 82 / 16 |

Short chunks come from short sections, e.g. a top-level heading with a one-sentence intro before its first subsection. For `large_wide`, many sections are shorter than 600 words, so its median sits well below the preset size.

### 8.4 Pages that could not be reliably extracted (flagged; text is kept unless noted)

| Paper | Flag | Pages | What is unreliable |
| :--- | :--- | :--- | :--- |
| Attention | `low_text` (figures) | 13, 14, 15 | Attention-visualisation figures; only the heading and figure captions are extracted. The per-word figure text was rotated or dropped. |
| Attention | `contains_table` | 6, 8, 9, 10 | Table cells are read row-wise. The Table 2 header merges with its first data rows (e.g. "EN-DE EN-FR EN-DE EN-FR ByteNet [18] 23.75 …"). |
| Attention | `math_heavy` | 6 | Equations are flattened (also on page 4, which is not flagged): `softmax(QK^T / √d_k / )V (1)`; fraction numerators drift (`1/√d_k` → "√ d_k", with the "1" appearing elsewhere on the line); sums and indices become `P^d^k i =1 q_i k_i`, `h_t_−_1`. |
| Moshi | `contains_table` | 8, 22, 23, 25, 27, 29, 30, 33, 35, 37, 38, 39, 40, 53, 61, 66 | Same row-wise table caveat. |
| Moshi | `figure_text_removed` | 34, 59 | Plot axis ticks and labels removed; captions kept. |
| Moshi | `math_heavy` | 13, 54, 58 | Flattened equations. |
| H-Net | `contains_table` | 11, 13, 14, 17, 21, 37, 38 | Same table caveat. |
| H-Net | `figure_text_removed` | 3, 7, 15, 18, 38, 39 | Byte-boundary visualisations (hex dumps, letter-spaced text) removed; captions kept. |
| H-Net | `math_heavy` | 5, 6, 7, 35 | Flattened equations (split fragments read left to right). |
| H-Net | `low_text` | 24 | Acknowledgements-only page (47 words): genuine, not an extraction failure. |

**Hyphenation decisions to review** (all logged in the manifest):
- **Attention:**
  - merged-unverified: `multiplicative`, `surprisingly` (correct);
  - kept: `english-to`, `sequence-aligned`, `source-target` (correct).
- **Moshi:**
  - 22 merged-unverified, mostly correct (`conflict`, `fundamentally`, `particularly`, `transformation`, names such as `schrittwieser`). **Questionable:** `crossmatch`, `melspectrogram`.
  - kept: `best-selling`, `lower-capacity`, `semantic-to`, `speech-based`, `the-art`, `to-speech`.
- **H-Net:**
  - merged-unverified: `accelerating`, `outrageously` (correct); **questionable:** `bitsper`, `predecay`.
  - kept: `cross-modality`, `high-resource`, `lesson-tokenization`, `tokenization-blog`.

### 8.5 Manual inspection excerpt: Attention pages 1–4 and table page 8 (from `blocks.jsonl`; `##` = heading; long blocks truncated)

```text
===== page 1 =====
Provided proper attribution is provided, Google hereby grants permission to reproduce the tables and figures in this paper solely for use in journalistic or
scholarly works.
Attention Is All You Need
Ashish Vaswani^∗
Google Brain avaswani@google.com
Noam Shazeer^∗
Google Brain noam@google.com
Niki Parmar^∗ Google Research nikip@google.com
... (remaining authors, each followed by affiliation)
## Abstract
The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder. ...
∗ Equal contribution. Listing order is random. Jakob proposed replacing RNNs with self-attention ...
31st Conference on Neural Information Processing Systems (NIPS 2017), Long Beach, CA, USA.

===== page 2 =====
## 1 Introduction
Recurrent neural networks, long short-term memory [13] and gated recurrent [7] neural networks ...
... they generate a sequence of hidden states h_t, as a function of the previous hidden state h_t_−_1 and the input for position t. ...
## 2 Background
... End-to-end memory networks are based on a recurrent attention mechanism instead of sequence-aligned recurrence ...
## 3 Model Architecture
... the encoder maps an input sequence of symbol representations (x_1, ..., x_n) to a sequence of continuous representations z = (z_1, ..., z_n). ...

===== page 3 =====
Figure 1: The Transformer - model architecture.
The Transformer follows this overall architecture using stacked self-attention and point-wise, fully connected layers ...
## 3.1 Encoder and Decoder Stacks
Encoder: The encoder is composed of a stack of N = 6 identical layers. ...
Decoder: The decoder is also composed of a stack of N = 6 identical layers. ...
## 3.2 Attention
An attention function can be described as mapping a query and a set of key-value pairs to an output, ...

===== page 4 =====
Scaled Dot-Product Attention          <- figure label (correctly NOT a heading)
Multi-Head Attention                  <- figure label (correctly NOT a heading)
Figure 2: (left) Scaled Dot-Product Attention. (right) Multi-Head Attention consists of several attention layers running in parallel.
of the values, where the weight assigned to each value is computed by a compatibility function of the query with the corresponding key.
## 3.2.1 Scaled Dot-Product Attention
We call our particular attention "Scaled Dot-Product Attention" (Figure 2). The input consists of queries and keys of dimension d_k, and values of dimension d_v. ...
Attention(Q, K, V ) = softmax(QK^T
√d_k
)V (1)
... except for the scaling factor of √ d_k . Additive attention computes the compatibility function using a feed-forward network with 1 a single hidden layer. ...
## 3.2.2 Multi-Head Attention
Instead of performing a single attention function with d_model-dimensional keys, values and queries, ...
4 To illustrate why the dot products get large, ... q · k = P^d^k
i =1 q_i k_i, has mean 0 and variance d_k.

===== page 8 (table page) =====
Table 2: The Transformer achieves better BLEU scores than previous state-of-the-art models on the English-to-German and English-to-French newstest2014 tests at a fraction of the training cost.
BLEU Training Cost (FLOPs)
Model
EN-DE EN-FR EN-DE EN-FR ByteNet [18] 23.75 Deep-Att + PosUnk [39] 39.2 1.0 · 10^20
GNMT + RL [38] 24.6 39.92 2.3 · 10^19 1.4 · 10^20
ConvS2S [9] 25.16 40.46 9.6 · 10^18 1.5 · 10^20
MoE [32] 26.03 40.56 2.0 · 10^19 1.2 · 10^20
Deep-Att + PosUnk Ensemble [39] 40.4 8.0 · 10^20
GNMT + RL Ensemble [38] 26.30 41.16 1.8 · 10^20 1.1 · 10^21
ConvS2S Ensemble [9] 26.36 41.29 7.7 · 10^19 1.2 · 10^21
Transformer (base model) 27.3 38.1 3.3 · 10^18
Transformer (big) 28.4 41.8 2.3 · 10^19
Residual Dropout We apply dropout [33] ... For the base model, we use a rate of P_drop = 0.1.
Label Smoothing During training, we employed label smoothing of value ε_ls = 0.1 [36]. ...
## 6 Results
## 6.1 Machine Translation
On the WMT 2014 English-to-German translation task, the big transformer model ... new state-of-the-art BLEU score of 28.4. ...
## 6.2 Model Variations
5 We used values of 2.8, 3.7, 6.0 and 9.5 TFLOPS for K80, K40, M40 and P100, respectively.
```

**Inspection verdict:**
- **Good:** prose order, headings and section numbering, author/affiliation pairing, and the `d_model` / `d_k` / `10^20` notation are faithful.
- **Table rows are mostly intact,** but the column headers merge into the first rows.
- **Equations are flattened** and fraction structure is lost.

## 9. Errors and Root-Cause Analysis

All found by inspecting the real PDFs or by tests, and fixed before the final runs.

1. **Figure labels taken as headings.** In Attention, the labels inside Figure 2 ("Scaled Dot-Product Attention") matched outline titles before the real "3.2.1" heading, so 3.2.1/3.2.2 lost their numbers. Root cause: the first match was accepted.
2. **Figure-heavy pages misread as two-column** (Moshi pages 34 and 59: three plot panels). Root cause: the column test counted short figure labels.
3. **Glyph filter false positives.** One equation (Moshi page 14, `Vs,q = As−τ,q …`) and one table header (Moshi page 40, `k = 1 k = 2 …`) were removed. Root cause: the short-token rule alone can't tell them from axis ticks.
4. **Hyphenation errors:**
   - `sequencealigned`, `englishto`, `crossmodality`, `bitsper` were merged; root cause: unknown joins defaulted to merging.
   - After the first fix, `multi-plicative` and `inter-mediate` were wrongly kept; root cause: compound parts such as "multi" were counted as standalone words.
   - Then `high-est` was wrongly kept and `the-art` wrongly merged; root cause: length thresholds.
5. **Glued superscripts** (`D^sdimensions`) and **interleaved authors and affiliations** (Attention page 1).
6. **Test-fixture issues:**
   - heading boxes were a fraction of a point too short for their font size (PyMuPDF refused the text);
   - two synthetic PDFs were below the 1 KB minimum;
   - a short "full-width" line didn't cross the centre line;
   - MuPDF merges synthetic same-baseline words into one block, so neither the word-soup fixture nor the row fixture produced the expected blocks;
   - MuPDF stored a block's lines out of left-to-right order. That is real behaviour, not only a fixture problem.
7. **Ruff:** one import-sort finding, one E501, two SIM117.

## 10. Fixes Applied

1. Separate heading-location pass: outline entries are matched in order on their page or the next, and a numbered match wins. Result: every outline entry carries its number.
2. The two-column decision now uses only paragraph-sized blocks (≥ 15 words). Moshi pages 34 and 59 are now single-column.
3. Blocks containing `=` are never treated as glyph text. All removals were re-listed and checked by eye.
4. Hyphenation:
   - the vocabulary uses standalone words only;
   - a "both halves are words" rule, with a second-part length ≥ 4 or a connector on either side.

   Re-listed and reviewed; residual questionable merges are listed in §8.4.
5. A space is inserted after a script span followed by normal-size text. Rows group horizontally overlapping blocks into stacks read top-down. Lines inside a block are ordered by row, then x.
6. Fixtures: boxes grow to fit, body pages were added, the footer line was lengthened, and the word-soup check became a unit test on the filter. The row test now asserts word order rather than block count.
7. Ruff fixes. All tests and ruff were then re-run (§7), and the real corpus was regenerated twice with identical output.

## 11. Outstanding Issues

1. **Equations and tables.** Equations are flattened and table headers merge with rows (§8.4). These are known retrieval and grounding traps. They fit the ARCH §11 "numbers split or garbled by extraction" trap category and should be considered when the owner writes questions.
2. **Questionable hyphen merges:** `crossmatch`, `melspectrogram` (Moshi), `bitsper`, `predecay` (H-Net). They are low-impact and listed for review; no manual overrides were added.
3. **Footnotes** stay inline at the bottom of each page's text, so a footnote can sit between a paragraph and its continuation on the next page.
4. **Presets.** Chunk sizes and overlaps remain [PROPOSED]; P04 finalises them.
5. **Two-column ordering** is exercised only on synthetic PDFs. None of the three papers has two-column body text.
6. **`extra_headings` is a hand-maintained, paper-specific setting** (one entry).

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P03 + owner prompt) | Status | Evidence |
| :--- | :--- | :--- |
| Exact versions fetched from version-specific URLs | `PASS` | registry test; download log; page-1 stamp check per paper |
| PDF validated; arXiv ID, version, SHA-256, size, page count in tracked manifest | `PASS` | §8.1; `data/papers/manifest.json` tracked (`git check-ignore`: not ignored) |
| Manifest hashes stable across runs | `PASS` | two runs byte-identical; `--check` → unchanged; real-corpus test |
| PDFs and artefacts stored per Git-ignore rules | `PASS` | PDFs ignored (`.gitignore:21`); `data/processed/` ignored (`.gitignore:22`) |
| PyMuPDF extraction keeps page numbers and reading order; 2-column test reads left then right | `PASS` | `test_two_column_page_reads_left_column_then_right`; page-tagged blocks |
| Sections, references and metadata tagged | `PASS` | §8.2; section and reference tests |
| Hyphenation repair | `PASS` | 8-case test; per-paper logs |
| Deterministic chunking with paper, version, page range and stable ID | `PASS` | chunk tests; digests identical across runs |
| Tests for validation, extraction, metadata, reading order, chunk boundaries, reproducibility | `PASS` | 46 tests (§7) |
| Manual inspection excerpt of Attention pages 1–4 and a table page | `PASS` | §8.5 |
| No indexes, generation or Groq calls | `PASS` | no retrieval/index code; 0 Groq requests |
| Offline `pytest`, `ruff check .`; one networked fetch (arXiv only) | `PASS` | 209 passed; ruff clean; one fetch session to arxiv.org |

## 13. Remaining Risks

- **Numeric questions on tables and equations** may retrieve garbled context. P04B verification should check gold citations on flagged pages.
- **Reference chunks** (7–28 per paper per preset) contain heavy keyword overlap. P04's down-weighting relies on the exact `is_reference` tag delivered here.
- **PyMuPDF version.** A different PyMuPDF build could change extraction subtly. The manifest records `pymupdf_version` and the digests, so `--check` would detect any drift.

## 14. Recommended Next Phase

**P04: Retrieval indexes, scope analyzer, eval schema and grading tooling.** The owner writes the evaluation questions in parallel. Preset values are to be confirmed from this text. P04 needs one model download for fastembed.

Stopping here; P04 does not start without explicit owner approval.
