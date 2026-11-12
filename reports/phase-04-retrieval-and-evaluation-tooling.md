# Phase 04 Implementation Report — Retrieval Indexes, Scope Analyzer, Evaluation Schema and Grading Tooling

> **Phase ID:** `p04`
> **Date:** `2026-11-12 10:13 IST`
> **Status:** `Completed`. Verified in Conda env `the-relay` (Python 3.12.16): 274 passed, ruff clean. **0 Groq calls.** The only network use was one Hugging Face download of the two planned models.
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P04 and the owner's prompt:
- build local BM25 and dense indexes over the P03 chunks;
- use FastEmbed with the planned ONNX model, downloaded once with its version recorded, and NumPy exact cosine;
- fuse with RRF and keep source, version, section and page citations on every result;
- provide at least three independently configurable retrieval settings;
- finalise the P03 chunk sizes and overlaps with a documented rationale;
- handle `is_reference` chunks per the architecture;
- implement the scope analyzer, the evaluation-question schema and validator, and the manual grading tooling;
- add offline tests.

Not in scope: authoring or populating questions, answer generation, Groq calls, P04B.

## 2. Implementation Summary

**`src/relay/retrieval/embedder.py`**
- Two Protocols, `Embedder` and `Reranker`; tests use fakes.
- **`FastEmbedEmbedder`:** `BAAI/bge-small-en-v1.5`, 384 dimensions, L2-normalised. Queries get the BGE v1.5 retrieval instruction prefix.
- **`FastEmbedReranker`:** `Xenova/ms-marco-MiniLM-L-6-v2` cross-encoder.
- **Offline by default:** both load from `data/models/` with `local_files_only=True`, 4 fixed threads and batch size 4.
- `describe_cached_model` records the Hugging Face revision and the ONNX SHA-256.

**`src/relay/retrieval/hybrid_index.py`**
- **BM25:** `rank_bm25.BM25Okapi` (k1 1.5, b 0.75) over a deterministic tokenizer. It keeps identifiers whole (`d_model`, `10^20`, `28.4`, `h-net`) and also indexes their parts.
- **Dense:** every chunk is embedded in windows of up to 220 words (step 180) and scored by its best window, using exact NumPy dot product on normalised vectors.
- **Fusion:** **RRF**, `Σ 1/(60 + rank)` over the enabled rankers.
- **References:** `is_reference` chunks get fused score × 0.5 and, after rerank, rerank score − 2.0, unless `include_references=True`.
- **Filtering and rerank:** an optional paper filter, then an optional cross-encoder rerank of the top-N fused candidates.
- **Ties** break by corpus order.
- **Results:** every `RetrievedPassage` carries `paper_id`, `arxiv_id`, `version`, section number/title/path/kind, `page_start`, `page_end`, `is_reference`, per-ranker ranks and scores, and a `citation` string, e.g. `arXiv:2507.07955v2, pp. 4-5, 2.2 Dynamic Chunking (DC)`.
- **Persistence:** `save`/`load` writes `chunks.jsonl`, `dense_windows.npy` and `dense_window_owner.npy` (`allow_pickle=False`) plus a manifest with SHA-256 digests. `load` refuses tampered files, a stale chunk set (checked against `data/papers/manifest.json`), a different embedder or a different tokenizer version.

**`RETRIEVAL_SETTINGS`** are three independently configurable `RetrievalSetting` models. The names are the `retrieval_setting` values that go into `results.csv`. All fields can be overridden with `model_copy` and are validated.

| Setting | Chunks (words / overlap) | Rankers | Rerank | top_k |
| :--- | :--- | :--- | :--- | -: |
| `small_fast` | 160 / 25 | BM25 + dense, RRF | no | 4 |
| `balanced_rerank` | 220 / 35 | BM25 + dense, RRF | top 12 → MiniLM | 4 |
| `large_wide` | 400 / 60 | BM25 + dense, RRF | no | 5 |

