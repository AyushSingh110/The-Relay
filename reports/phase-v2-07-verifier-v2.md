# Phase V2-07 Implementation Report — Verifier v2 (L3 refusal form, L4 entailment)

- **Date:** 2026-11-20
- **Phase:** V2-07 (`docs/IMPLEMENTATION_PLAN_V2.md` §V2-07; `docs/ARCHITECTURE_V2.md` §6, layers L3 and L4)
- **Status:** completed, pending owner review.
  - L3 is complete.
  - L4 is integrated in advisory mode.
  - The calibration finds that L4, as integrated, cannot separate supported from unsupported sentences. An exploratory alternative does much better. Both need your decision (§11).
- **Real Groq calls:** 0. `results.csv` and `evidence/` are unchanged.

## 1. Objective

- **L3:** close the v1 gap where a refusal-shaped sentence ("the passages do not state…") carried a citation tag and was delivered as verified.
- **L4:** add an advisory entailment score to every verified sentence, using a local model.
- **Spike:** measure L4's latency and memory.
- **Calibration:** produce the calibration table that a blocking-threshold decision needs, without setting a threshold.
- **Regression:** re-check the 84-replay against v1.

## 2. Implementation Summary

### Owner decisions taken before coding (docs/DECISIONS.md, 2026-11-20)

- **The L4 model is an ONNX model on the existing onnxruntime.** The candidates were cross-encoder/nli-MiniLM2-L6-H768 and cross-encoder/nli-deberta-v3-xsmall. typeform/distilbert was replaced, because it has no ONNX export and no licence, and converting it needs PyTorch.
- **Calibration labels are per-sentence proposals by Claude Code,** marked unreviewed.
- **In streaming, L3 holds a refusal-shaped first sentence and decides at the end.**
- **In buffered mode, a refusal-shaped sentence among claims makes the answer BLOCKED,** as v1 treats any failed sentence.

### L3: refusal-form detection

**Shared implementation.** `verifier.py` has a `_REFUSAL_FORM` pattern set, `is_refusal_form`, check "f" with failure code `refusal_as_claim`, and `UnitCheck.refusal`. It lives in `SentenceChecker`, which the whole-answer verifier and the streaming verifier share.

**Precedence.** A refusal-shaped sentence is a refusal whatever its citations. Its grounding is meaningless to check.

**Whole-answer verdicts:**
- an answer made only of refusal-shaped sentences is `REFUSAL_VERIFIED`, delivered as the collection's refusal message and cached;
- one refusal-shaped sentence among claims makes the answer `BLOCKED`.

**Streaming** (`streaming.py`):
- **After verified sentences:** the answer stops with `refusal_as_claim` and is partial.
- **Before any release:** the sentence is held.
  - Only refusals follow: a verified refusal, cached.
  - A claim follows: `refusal_with_content`, refused and not cached.

**UI:** the stop reason now reads "the next sentence said the passages do not contain the answer".

### L4: the advisory entailment score

**`EntailmentScorer`** (`verifier.py`) runs one ONNX NLI cross-encoder on onnxruntime + tokenizers:
- the entailment class is read from `config.json`;
- `onnxruntime` is imported only when a scorer is built.

**`score_sentence`** (`streaming.py`):
- the confidence is the best score over the sentence's cited passages;
- every model call goes through `asyncio.to_thread`;
- any scorer error gives `confidence: null`, never a failed answer.

**In `/v1/ask`:**
- every `sentence` event carries `confidence`, in streaming and in buffered mode; it is null on a cache hit, when the collection has `l4_enabled = false`, or when no model is installed;
- the flight trace records `l4_confidences`.

**Per-collection switch:** `l4_enabled`, added in migration 005, default 1; pre-005 rows read as on.

**Settings and start-up:**
- `[verifier]` settings set the model directory, model file and thread count;
- `compose.py` loads the scorer once at start-up in a worker thread;
- missing or damaged model files leave L4 off with a start-up note.

**Scripts:** `scripts/download_l4_model.py` (pinned revision) and `scripts/run_l4_calibration.py`.

## 3. Files Created

| Path | Purpose |
| :-- | :-- |
| `src/relay/experiments/l4_calibration.py` | Calibration rows (score, L2, L3, label) and threshold table. |
| `scripts/run_l4_calibration.py` | Writes `reports/v2-07-l4-calibration.md`. |
| `scripts/download_l4_model.py` | Downloads the approved model at a pinned revision into `data/models/nli/` (gitignored). |
| `reports/v2-07-l4-calibration.md` | The calibration table, plus the exploratory window comparison. |
| `reports/v2-07-l4-labels.json` | 207 proposed per-sentence labels, keyed on sentence + cited chunk ids, each with a reason. |
| `tests/test_v2_07_l3.py`, `tests/test_v2_07_l4.py` | 46 L3 tests and 19 L4 tests (65 new in total). |
| `scratch/v2_07_l4_*.py` (gitignored) | The spike, the quiet latency re-measure and the exploratory windows. |

