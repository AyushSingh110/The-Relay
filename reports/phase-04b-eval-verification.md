# Phase 04B Implementation Report — Evaluation Question Verification

> **Phase ID:** `p04b`
> **Date:** `2026-11-12 19:57 IST`
> **Status:** `Completed` (verification only). **No question was edited.** `eval/questions.json` SHA-256 is `14616cd5271b08c911c418787d6e95c20f7ba043ae6bb345d2b91e4b22d085af` before and after.
> **Owner Approval:** `Owner Approved (2026-11-12)`

---

## 1. Objective

Verify the owner-written `eval/questions.json` (21 questions) against the extracted text and raw PDFs of the pinned papers (`1706.03762v7`, `2410.00037v1`, `2507.07955v2`):
- every expected answer, required fact, gold citation, page, section and quote;
- that each unanswerable question is unsupported by all three papers;
- whether gold pages are reachable under all three retrieval settings;
- the known traps.

Report each question as confirmed or discrepancy found. Proposed corrections are listed separately for owner approval (§8.3).

Out of scope, and not done: editing questions, inventing evidence, changing the corpus, building retrieval features, Groq calls, P05.

## 2. Implementation Summary

No repository code was written or changed. The verification used a read-only scratch harness (outside the repo) over the existing P03/P04 artefacts and library functions. It ran these checks:

1. **Quotes.** Each of the 37 gold quotes was matched against our extracted page text (exact after whitespace/quote normalisation, plus an alphanumeric-only match) **and** against PyMuPDF's raw page text. The search covered the stated page, every other page, and page-boundary spans.
2. **Sections.** The cited section was compared with the section of the block containing the quote.
3. **Required facts.** Each `|` alternative was located: in the expected answer, on the gold pages, and anywhere in the cited paper.
4. **Grader self-check.** The P04 grader graded the owner's own `expected_answer` with the gold pages as citations. Anything other than `correct` means the facts and the reference answer disagree.
5. **Expected-answer claims.** Every number and named entity beyond the quotes was searched in the papers and read in context.
6. **Unanswerable questions.** Each topic's key terms were searched across all three papers, with every hit tagged body or reference.
7. **Reachability.** Each question ran under `small_fast`, `balanced_rerank` and `large_wide`, unfiltered (as the pipeline's first retrieval will be), with `include_references` taken from the scope analyzer. The rank of every gold page was recorded, and also with the scope analyzer's suggested paper filter.

## 3. Files Created

- `reports/phase-04b-eval-verification.md`: this report.
- Scratch only (not in the repo): the verification harness and its JSON and text outputs in the session scratchpad.

## 4. Files Modified

- `ENGINEERING_LOG.md`: P04B entry appended.
- **Not modified:** `eval/questions.json` (hash above), the corpus, indexes and code.

## 5. Technical Decisions

- **Verdict scale:**
  - **CONFIRMED:** quotes, pages, sections and answer claims verified; facts grounded; self-grade `correct`.
  - **CONFIRMED WITH NOTES:** correct, with a wording or grading-strength observation.
  - **DISCREPANCY:** something must change for the question to grade correctly or be factually exact.
- **Two text sources for quotes.** Using both extracted and raw text separates a wrong quote from an extraction artefact. All quotes passed both.
- **Unfiltered first retrieval** matches the pipeline (ARCH §6). Filtered results are reported separately.
- **Figure evidence cannot be verified.** Claims that rest only on figure *plots* can't be checked from text and are reported as unverifiable rather than assumed.

## 6. Commands Actually Executed

```bash
PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe; export HF_HUB_OFFLINE=1
$PY scripts/validate_questions.py                          # 21 questions, errors [], warnings [], exit 0
sha256sum eval/questions.json                              # 14616cd5...d085af (start)
$PY <scratch>/p04b_verify.py <scratch>/p04b.json           # "ok 21" (offline; local models)
# claim/term searches and q21 reference-rank probe (read-only Python over data/processed, data/indexes)
$PY -m pytest -q -p no:cacheprovider tests/test_grader.py tests/test_retrieval.py tests/test_scope.py   # 65 passed in 0.98s
$PY -m pytest -q -p no:cacheprovider                       # 274 passed in 4.35s
$PY -m ruff check .                                        # All checks passed!
sha256sum eval/questions.json                              # 14616cd5...d085af (end, unchanged)
```

**Verification commands for the owner (PowerShell):**

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
$env:HF_HUB_OFFLINE = "1"
python scripts/validate_questions.py; $LASTEXITCODE                        # valid, 0
python -m pytest -q tests/test_grader.py tests/test_retrieval.py tests/test_scope.py
python -m pytest -q
python -m ruff check .
Get-FileHash eval/questions.json -Algorithm SHA256                          # 14616CD5...D085AF
```

## 7. Test Results

- `scripts/validate_questions.py` (strict): **valid**, 0 errors, 0 warnings, exit 0.
  - 21 questions: direct_factual 8, multi_hop 4, numeric_technical 2, cross_paper 1, unanswerable 6.
  - Retriever traps: 4 (`shared_vocabulary` 2, `garbled_extraction` 1, `bibliography_match` 1).
- `tests/test_grader.py` + `tests/test_retrieval.py` + `tests/test_scope.py`: **65 passed**.
- Full suite: **274 passed**, 0 failed, 0 skipped. `ruff check .`: **All checks passed!**

## 8. Actual Output and Generated Artifacts

### 8.1 Headline results

| Check | Result |
| :--- | :--- |
| Gold quotes found verbatim on the stated page (extracted text) | **37 / 37** (exact match after whitespace normalisation) |
| Same quotes in raw PDF text (PyMuPDF) | **37 / 37** |
| Cited sections match the section containing the quote | **37 / 37** |
| Pages within page counts / correct paper versions | **all** (validator + manual) |
| Grader self-check (expected answer + gold citations → `correct`) | **14 / 15** (q09 → `wrong`) |
| Unanswerable questions unsupported by all three papers | **6 / 6 confirmed** |
| Answerable questions with ≥ 1 gold page in top-k | `small_fast` 13/15 · `balanced_rerank` 13/15 · `large_wide` 13/15 |
| Gold pages reached in top-k (out of 31) | 17 · 18 · 19 |
| Questions with **no** gold page reachable in any setting | **q09, q15** |

**Verdicts:** 17 confirmed (8 of them with notes; q21's verdict is confirmed with a separate notes correction) and 4 with discrepancies:
- **q09:** grading-blocking;
- **q15:** citation coverage;
- **q14:** an unsupported phrase;
- **q13:** a minor imprecision.

### 8.2 Per-question findings

Gold-page ranks are given as `small_fast / balanced_rerank / large_wide`; "—" means not in top-k.

**q01** (direct_factual): **CONFIRMED WITH NOTES**
- Both p. 2 quotes are verbatim, §1 Introduction.
- Answer claims "O(n) sequential operations" and "path length O(1)" are verified on **p. 6** (§4 Why Self-Attention; Table 1: "Self-Attention … O(1) O(1) Recurrent … O(n) O(n)"). p. 6 is not cited, and the `O(1)` fact alternative exists only there.
- Retrieval: p. 2 = 1 / 1 / 1.

**q02** (direct_factual, `garbled_extraction`): **CONFIRMED WITH NOTES**
- Both p. 4 quotes are verbatim, §3.2.1.
- The trap is real but described inaccurately in `notes`. The actual extracted artefact is "…except for the scaling factor of √ d_k . Additive attention computes the compatibility function using a feed-forward network with 1 a single hidden layer." The fraction's "1" is displaced to a later point; the extraction does not read "√ dk . 1". The equation flattens to `softmax(QK^T / √d_k / )V (1)`.
- Retrieval: p. 4 = 1 / 1 / 1. The garbled sentence and the gold sentences share a chunk.

**q03** (direct_factual, `shared_vocabulary`): **CONFIRMED WITH NOTES**
- p. 5 quote verbatim, §3.2.2. `d_model = 512` is verified on p. 3.
- **Trap did not fire:** every top-k passage in every setting was from Attention. Reranking put Table 3 (§6.2, p. 9) first and the gold p. 5 second.
- Several fact alternatives are generic ("key", "value", "64", "eight"); see §8.3 G-1.
- Retrieval: p. 5 = 1 / 2 / 1.

**q04** (direct_factual): **CONFIRMED WITH NOTES**
- 3 quotes verbatim, p. 5, §3.2.3. "−∞" masking is verified on p. 5.
- Facts `encoder|self-attention` and `decoder|self-attention` are satisfied by almost any answer (§8.3 G-1).
- Retrieval: p. 5 = 1 / 1 / 1.

**q05** (multi_hop): **CONFIRMED**
- 2 quotes verbatim, p. 6, §3.5. "PE_pos+k … linear function of PE_pos" is verified on p. 6.
- Retrieval: p. 6 = 1 / 1 / 1.

**q06** (direct_factual): **CONFIRMED WITH NOTES**
- p. 1 (Abstract) and p. 2 (§1) quotes verbatim. The pipeline components, "several seconds", "emotion and accent" and backchanneling are verified on pp. 1–2.
- The fact `latency` matches any Moshi answer (§8.3 G-1).
- Retrieval: p. 1 = — / 2 / 4; p. 2 = 2 / — / 2.

**q07** (multi_hop): **CONFIRMED WITH NOTES**
- 3 quotes verbatim (p. 3 §1, p. 7 §3.1, p. 13 §3.4.1).
- Every answer claim is verified:
  - 2.1T tokens (p. 3);
  - SeaNet autoencoder (p. 9);
  - Transformer-based bottleneck (p. 10);
  - 24 kHz and 80 ms frames (p. 10);
  - Q = 8 codebooks at 12.5 Hz (p. 12);
  - WavLM distillation (pp. 10–11);
  - Temporal Transformer initialised from Helium (pp. 8, 20);
  - temporal context vector z_s (p. 13).
- Facts `Helium|7B` and `Mimi|neural audio codec` are satisfied by just naming the components (§8.3 G-1).
- Retrieval: p. 3 = — / 4 / —; p. 7 = 4 / 3 / 4; **p. 13 = — / — / —**.

**q08** (multi_hop): **CONFIRMED**
- 3 quotes verbatim (p. 3, p. 15 §3.4.4, p. 29 §5.5).
- Verified: word-level Whisper timestamps and PAD/EPAD (p. 16); "17 tokens, instead of 16" (p. 29); the "scaffolding" wording (p. 15).
- Retrieval: p. 3 = 1 / 1 / 3; p. 15 = — / — / 4; **p. 29 = — / — / —**.

**q09** (direct_factual): **DISCREPANCY (grading-blocking)**
- Both quotes are verbatim (p. 2 §1, p. 15 §3.4.3), so the evidence is right.
- **The grader self-check gives `wrong`.** Two required facts don't occur in the owner's own expected answer:
  - `parallel streams|two autoregressive token streams`: the answer says "two parallel audio token streams";
  - `removes the concept of speaker turn|removal of explicit speaker turns`: the answer says "removes the concept of explicit speaker turns".
- **The facts' own wording comes from p. 1 (Abstract)**, which is not a gold citation: "…modeling separately its own speech and that of the user into **parallel streams**. This allows for the **removal of explicit speaker turns**…"
- **No gold page is reachable in any setting**, with or without the Moshi filter. The retriever returns p. 6–7 §3.1 ("Moshi is a multi-stream speech-to-speech Transformer model, which allows for full-duplex spoken dialogue…"; "able to both speak and listen to the user at the same time, and does not need to explicitly model speaker turns"), p. 3 §1 and, in `large_wide`, p. 1 (rank 4).
- A correct, well-cited answer would therefore currently be graded `wrong`. Proposals C-1 and C-2 in §8.3.

**q10** (numeric_technical): **CONFIRMED WITH NOTES**
- 3 quotes verbatim (p. 1, p. 2, p. 26 §5.3).
- Verified: 80 ms frame size and stride (p. 10); 230 ms over 10 languages (p. 2); acoustic delay 2 → 1 for 160 ms (p. 26).
- Two forbidden claims are full sentences ("Moshi has a theoretical latency of 80ms in its final full model", "…practical latency of 640ms"). Sentences like these almost never match an answer by substring (§8.3 G-2).
- Retrieval: p. 1 = — / 3 / 4; p. 2 = 1 / 1 / 1; **p. 26 = — / — / —**.

**q11** (direct_factual): **CONFIRMED**
- 2 quotes verbatim (p. 1, p. 2, §1).
- Chinese/code/DNA (pp. 1, 10, 15) and "scaling more powerfully with data and parameters" (p. 1, footnote) are verified.
- Retrieval: p. 1 = 3 / 2 / 4; **p. 2 = — / — / —**.

**q12** (direct_factual): **CONFIRMED WITH NOTES**
- 2 quotes verbatim (p. 4 §2.1.1, p. 5 §2.1.2).
- Verified: autoregressive U-Net (p. 2); Mamba-2 encoder/decoder (p. 5); "majority of model capacity in the main network … L^S≪L^0" (p. 5); eq. (3) `Dechunk(…) + Linear(x̂^s)` (p. 5).
- Facts `encoder` / `main network` / `decoder` repeat the question, and `Linear` substring-matches "linearly" and "nonlinear" (§8.3 G-1).
- Retrieval: p. 4 = 1 / 1 / 1; p. 5 = 2 / 1 / 1.

**q13** (cross_paper, `shared_vocabulary`): **DISCREPANCY (minor) + retrieval failure on the cross-paper half**
- 3 quotes verbatim (H-Net p. 6 §2.2.1, p. 7 §2.2.2; Attention p. 4 §3.2.1).
- Expected answer: "EMA weighted by p_t". The paper defines eq. (5) `z̄_t = P_t ẑ_t + (1 − P_t) z̄_{t−1}`, with eq. (6) `P_t = p_t if b_t = 1, 1 − p_t otherwise` (p. 7). The weight is the confidence P_t, not p_t (C-4).
- The fact alternative `EMA` substring-matches "remains", "schema" and so on (§8.3 G-1).
- **Attention p. 4 is never retrieved in any setting.** The scope analyzer sees a strong H-Net cue but only a weak Attention cue ("Transformer"), so its suggested filter is H-Net only, which would exclude the Attention evidence outright. The `shared_vocabulary` trap fires in the opposite direction: the retriever never surfaces the Transformer side.
- Retrieval: H-Net p. 6 = 4 / 4 / —; p. 7 = 4 / 4 / 3; **Attention p. 4 = — / — / —**.

**q14** (multi_hop): **DISCREPANCY (minor, unsupported phrase)**
- 3 quotes verbatim (p. 7 §2.2.2; p. 17 and p. 18 §3.3). The quote is correctly split at the p. 17→18 page break.
- Verified: the "product" / "du" example (p. 7); L^1/L^0 and L^2/L^1 (p. 18, Fig. 7).
- **"Removing it is the most damaging ablation" is not stated in the text.** p. 17 says "The smoothing module proves essential for stable training dynamics". The relative size of the ablations is only shown in Figure 7, a plot that can't be checked from text (C-5).
- Retrieval: p. 7 = 3 / — / 2; p. 17 = 1 / 1 / 1; p. 18 = 1 / 1 / 1.

**q15** (numeric_technical): **DISCREPANCY (citation coverage)**
- 4 quotes verbatim (p. 4 §1; p. 12 §3.1 ×2; p. 17 §3.2). The figures are verified:
  - 200B / 100B / 30B crossovers (p. 12);
  - 2.2 % / 2.6 % (p. 12);
  - "twice its size" (pp. 1, 4);
  - 3.6× (pp. 4, 17);
  - Large H-Net (2-stage) average 55.5 = XL Transformer average 55.5 (downstream-evaluation table, p. 13).
- **No gold page is reachable in any setting.** The direct statement of the effect of stages is on **p. 22 (§4 Discussion)**: "iterating H-Net from 0 stages (i.e. an isotropic model) to 1 stage and from 1 stage to 2 stages consistently improves performance. We did not attempt a 3-stage H-Net at all". It also appears on **p. 1 (Abstract)**: "Iterating the hierarchy to multiple stages further increases its performance…". The retriever returns p. 22 in `balanced_rerank` and `large_wide`, but an answer citing p. 22 would grade `wrong` (C-3).
- Wording: "boosts XWinograd-zh from 59.9 to 66.3". The paper compares H-Net (2-stage) at 66.3 with the **tokenized Transformer** at 59.9 (p. 4; Table 4, p. 17: Transformer 0.599, H-Net (2-stage) 0.663) (C-6).
- The forbidden claim "The paper also evaluates a 3-stage H-Net" is correctly contradicted by p. 22.
- Retrieval: **p. 4 / p. 12 / p. 17 = — / — / — in all settings.**

**q16** (unanswerable: blockchain consensus): **CONFIRMED UNANSWERABLE**
- 0 hits for blockchain / proof of work / proof of stake / consensus / cryptocurrency / miner / validator in all three papers.
- Top rerank score −8.48.
- Scope: no cues, coverage 0.70, so no out-of-corpus signal.

**q17** (unanswerable: photosynthesis): **CONFIRMED UNANSWERABLE**
- 0 hits for photosynthesis / Calvin / chlorophyll / thylakoid / stroma / chloroplast.
- Top rerank score −10.20.

**q18** (unanswerable: TCP handshake): **CONFIRMED UNANSWERABLE**
- 0 hits for TCP / handshake / SYN / ACK / packet.
- Top rerank score −10.27.
- **This is the only question that raises the scope `out_of_corpus_signal`** (coverage 0.33).

**q19** (unanswerable: Newton's laws): **CONFIRMED UNANSWERABLE**
- 0 hits for Newton / laws of motion / inertia / F = ma / "equal and opposite".
- Top rerank score −10.28. The top hit is Moshi App. F (synthetic general-knowledge transcripts).

**q20** (unanswerable: MySQL vs MongoDB): **CONFIRMED UNANSWERABLE**
- The only hit is a false positive: "bson" inside the author name "Webson" in an H-Net reference entry (p. 27).
- No MySQL / MongoDB / SQL / NoSQL / relational / database text.
- Top rerank score −9.03.

**q21** (unanswerable: WavLM pretraining, `bibliography_match`): **CONFIRMED UNANSWERABLE; trap works as designed**
- **What the corpus says about WavLM.** It is mentioned in the Moshi body on **pp. 10, 11, 12, 22, 24, 38, 53**, and in the reference entry on **p. 43** ("Wavlm: Large-scale self-supervised pre-training for full stack speech processing"). Its uses are distillation targets (16 kHz, 1024-d, 50 Hz; pp. 10–12), ablation tables (pp. 22, 24, 53) and speaker verification (p. 38).
- **WavLM's pretraining is never described.** "masked speech prediction", "utterance mixing", "gated relative position bias", "overlapped speech" and "k-means" have no hits. The only "masked prediction" text in the corpus is the title of the *HuBERT* reference (p. 46, a reference entry).
- **Retrieval:**
  - The top-1 hit in all settings is Moshi §3.3.2 (WavLM distillation, pp. 11–12), with rerank +2.09 against −8.5 to −10.3 for q16–q20. A score-based gate would therefore pass q21 to the LLM, which tests cite-or-refuse as intended.
  - **Bibliography trap:** with the default reference down-weighting, no reference chunk is in the top 12 in any setting. With down-weighting lifted, the WavLM reference entry (p. 43) ranks 7 / 6 / 2. The P04 reference policy is what defuses this trap.
- **Note correction (C-7):** `notes` lists "pp. 4, 10-11, 38, ref p. 43". p. 4 has no WavLM mention, and pp. 12, 22, 24, 53 are missing.

### 8.3 Proposed corrections, for owner approval (none applied)

**Discrepancies affecting grading or accuracy:**

| ID | Question | Field | Proposal | Evidence |
| :--- | :--- | :--- | :--- | :--- |
| C-1 | q09 | `required_facts` | Widen the alternatives so the reference answer and close paraphrases pass, e.g. `"parallel streams\|parallel audio token streams\|two autoregressive token streams\|two parallel"` and `"speaker turn"` (or `"removes the concept of speaker turn\|explicit speaker turns\|removal of explicit speaker turns"`) | self-grade `wrong`; p. 1 / p. 2 wording |
| C-2 | q09 | `gold_citations` | Add **p. 1 (Abstract)** with the quote "Moshi generates speech as tokens from the residual quantizer of a neural audio codec, while modeling separately its own speech and that of the user into parallel streams. This allows for the removal of explicit speaker turns, and the modeling of arbitrary conversational dynamics." Optionally add **p. 7 (§3.1)**: "The model is able to both speak and listen to the user at the same time, and does not need to explicitly model speaker turns." | the facts originate on p. 1; p. 1 and p. 6–7 are what retrieval returns |
| C-3 | q15 | `gold_citations` | Add **p. 22 (§4 Discussion)**: "In this paper, we showed that iterating H-Net from 0 stages (i.e. an isotropic model) to 1 stage and from 1 stage to 2 stages consistently improves performance." Optionally add **p. 1 (Abstract)**: "Iterating the hierarchy to multiple stages further increases its performance by modeling multiple levels of abstraction, demonstrating significantly better scaling with data and matching the token-based Transformer of twice its size." | the direct statement of the asked effect; p. 22 is retrieved |
| C-4 | q13 | `expected_answer` | "EMA weighted by p_t" → "EMA weighted by the confidence P_t (P_t = p_t at boundaries, 1 − p_t otherwise)" | eqs. (5)–(6), p. 7 |
| C-5 | q14 | `expected_answer` | Replace "removing it is the most damaging ablation" with text-supported wording, e.g. "the smoothing module proves essential for stable training dynamics". Or keep it if the owner confirms it from Figure 7 (a plot, not text-verifiable) | p. 17 |
| C-6 | q15 | `expected_answer` | "boosts Chinese XWinograd-zh from 59.9 to 66.3" → "reaches 66.3 on XWinograd-zh versus 59.9 for the tokenized Transformer" | p. 4; Table 4, p. 17 |

**Notes accuracy (no grading effect):**

| ID | Question | Proposal |
| :--- | :--- | :--- |
| C-7 | q21 | Correct the page list to "pp. 10–12, 22, 24, 38, 53; reference p. 43" (no WavLM mention on p. 4). |
| C-8 | q02 | Describe the artefact as extracted: "…scaling factor of √ d_k ." with the fraction's "1" displaced to "…a feed-forward network with 1 a single hidden layer". |
| C-9 | q01 | Optional: add p. 6 (§4 / Table 1) as a gold citation, since the expected answer's O(n) / O(1) claims and the `O(1)` fact alternative are only there. |

**Grading strength (optional; the owner reviews every grade anyway):**
- **G-1. Generic or substring-prone fact alternatives** make proposals lenient. The grader is a normalised *substring* test, so any answer that repeats the question passes these. Consider more specific phrases:
  - q03: `key`, `value`, `64`, `eight`;
  - q04: `encoder|self-attention`, `decoder|self-attention`;
  - q06: `latency`;
  - q07: `Helium`, `Mimi`;
  - q12: `encoder`, `main network`, `decoder`, `Linear` (matches "linearly" / "nonlinear");
  - q13: `EMA` (matches "remains"), `boundary`, `softmax`;
  - q14: `gradient`, `substantial`;
  - q15: `3.6` (matches "3.62", "13.6").

  A grader change to word-boundary matching would be a P04 tooling change and is **not** made here; it needs owner approval.
- **G-2. Full-sentence forbidden claims rarely match** an answer verbatim: q01, q04, q05, q06, q07, q08, q09, q10 (two of three), q11, q12, q13, q14, q15. Short distinctive phrases would catch more (e.g. `RoPE`, `voice activity detector`, `3-stage`, `640ms`, `80ms`). The short ones already present work: q03, q10 "slower than…", q16–q21.

### 8.4 Trap and retrieval observations

| Trap | Question(s) | Observed |
| :--- | :--- | :--- |
| `shared_vocabulary` | q03 | Did **not** fire: Attention-only top-k in all settings. |
| `shared_vocabulary` | q13 | Fired against the cross-paper half: Attention p. 4 is never retrieved, and the scope filter suggestion (H-Net only) would exclude it. |
| `garbled_extraction` | q02 | Present in the retrieved gold chunk (rank 1). It tests whether the LLM reads the intact sentence or the garbled one. |
| `bibliography_match` | q21 | Defused by reference down-weighting (0 reference chunks in the top 12). Without it, the WavLM reference ranks 2–7. |
| version confusion | (no `version_leak` question) | Not exercised by this set. Every gold citation uses the pinned versions, and the scope analyzer reported no version mentions. |

Further observations:
- **Off-topic unanswerables (q16–q20)** get rerank scores of −8.5 to −10.3. Lexical coverage flags only q18, so the rerank score is the stronger early-refusal signal (P05 design input).
- **Multi-hop and numeric questions lose later evidence pages.** q07 p. 13, q08 p. 29, q10 p. 26 and q11 p. 2 are never in top-k: the first-retrieval top-k is dominated by Introduction / Related Work chunks. This is a measurable Table 3 effect, not a question defect.
- **Reachability is similar across settings** (13/15 questions; 17, 18 and 19 of 31 gold pages). `large_wide` reaches the most pages.

## 9. Errors and Root-Cause Analysis

1. **First results print truncated.** The first results print exceeded the tool's output limit and was saved to a file, which was then read in full.
2. **My WavLM search capped at 12 hits.** The 12-hit display limit in my term search hid the complete list of WavLM mentions. Re-run without the limit (11 mentions, §8.2 q21).

Neither affected the repository.

## 10. Fixes Applied

Both handled as described; all figures in §8 come from the complete outputs.

## 11. Outstanding Issues

1. **Owner decisions needed on C-1 … C-9 and G-1 / G-2.** Until C-1 and C-2 are resolved, a correct answer to q09 grades `wrong` (the owner's review would catch it, but Table 3 should not depend on overrides).
2. **No `version_leak` question.** Version confusion is untested; this is optional coverage, not a validator requirement.
3. **Cross-paper reachability (q13).** The Transformer evidence is unreachable under first retrieval, and the scope filter suggestion would hide it. This is input for P05's evidence gate and loop design (no change made in P04B).
4. **Figure-only evidence** (q14, Figure 7) cannot be text-verified.

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P04B + owner prompt) | Status | Evidence |
| :--- | :--- | :--- |
| `eval/questions.json` exists and is valid before starting | `PASS` | strict validator: 0 errors / 0 warnings |
| Every expected answer, fact, citation, page, section and quote checked against the PDFs | `PASS` | §8.1–§8.2 (37/37 quotes, raw and extracted) |
| Unanswerable questions verified unsupported by all three papers | `PASS` | §8.2 q16–q21 term searches |
| Gold-page reachability under all three settings | `PASS` | §8.1, §8.2 ranks |
| Known traps examined | `PASS` | §8.4 |
| Each question reported as confirmed / discrepancy, with page quotes | `PASS` | §8.2 |
| No silent edits; corrections listed for approval | `PASS` | hash unchanged; §8.3 |
| No invented evidence, corpus change, new retrieval features, Groq or P05 | `PASS` | no repo code changed; 0 Groq calls |
| Validator and relevant offline tests run, outputs reported | `PASS` | §7 |

## 13. Remaining Risks

- **Lenient grading.** Generic fact alternatives (G-1) let wrong but on-topic answers be proposed `correct`. Owner review mitigates this, but it costs review time.
- **Under-counted correct answers.** Gold citations miss pages that legitimately support some answers (q09, q15; optionally q01), so correct answers citing those pages are proposed `wrong`.
- **Uneven difficulty across categories.** Only one cross-paper question exists, and its second-paper evidence is unreachable. Table 3's cross-paper behaviour rests on a single data point.

## 14. Recommended Next Phase

- **The owner reviews §8.3** and applies any approved corrections to `eval/questions.json` (owner edits, or explicit instruction for Claude to apply exactly the approved items). The strict validator should then be re-run.
- **P05** (pipeline, evidence gate, verifier, safe refusal) only after explicit approval. The rerank-score and cross-paper observations in §8.4 are design inputs for it.

Stopping here.

---

## Addendum: Owner-Approved Corrections Applied (2026-11-12)

The owner approved P04B and authorised Claude to apply **C-1 … C-9** plus **data-only G-1 and G-2** inside `eval/questions.json`. `grader.py` and all other code are unchanged. The headline quote count above was corrected from 45 to **37**, a counting error on my part; the per-question findings were unaffected.

**Applied:**
- **C-1 / C-2 (q09):** widened the two required facts; added gold citations p. 1 (Abstract) and p. 7 (§3.1).
- **C-3 (q15):** added p. 22 (§4 Discussion) and p. 1 (Abstract).
- **C-4 / C-5 / C-6:** the wording fixes in q13 (P_t), q14 ("proves essential for stable training dynamics") and q15 (66.3 vs the Transformer's 59.9).
- **C-7 / C-8:** the q21 and q02 notes.
- **C-9 (q01):** added p. 6 (§4).
- **G-1:** generic or substring-prone fact alternatives replaced by specific phrases in q03, q04, q06, q07, q10, q12, q13, q14 and q15.
- **G-2:** short distinctive forbidden phrases **appended** to q01 and q04–q15. The owner's original sentences were kept.

**Integrity proof.** The pre-edit backup could not be taken, because the first edit command failed to parse and never ran. Instead, the applied edits were inverted programmatically, and the result is **byte-identical to the original** (`14616cd5…22d085af`). This proves the file differs from the owner's version only by the listed changes. New `eval/questions.json` SHA-256: `ce4dfe418b92a22b4b88ed6667a1f0d4e4470bc89cb1eb9ee7d18d07f05b3903`.

**Re-verification (same harness, offline):**
- **Quotes:** all 42 gold quotes (37 original + 5 new) are verbatim on their stated pages in both extracted and raw PDF text, with 42/42 sections correct.
- **Grader self-check:** **15/15 `correct`** (q09 was `wrong` before).
- **Forbidden phrases:** none matches its own expected answer.
- **Restatement test:** grading each question's own text as the answer, with gold pages cited, gives `wrong` for all 15, before and after. Facts satisfied by mere restatement fell from 2→0 (q07), 3→0 (q12), 1→0 (q13) and 2→1 (q03).
- **Reachability** (≥ 1 gold page in top-k):

  | Setting | Questions | Gold pages |
  | :--- | :--- | :--- |
  | `small_fast` | **15/15** | 20/36 |
  | `balanced_rerank` | **14/15** | 19/36 |
  | `large_wide` | **15/15** | 22/36 |

  q15 is still unreached by `balanced_rerank`: its reranker prefers the p. 23 Discussion chunk over p. 22.

**Final checks after the edits:** strict `validate_questions.py` → valid, 0 errors, 0 warnings; `pytest -q` → 274 passed; `ruff check .` → All checks passed! (see ENGINEERING_LOG).