**`src/relay/retrieval/scope.py`** (pipeline step 1) is deterministic lexicon/regex:
- **Paper cues:** *strong* cues are unique to one paper (title, Vaswani, Moshi/Mimi/Helium/Kyutai/Inner Monologue, H-Net/dynamic chunking, arXiv IDs). *Weak* cues are shared vocabulary ("Transformer", "speech", "chunk", …). **Only strong cues produce a suggested paper filter.**
- **Other signals:**
  - comparison intent;
  - reference intent, which turns off down-weighting;
  - verbatim technical identifiers;
  - arXiv version mentions, with `version_mismatch` for non-pinned versions;
  - corpus term coverage against the index vocabulary;
  - an `out_of_corpus_signal`.

  These are signals only; early refusal is decided by the P05 evidence gate.

**`src/relay/eval/questions.py`** implements the ARCH §11 schema as strict Pydantic:
- **Fields:** `id`, `author`, `category` (5 values), `question`, `answerable`, `expected_answer`, `required_facts` (with `|` alternatives), `gold_citations{paper_id, page, section, quote}`, `forbidden_claims`, `trap_type` (4 values or null), `notes`.
- **Consistency rules:**
  - `unanswerable` exactly when `answerable=false`;
  - answerable questions need facts, citations and an expected answer;
  - pages are checked against the manifest's page counts;
  - ids are unique;
  - template placeholders `<…>` are rejected.
- **Coverage (strict):** at least 15 questions, at least 3 unanswerable, at least 3 traps, at least 3 distinct trap types. Warnings for fewer than 18 questions and for missing categories.

**`src/relay/eval/grader.py`**
- **Proposals:** a deterministic first pass proposes one of the four labels with reasons, following ARCH §11 exactly. That includes "a correct fact with a wrong or missing citation is `wrong`" and page-range citations covering the gold page.
- **Review:** `GradeRecord` starts `pending`. Owner review either *confirms* the proposal or *overrides* it; an override requires a note. Only reviewed grades are returned by `final_grades()`.
- **Storage:** grades go in `grades.json`, stored with the run evidence (ARCH §13.1).

**Scripts**
- `scripts/build_indexes.py`: `--download-models` (one time), an offline build, and `--check` (rebuild in memory; exit 1 on drift). It writes the tracked `data/retrieval_manifest.json`.
- `scripts/validate_questions.py`.
- `scripts/grade_answers.py`: `propose` / `show` / `review` / `summary`. `propose` never overwrites an existing grades file.

**Owner files**
- `eval/questions.template.json` contains placeholders only and the validator rejects it.
- `eval/README.md` explains how to write and grade questions.
- **`eval/questions.json` was not created.** The owner writes it.

**P03 extraction limitations preserved.** The source text is unchanged:
- flattened equations, merged table headers and questionable hyphen joins remain in the chunks;
- the tokenizer normalises only case and width, without rewriting content;
- `eval/README.md` lists them as candidate `garbled_extraction` traps.

## 3. Files Created

- `src/relay/retrieval/__init__.py`, `embedder.py`, `hybrid_index.py`, `scope.py`.
- `src/relay/eval/__init__.py`, `questions.py`, `grader.py`.
- `scripts/build_indexes.py`, `scripts/validate_questions.py`, `scripts/grade_answers.py`.
- `eval/questions.template.json`, `eval/README.md`.
- `tests/test_retrieval.py` (21), `tests/test_scope.py` (16), `tests/test_grader.py` (28).
- `data/retrieval_manifest.json` (**tracked**): model provenance, library versions, settings, per-index digests.
- **Gitignored:**
  - `data/models/` (two models, 152 MB);
  - `data/indexes/{small_fast, balanced_rerank, large_wide}/`.
- `reports/phase-04-retrieval-and-evaluation-tooling.md`: this report.

## 4. Files Modified