## 4. Files Modified

- **Orchestration and API:**
  - `src/relay/orchestrator/verifier.py`: L3, plus the L4 scorer.
  - `src/relay/orchestrator/streaming.py`: L3 hold and stop, L4 confidence.
  - `src/relay/api/app.py`: `RelayService.entailment`.
  - `src/relay/api/routes/ask_v1.py`: scorer plumbing, `confidence`, `l4_confidences`, and the cancel-record usage fix.
- **Configuration and store:**
  - `src/relay/app/compose.py`: `_open_l4`.
  - `src/relay/app/settings.py` and `relay.example.toml`: `[verifier]`.
  - `src/relay/store/migrations.py` and `repositories.py`: migration 005, `l4_enabled`.
  - `src/relay/app/routes/ingestion.py`: `l4_enabled` on create and list.
- **UI:**
  - `web/src/answer.ts` and its test: the stop wording.
  - `src/relay/app/static/`: rebuilt.
- **Tests and docs:**
  - `tests/test_v1_regression.py`: exactly the refusal-shaped rows may now change, and only as decided.
  - `docs/DECISIONS.md`: the four owner decisions.

## 5. Technical Decisions

1. **Corrections to the brief:**
   - **The q02 premise doesn't hold.** The brief names q02 small_fast as the known gap, but that recorded answer has no refusal-shaped sentence, and v1 BLOCKED it for ungrounded numbers. The plan's case, v1 qwen **q21 balanced_rerank** ("The provided passages do not state how Wav LM performs…"), is used instead. Test 3 checks that q21 is now classified as `refusal_as_claim`, and that q02 is unaffected.
   - **The phrase list is broadened and narrowed.** It now also matches the real phrasings ("The **provided** passages do not…", "None of the passages mention…", "There is no information in the passages…"). Every branch has to name the retrieved sources, so genuine claims ("no information is provided to the decoder", "cannot be determined from context in advance") are not caught. Both lists are covered by tests.
2. **L4 model choice:** MiniLM2, int8 AVX2 file, 8 threads per call. It is the only configuration measured under 150 ms (§8). deberta-v3-xsmall was slower at every setting measured.
3. **L4 scoring rule:** best entailment over the cited passages, with the whole passage as premise. That is exactly the brief's `score(sentence, passage)`.
4. **L4 runs before each sentence is released,** so the `sentence` event can carry it. That adds the scoring time per sentence; it stays advisory.
5. **Calibration labels** are keyed on sentence + cited chunk ids, because a tag number points to different chunks in different settings.

## 6. Commands Actually Executed

- `python -m pytest -q`: after L3, after L3's review fixes, after L4, and at the end.
- `python -m ruff check .`
- In `web/`: `npx jest`, `npx eslint .` and `npx tsc --noEmit`; then `relay ui build`.
- `python scripts/replay_table3.py`, buffered and `--streaming`: after L3 and at the end.
- `python scratch/v2_07_l4_spike.py`: downloads and a first timing, made under CPU contention and discarded.
- `python scratch/v2_07_l4_latency.py`: the quiet re-measure.
- `python scripts/run_l4_calibration.py`
- `python scratch/v2_07_l4_windows.py`: run twice; the second run uses the corrected labels.
- Public Hugging Face metadata lookups and the model downloads; no project data was sent.

## 7. Test Results

These are the final runs, after the last edit:

| Suite | Result |
| :-- | :-- |
| Backend `pytest -q` | **822 passed** (757 before this phase, plus 65 new) |
| `ruff check .` | All checks passed |
| UI: `npx jest`, `eslint`, `tsc` | 41 passed (40 + 1 new); 0 problems |
| `results.csv` sha256 | `8dfb10df…44efd`, unchanged; `git status evidence results.csv` empty |

**The 10 requested tests:**

1. Each refusal pattern, in 25 phrasings, is `refusal_as_claim`.
2. Claims, including negative claims and the near-misses from the review, are not refusals (13 cases).
3. q21 is classified; q02 is unaffected.
4. A single-sentence refusal is a verified refusal and is cached.
5. A mixed answer stops at the refusal-shaped sentence and keeps a partial.
6. `entailment_probability` stays a probability: a seeded property test (500 cases), plus a real-model test with 34 random inputs and the label-order check.
7. Scoring doesn't block the event loop: 10 concurrent calls, each under 3× a single call, with heartbeat gaps under 50 ms.
8. With `l4_enabled = false`, the scorer is not called and confidence is null.
9. `sentence` events carry `confidence`, in streaming and buffered mode.
10. The calibration run covers all 21 questions; q18 has no sentence, because it was refused before any model call.