- **`src/relay/ingestion/chunker.py`:** `CHUNK_PRESETS` finalised (180/30 → 160/25, 330/50 → 220/35, 600/90 → 400/60), plus documentation. No logic change.
- **`data/papers/manifest.json`:** regenerated by `ingest_papers.py --offline`. Only the chunk-set counts and digests changed; paper checksums and page counts are unchanged.
- **`ARCHITECTURE.md` §6:** final presets, rationale, RRF and the reference policy, marked "pending owner approval".
- **`.gitignore`:** `+data/models/`.
- **`README.md`:** a retrieval and eval tooling section.
- **`reports/phase-03-pdf-ingestion.md`:** Owner Approved (2026-11-12).
- **`ENGINEERING_LOG.md`:** a P03 approval entry and the P04 entry.
- No P01/P02 module was changed, and nothing was installed (fastembed, onnxruntime, rank-bm25 and numpy were already present).

## 5. Technical Decisions

### 5.1 Final chunk sizes and overlap: measured rationale

Measurements use the BGE WordPiece tokenizer on the real P03 chunks:

| P03 proposal | tokens p50 / p90 / max | chunks over 512 tokens |
| :--- | :--- | :--- |
| 180 w | 267 / 393 / 549 | 1 / 402 |
| 330 w | 471 / 719 / 880 | **79 / 235 (34 %)** |
| 600 w | 533 / 1247 / 1444 | **84 / 164 (51 %)** |

- **Token density:** the corpus averages **1.56 tokens/word (p90 2.2)**. Per paper: Attention 1.49, Moshi 1.50, H-Net 1.65.
- **Model limits:** both ONNX models see at most 512 tokens. The proposed 330/600-word chunks would have been silently truncated.
- **Section lengths:** median 208 words (Attention), 517 (Moshi), 300 (H-Net).

Decisions:
- **`balanced_rerank` = 220 words.** The cross-encoder must see the whole passage: 220 words ≈ 484 tokens at p90. Result: 16 of 341 chunks exceed 512 tokens, all math- or table-heavy, down from 34 %.
- **Dense windows of ≤ 220 words (step 180), best window scores the chunk.** Every word of every chunk is embedded, whatever its size. With 400-word chunks, only 17 of 355 windows exceed 512 tokens.
- **`small_fast` = 160 words** (p90 350 tokens, none over 512): the cheapest, most precise prompts.
- **`large_wide` = 400 words, top-5,** sized by the LLM prompt budget. The estimated passage context is 5 × 546 ≈ 2,730 BGE tokens (*a proxy estimate; Groq tokenizers differ, and P07 measures the real numbers*). The original 6 × 600 ≈ 5,600 would leave about one call per minute under the measured 8,000 TPM per model, before the possible up-front `max_completion_tokens` reservation seen in P02-LIVE.
- **Overlap ≈ 15 % (25 / 35 / 60 words)** is at least one sentence, so a fact split by a window boundary appears whole in some chunk.
- **Estimated passage tokens per prompt** (proxy): small 960, balanced 1,288, large 2,730. These are three clearly different cost points for Table 3.

### 5.2 Other decisions

1. **Reranker downloaded with the embedder.** `balanced_rerank` requires the planned cross-encoder (ARCH §3/§6), so both planned models were fetched in one download session. Every later load is local-only.
2. **BGE query prefix.** FastEmbed does not add BGE's query instruction, so it is applied explicitly and recorded in the manifest.
3. **Threads and batch size.** 4 fixed threads with batch size 4. Profiling 64 chunks: 1 thread / batch 32 took 45.0 s, versus 2.8 s with default threads / batch 4. FastEmbed pads every batch to its longest passage. A fixed thread count keeps ONNX output reproducible, which was verified (§8.3).
4. **BM25 tokenizer keeps compounds and their parts** (`d_model` → `d_model`, `d`, `model`), so "d_model", "d model" and "d-model" all match. The source text is untouched.
5. **References are down-weighted, not excluded:** × 0.5 on the fused score and −2.0 on the rerank logit. They stay reachable, and reference intent (cite / references / `[12]`) disables the down-weighting.
6. **Scope filters only on strong cues.** Shared vocabulary such as "Transformer" or "chunk" never narrows the search, because that is a known trap. Question words are excluded from coverage.
7. **`out_of_corpus_signal` is advisory.** Lexical coverage alone cannot separate near-topic questions: for example, Moshi's appendix F.1 contains general-knowledge dialogue examples (§8.4).
8. **Fact matching** is a normalised substring test with `|` alternatives. It is deterministic and transparent, and every proposal is owner-reviewed. No LLM judge.
9. **Pending grades never count.** `final_grades()` returns only reviewed rows, so pending proposals can't reach `results.csv`.