## 8. Actual Output and Generated Artifacts

### L4 latency and memory

Development machine: 14 physical / 20 logical cores, no AVX-512. Measured on a quiet machine with 200 real (cited passage, sentence) pairs from Table 3, a median of about 330 tokens. Times are per pair, one call at a time.

| Model and file | Threads | p50 | p95 | Memory increase |
| :-- | --: | --: | --: | --: |
| **MiniLM2-L6-H768, int8 AVX2 (chosen)** | 8 | **142.3 ms** | **237.5 ms** | ~90 MB |
| MiniLM2-L6-H768, int8 AVX2 | 4 / 1 | 192.2 / 380.8 ms | 406.5 / 2,198 ms | ~91 MB |
| MiniLM2-L6-H768, full precision | 8 / 4 / 1 | 152.6 / 272.3 / 1,490 ms | 257.8 / 447.3 / 2,784 ms | ~322 MB |
| deberta-v3-xsmall, full / int8 | 4 | 235.3 / 233.0 ms | 492.7 / 476.1 ms | 392 / 200 MB |

**The 150 ms target is met only per (sentence, passage) pair.** A sentence that cites k passages costs about k × 142 ms.

### Calibration (`reports/v2-07-l4-calibration.md`; labels proposed, unreviewed)

The table covers the 224 sentences that pass L1–L3 (26 not fully supported; 4 strictly not supported). The integrated whole-passage L4 does not discriminate:

| Threshold | Blocked | TPR, not fully supported | FPR, not fully supported | TPR, strict | FPR, strict |
| --: | --: | --: | --: | --: | --: |
| 0.3 | 188 | 92.3 % (24/26) | 82.8 % (164/198) | 100 % (4/4) | 83.6 % (184/220) |
| 0.5 | 207 | 96.2 % (25/26) | 91.9 % (182/198) | 100 % (4/4) | 92.3 % (203/220) |
| 0.7 | 213 | 96.2 % (25/26) | 94.9 % (188/198) | 100 % (4/4) | 95.0 % (209/220) |
| 0.9 | 221 | 100 % (26/26) | 98.5 % (195/198) | 100 % (4/4) | 98.6 % (217/220) |

Median scores are 0.045 for supported, 0.044 for partially supported and 0.038 for not supported.

**Exploratory, not integrated:** scoring against 2-sentence windows of the passage gives AUROC **0.756** (whole passage: 0.530). Supported sentences then score a median of 0.895, against 0.090 for unsupported ones. At threshold 0.3 that blocks 61.5 % of the not-fully-supported sentences, with a 12.6 % false-positive rate. The cost is about 3× the model calls per sentence (0.40 s sequential).

### Regression check: the 84-replay against v1

| Replay | Matches v1 | Changed rows |
| :-- | :-- | :-- |
| Buffered, v1 path, 63 rows | 58 | q10 small_fast; q13 small_fast, balanced_rerank and large_wide; q15 balanced_rerank |
| Buffered, `v1-papers` path, 21 rows | 19 | q10 small_fast, q13 small_fast |
| **Total** | **77 of 84** | 7 rows from 5 answers, all VERIFIED → BLOCKED |
| Streaming, 63 rows | 58 agree | q10 small (1 sentence released, then `refusal_as_claim`); q13 ×3 (3, 4 and 4 released, then `refusal_as_claim`); q15 balanced (0 released, `refusal_with_content`) |

The partial-answer rate on the streaming replay is now 5 of 55 (9.1 %); it was 1 of 55 before L3.

### Why each changed verdict is correct

Every changed answer contains a sentence asserting that the passages lack something, which v1 delivered as a verified claim. **All five were rubric-graded "wrong" in v1**: rubric-proposed, missing 3–4 required facts each.

| Answer | The refusal-shaped sentence v1 verified | v1 rubric | New verdict |
| :-- | :-- | :-- | :-- |
| q10 small_fast | "The provided passages do not report a specific practical latency figure for Moshi [P1][P2][P3][P4]." | wrong (4 facts missing) | Buffered: BLOCKED (safe refusal). Streaming: 1 verified sentence kept, then stopped. |
| q13 small_fast | "The provided passages do not state how the routing module's boundary information differs …" | wrong (3 missing) | BLOCKED; streaming keeps 3 sentences |
| q13 balanced_rerank | "The provided passages do not contain information comparing …" | wrong (4 missing) | BLOCKED; streaming keeps 4 |
| q13 large_wide | "The passages do not provide a direct comparison …" | wrong (4 missing) | BLOCKED; streaming keeps 4 |
| q15 balanced_rerank | "The passages do not explicitly state how increasing the number of hierarchical stages affects …" (the first sentence, followed by claims) | wrong (4 missing) | BLOCKED; streaming refuses, not cached |

No verdict changed for any other reason, and no VERIFIED answer without a refusal-shaped sentence changed. The tests assert this.

## 9. Errors and Root-Cause Analysis

| Error | Cause | Fix |
| :-- | :-- | :-- |
| First spike latencies were invalid (393 ms) | It ran alongside the test suite and the replay. | Re-measured on a quiet machine. |
| q21 test picked the wrong row | The model-comparison run has several models per question, and `load_rows` has no model field. | The row is selected by its recorded sentence. |
| Calibration test expected 21 questions | q18 has no model answer in any setting. | The test asserts 21 read and 20 with sentences. |
| Event-loop test failed once (64 ms gap) | Thread-pool start-up on first use, with the CPU contended. | The pool is warmed first; the threshold is now one scoring call (50 ms). |
| Shell heredoc quirks (an apostrophe; `\n`) | The tool's shell. | Patch scripts as files, and the Edit tool. |

## 10. Fixes Applied (code reviews)

**L3 review** (`pr-review-toolkit:code-reviewer`):
- **Important:** 5 false positives on genuine claims, and 13 missed common phrasings. The pattern set was rewritten and every case added to the tests; they failed first.
- **Minor, unchanged:** refusal prose plus the INSUFFICIENT_EVIDENCE marker is BLOCKED, not a cached refusal (§11).

**Full-phase review:**
- **Important, fixed with tests that failed first:**
  - Calibration labels were keyed on tag numbers. Re-keyed on chunk ids; 10 pairs were labelled anew (all supported).
  - A corrupt model crashed start-up. `_open_l4` now catches any error, so L4 stays off.
  - A buffered disconnect during scoring recorded no spend. The cancel record now uses the finished call's real usage.
- **My own, before the review result:** a scorer error killed the answer. Advisory scoring now never fails an answer (test first).
- **Minor:**
  - The "shown" wording is fixed.
  - Multi-passage latency, the 8 threads under load, `huggingface_hub` not declared in `pyproject`, and the timing-based test are deferred (§11).

## 11. Outstanding Issues: decisions for the owner

1. **The L4 method.** As integrated (whole passage) it does not discriminate; scoring against 2-sentence windows does much better (AUROC 0.756 against 0.530) but costs about 3× the model calls. Options:
   - **(a)** Switch to windows, with batching (one padded model call per sentence) to keep latency near the target. Recommended.
   - **(b)** Keep whole-passage scoring as an advisory display only.
   - **(c)** Try a long-premise NLI model.

   No threshold should be considered until this is decided and the labels are reviewed.
2. **Review the 207 proposed labels** in `reports/v2-07-l4-labels.json`. There are 4 not_supported and 21 partially_supported. Small positive counts make every rate uncertain.
3. **L4 latency per sentence:** k cited passages cost k × ~142 ms, before the sentence is released. With 8 ONNX threads per call, concurrent requests oversubscribe the CPU. Batching and a lower thread count or a semaphore would fix both, and should be decided together with item 1.
4. **L3 plus the marker:** "The passages do not state X. INSUFFICIENT_EVIDENCE" is BLOCKED, not cached. Both paths agree and the user still gets a refusal. Keep this, or treat it as a pure refusal?
5. **The download script relies on `huggingface_hub`,** which fastembed installs but `pyproject` does not declare.
6. **Cached answers carry no L4 confidence** (null).

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| The known gap is classified as `refusal_as_claim` | **Met for the plan's q21 case.** The brief's q02 has no such sentence (§5). |
| L4 integrated in advisory mode; every verified sentence gets a confidence | **Met** (null when off, uninstalled or cached) |
| `reports/v2-07-l4-calibration.md` exists | **Met.** It shows no usable threshold for the integrated method. |
| 84-replay passes, or each changed verdict explained | **Met.** 77 of 84 match; the 7 changes from 5 answers are all intended and explained. |
| L4 CPU median latency measured and reported | **Met.** 142.3 ms per pair. |
| All 757 existing tests pass | **Met.** Two v1-regression tests now assert the intended changes. |
| All 10 new tests pass | **Met.** 65 new tests in total. |
| ruff clean; evidence unchanged; 0 real calls | **Met** |

## 13. Remaining Risks

- **Labels:** they are Claude-proposed and unreviewed, with small positive counts.
- **Latency:** measured on one laptop, and noisy at low thread counts because of its mix of performance and efficiency cores.
- **AVX2:** the int8 AVX2 model needs an AVX2 CPU; use `onnx/model.onnx` otherwise.
- **L3 breadth:** the patterns are wider now, so a new genuine claim could still match. The tests cover the known near-misses.

## 14. Recommended Next Phase

Your decisions on §11 items 1 and 2 come first. V2-08 (conversations) does not depend on them. Nothing has been started.