## 6. Commands Actually Executed

Git Bash with `PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe`. `.env` was not read; there were no Groq calls.

```bash
# FastEmbed API inspection (signatures, local_files_only, model registry entries)
# ONE-TIME model download (network: Hugging Face only), sandbox lifted for this command:
$PY -c "TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='data/models'); TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='data/models')"
#   embedder ok 57.0 s dim 384 ; reranker ok 36.5 s ; data/models = 152M
#   snapshots: Qdrant/bge-small-en-v1.5-onnx-Q@aa8f8b06..., Xenova/ms-marco-MiniLM-L-6-v2@a0914435...
git check-ignore -v data/models/x                     # .gitignore:24:data/models/
# Token measurements on P03 chunks (tokenizers + the BGE tokenizer.json)
$PY scripts/ingest_papers.py --offline   (x2)          # corpus manifest IDENTICAL; --check unchanged
export HF_HUB_OFFLINE=1
$PY scripts/build_indexes.py             # first attempt: very slow (1 thread, batch 32) -> stopped
# profiling: threads 1/batch 32 = 44.96 s, 1/4 = 12.57 s, default/32 = 5.36 s, default/4 = 2.80 s (64 chunks)
$PY scripts/build_indexes.py             # 4 threads, batch 4: written in 1m38s
$PY scripts/build_indexes.py --check     # {"outcome": "checked", "manifest_unchanged": true}
# Offline smoke queries on the real indexes (engineering sanity checks, NOT evaluation)
$PY -m pytest -q -p no:cacheprovider tests/test_retrieval.py tests/test_scope.py tests/test_grader.py
#   run 1: 2 failed, 63 passed  ->  fixes  ->  65 passed
$PY -m ruff check .                      # 4 findings -> fixed -> All checks passed!
# Final verification after the last code edit:
$PY scripts/build_indexes.py --check     # manifest_unchanged: true, exit 0
$PY scripts/ingest_papers.py --check     # manifest_unchanged: true, exit 0
$PY -m pytest -q -p no:cacheprovider -rs # 274 passed in 3.47s (no skips)
$PY -m ruff check .                      # All checks passed!
$PY scripts/validate_questions.py --file eval/questions.template.json --allow-incomplete  # exit 1 (placeholders)
$PY scripts/validate_questions.py        # exit 1: eval/questions.json not found (owner has not written it)
```

**Verification commands for the owner (PowerShell):**

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
$env:HF_HUB_OFFLINE = "1"          # guarantees no model download is attempted
python -m pytest -q
python -m pytest -v tests/test_retrieval.py tests/test_scope.py tests/test_grader.py
python -m ruff check .
python scripts/ingest_papers.py --check; $LASTEXITCODE      # manifest_unchanged true, 0
python scripts/build_indexes.py --check; $LASTEXITCODE      # manifest_unchanged true, 0 (~1.5 min)
python scripts/validate_questions.py --file eval/questions.template.json --allow-incomplete; $LASTEXITCODE  # 1 by design
git check-ignore -v data/models/x data/indexes/x data/processed/x
git status --short
```

## 7. Test Results

These are from the final runs, after the last code edit:
- `python -m pytest -q`: **274 passed, 0 failed, 0 skipped** (3.47 s, `filterwarnings=error`). The two real-data tests ran because the PDFs and indexes are present.
- Per file: retrieval 21, scope 16, grader 28 (new); ingestion 46, http_provider 75, probe_groq 15, schemas 29, foundation 21, run_store/aggregation 23.
- `python -m ruff check .`: **All checks passed!**

What the new tests cover:
- **Indexing and retrieval:**
  - tokenizer identifiers and parts;
  - dense windows with no truncation and the best-window score;
  - BM25 identifier ranking;
  - **RRF scores equal the reciprocal-rank sums**;
  - BM25-only and dense-only settings;
  - **reference down-weighting (×0.5) and its reversal under reference intent**;
  - paper filter, plus invalid and empty filters;
  - **rerank reordering and the reference penalty**;
  - **citations carry paper, version, section and page range**;
  - setting validation (3 invalid cases) and the three distinct settings.
- **Persistence and determinism:**
  - **save/load parity** (identical results for 4 queries);
  - tamper, stale and wrong-embedder refusals;
  - **deterministic builds and results**; tie-breaking by corpus order;
  - real indexes load and match the manifest digests, with a BM25 smoke check on Attention p. 8.
- **Scope:** strong cues (6 cases); shared vocabulary weak and never filtering; comparison (including "more than" not counting); reference intent (4 cases); verbatim identifiers; version mismatch; coverage and the out-of-corpus signal; determinism.
- **Questions:**
  - a strict pass on a valid 15-question synthetic set;
  - strict vs lenient coverage;
  - **12 malformed-question rejections**, including an unknown category, an unknown trap type, a wrong paper version and placeholders;
  - duplicate ids and out-of-range pages;
  - **the template is rejected**;
  - **unknown grade labels are rejected**.
- **Grading:**
  - correct;
  - alternative wording;
  - **a correct fact with a wrong page, wrong paper or no citation is `wrong`**;
  - page-range hits;
  - a missing fact or forbidden claim is `wrong`;
  - **all four labels**;
  - the pending → confirm / override workflow, with the note required;
  - grades-file round trip;
  - the end-to-end CLI (propose, refusal to overwrite, override, validate).
- **Fixtures:** all test questions are synthetic fixtures ("Fixture question about value X?"), not evaluation content.

## 8. Actual Output and Generated Artifacts

### 8.1 Models (downloaded once; recorded in `data/retrieval_manifest.json`)

| Role | Model | HF repo @ revision | ONNX file | SHA-256 | Bytes |
| :--- | :--- | :--- | :--- | :--- | -: |
| Embedder (384-d, 512 tok) | `BAAI/bge-small-en-v1.5` | `Qdrant/bge-small-en-v1.5-onnx-Q` @ `aa8f8b060edb00e03bfdd08813a2949946c8ba55` | `model_optimized.onnx` | `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431` | 66,465,124 |
| Reranker (512 tok) | `Xenova/ms-marco-MiniLM-L-6-v2` | `Xenova/ms-marco-MiniLM-L-6-v2` @ `a09144355adeed5f58c8ed011d209bf8ee5a1fec` | `onnx/model.onnx` | `c623d0bcb99f4622beb413eaef00cfbe5db20df9f1dd982da4b4f26022881870` | 90,992,115 |

- **Libraries:** fastembed 0.9.1, onnxruntime 1.31.1, numpy 2.5.4, rank-bm25 0.2.2, tokenizers 0.23.3.
- **Query prefix:** "Represent this sentence for searching relevant passages: ".
- **Threads:** 4.

### 8.2 Indexes

| Preset | Chunks (Attention / Moshi / H-Net) | Reference chunks | Dense windows | Windows over 512 tokens | Chunks over 512 tokens | Chunk-set SHA-256 | Vectors SHA-256 |
| :--- | :--- | -: | -: | -: | -: | :--- | :--- |
| `small_fast` | 442 (50 / 229 / 163) | 64 | 442 | 0 | 0 | `0026cbce…e0e99929` | `fd404047…bbf8adb7` |
| `balanced_rerank` | 341 (42 / 173 / 126) | 47 | 341 | 16 | 16 | `a28e046b…a9c70a3` | `384da3d5…15fc5ae2` |
| `large_wide` | 204 (29 / 100 / 75) | 25 | 355 | 17 | 110 (dense covered by windows) | `4a963959…79c09dd7` | `3e5f482c…c3cebdb82` |

- BM25 vocabulary: 8,918 terms.
- Full digests are in `data/retrieval_manifest.json`.
- For each preset, the chunk-set SHA-256 equals the index's chunk digest, so the index matches the corpus manifest.

### 8.3 Reproducibility

- `build_indexes.py --check` rebuilds every index in memory with the real ONNX models. The result is **byte-identical vectors** (`manifest_unchanged: true`), checked twice, including after the final code edit.
- `ingest_papers.py --check` → unchanged.

### 8.4 Offline smoke queries on the real indexes

These are engineering sanity checks written by Claude. They are **not** evaluation questions, are **not graded**, and show the top-3 citations only.

| Smoke query | Scope | Observation |
| :--- | :--- | :--- |
| "What is the dimensionality d_model of the base Transformer model?" | weak: Attention; no filter; id `d_model` | `small_fast` / `balanced_rerank` top hit: Attention §6.2 (Table 3, pp. 8–9). **`large_wide` top hit came from H-Net §3.2**, because H-Net also uses `d_model`: a live `shared_vocabulary` failure case. |
| "How many codebooks does Mimi use and at what frame rate?" | strong: Moshi → filter | All three settings return Moshi §3.3.1 / §5.2 pages. |
| "What does the routing module in dynamic chunking compute?" | strong: H-Net | H-Net §1 / §2.2.x; rerank score 5.13. |
| "Which papers are cited for layer normalization?" | reference intent | Down-weighting off; `balanced_rerank` ranks the H-Net References chunk (p. 30) first. |
| "What BLEU did the big Transformer get on English-to-French?" | weak: Attention; ids `BLEU`, `English-to-French` | All settings: Attention §6.1, p. 8, ranked first by both rankers. |
| "What is the capital of France?" | no cues; coverage 0.5, so **no** out-of-corpus signal | Top hit: Moshi Appendix F.1 "General knowledge" (synthetic dialogue examples). Cross-encoder scores are about −10, which is the clear signal for the P05 evidence gate. |

**Measured latency** (single offline run, 4 threads): BM25 + dense + RRF ≈ 7–19 ms per query. With rerank of 12 × 220-word passages: ≈ 0.6–0.8 s per query.

## 9. Errors and Root-Cause Analysis

1. **First index build was far too slow,** with only one preset written after about 10 minutes. Root cause: `threads=1` plus `batch_size=32`; FastEmbed pads each batch to its longest passage. Profiled (§6).
2. **`HybridIndex.build([])` raised a numpy error before the intended contract check.** Root cause: the empty-set guard ran after embedding. Found by a test.
3. **Scope coverage was diluted by question words** ("many", "use"), so an on-topic test question scored 0.6 instead of 1.0. Root cause: coverage counted every non-stopword token.
4. **Scripted edit not applied.** An edit removing "than" from the comparison pattern failed its own assertion because of heredoc escaping. It was caught and re-done with the editor. A test now proves "more than 2" is not a comparison.
5. **Ruff:** two E501, two SIM905; the auto-fix for SIM905 then produced long lines.

## 10. Fixes Applied

1. Fixed 4 threads and batch size 4. The full build takes 1 min 38 s, and determinism was re-verified with `--check`.
2. The empty-chunk guard now runs before embedding.
3. A small question-word set is excluded from coverage only. BM25 tokenisation is unchanged, so the indexes stay valid.
4. Comparison regex corrected, with a test.
5. Long lines wrapped and the word lists reflowed.

Re-verification: 274 passed, ruff clean, both `--check` runs unchanged (§6).

## 11. Outstanding Issues

1. **Truncation.** 16 `balanced_rerank` chunks and 17 dense windows still exceed 512 tokens, all on math- or table-heavy pages. Their tails are truncated *for scoring only*; the stored text is complete.
2. **Rerank latency** of about 0.7 s per query adds to the `retrieval_ms` of `balanced_rerank` rows. It is measured, not hidden.
3. **Scope out-of-corpus signal** is lexical and advisory. Near-topic unanswerable questions must be caught by cite-or-refuse and the verifier (ARCH §6 known limitation). The cross-encoder score is a candidate evidence-gate signal for P05.
4. **Grade application.** The grader matches facts by normalised substring. Paraphrased facts need `|` alternatives or an owner override; every proposal is reviewed anyway. **Applying final grades to `rows.csv` (`answer_grade`) is P07 work.**
5. **`eval/questions.json` does not exist.** It awaits the owner's questions, after which P04B verifies them against the PDFs.
6. **Pending owner approval:** the finalised presets (ARCH §6) and the setting parameters (RRF k 60, reference ×0.5 / −2.0, rerank 12, top-k 4/4/5).

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P04 + owner prompt) | Status | Evidence |
| :--- | :--- | :--- |
| Local BM25 + dense indexes over the P03 chunks | `PASS` | §8.2; `data/indexes/*` |
| FastEmbed planned ONNX model, downloaded once, model/version recorded | `PASS` | §8.1; one download session; `local_files_only` afterwards |
| NumPy exact cosine | `PASS` | `HybridIndex.dense_scores` (normalised dot product) |
| Hybrid retrieval with RRF | `PASS` | `test_rrf_scores_are_reciprocal_rank_sums` |
| Source, version, section and page citations in every result | `PASS` | `RetrievedPassage` fields; citation test |
| ≥ 3 independently configurable settings | `PASS` | `RETRIEVAL_SETTINGS`; settings tests |
| Chunk sizes/overlap finalised with rationale, reproducible | `PASS` | §5.1; `ingest --check` / `build --check` unchanged |
| Reference handling per architecture (down-weight unless asked) | `PASS` | down-weighting and rerank penalty tests |
| Embedder behind Protocol (fake in tests); paper filter; optional rerank | `PASS` | fakes in `test_retrieval.py`; filter and rerank tests |
| Scope analyzer per ARCH §6 | `PASS` | `tests/test_scope.py` (16) |
| Eval schema per ARCH §11, validator rejects malformed questions and unknown labels | `PASS` | 12 rejection cases; template rejected; label test |
| Grader proposes one of four labels with reasons; owner overrides; "correct fact, wrong citation → wrong" | `PASS` | `tests/test_grader.py` |
| Index save/load parity | `PASS` | `test_save_load_parity` |
| Deterministic output | `PASS` | determinism tests; byte-identical `--check` on real models |
| Questions not authored or populated by Claude | `PASS` | only the placeholder template and synthetic test fixtures exist |
| No generation, no Groq, no P04B | `PASS` | 0 Groq calls; no generation code |
| Offline `pytest`, `ruff`; one networked model download (reported) | `PASS` | 274 passed; ruff clean; §6 |

## 13. Remaining Risks

- **Shared vocabulary.** The `d_model` smoke query showed `large_wide` ranking an H-Net chunk first, so cross-paper vocabulary still misleads. This is expected and should be measured in Table 3. Scope paper filters help only when strong cues are present.
- **Reproducibility across machines.** ONNX results are reproducible here with 4 fixed threads; another CPU or onnxruntime build may differ slightly. `build_indexes.py --check` would detect that.
- **Prompt-token estimates** in §5.1 use the BGE tokenizer as a proxy. Real Groq prompt tokens must be measured in P07, the first real-answer run.

## 14. Recommended Next Phase

- The owner writes `eval/questions.json` (at least 15, target 18). **P04B** then verifies it against the PDFs. P05 (pipeline, evidence gate, verifier, safe refusal) can proceed in parallel per the plan.
- **Owner decisions needed:** approve the finalised presets and setting parameters (§5, §11 item 6).

Stopping here; no further phase starts without explicit owner approval.
