# Phase 07 Implementation Report — Model Comparison and Retrieval-Quality Evaluation

> **Phase ID:** `p07`
> **Date:** `2026-11-13 12:20 IST`
> **Status:** `Partial`.
> - Steps 1–4 are done: tooling, offline tests, and the live model comparison.
> - **Step 5 (Table 3) and the grading of Table 3 wait for the owner's model selection**, as the phase prompt requires.
> - Verified in Conda env `the-relay` (Python 3.12.16): 451 passed, 0 failed, 0 skipped; ruff clean.
> - **Real Groq calls: 18** (cap 24).
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P07, `ARCHITECTURE.md` §10–§11 and the phase prompt:

1. Build the evaluation runner and the two scripts. Runs must be cache-off, sequential and paced by ProviderGuard, with `--source real|stand-in`, `--max-real-calls` and the daily safety fraction 0.75.
2. Verify them offline against `ScriptedProvider`.
3. Run the live model comparison: about 6 owner questions × the candidate models on `balanced_rerank`.
4. **After the owner selects a model**, run Table 3: 21 questions × 3 settings.
5. Run the rubric grader to produce proposed grades.

## 2. Implementation Summary

- **`experiments/eval_runner.py`**
  - **Isolation.** `EvalRunner` calls the P05 pipeline in-process, one request at a time. There is no HTTP service, cache, SingleFlight or admission queue, so every row has `cache_status=off`.
  - **Per-model lanes.** Each model has its own `Pipeline` + `GuardedGenerator` + `ProviderGuard`, because Groq's limits are per model. Guard limits:
    - TPM 8000 and RPD 1000 (P02-LIVE headers);
    - an RPM pacing cap of 10, **assumed**;
    - TPD unknown;
    - concurrency 1;
    - daily fraction 0.75.
  - **No degradation.** The pipeline gets **no prompt budget**, so passages are never dropped. The guard waits for TPM quota instead.
  - **Order.** Items are question-major with models innermost, so each model's TPM bucket recovers between its calls.
  - **Per request:** a `ResultRow` with `answer_grade` blank until the owner reviews, and a trace with the full pipeline outcome, passages and guard state.
  - **Per run:** `answers.jsonl`, `grades.json` (P04 rubric, all `pending`), `answers_audit.json` and `summary.json`.
  - **Stop rules:** the call cap (before a request, or during a retry, which is traced rather than rowed); the guard's daily stop and `circuit_open` / `daily_budget_exhausted` (that model only); an auth error (whole run).
- **`experiments/eval_cli.py`** is the shared driver for `scripts/run_model_comparison.py` and `scripts/run_retrieval_eval.py`.
  - Dry run by default for `--source real`. A live run needs `--live`, `--max-real-calls` ≥ planned first attempts and ≤ `MAX_REAL_CALLS`, and a configured key.
  - Real calls are counted into the manifest through `RealCallBudget`.
  - The manifest records the guard limits with their basis, the question IDs, the SHA-256 of the question file, the corpus digest, the prompt version and the retry config.

## 3. Files Created

- `src/relay/experiments/__init__.py`, `src/relay/experiments/eval_runner.py`, `src/relay/experiments/eval_cli.py`. `eval_cli.py` was not named in the plan; it holds the CLI code shared by the two scripts.
- `scripts/run_model_comparison.py`, `scripts/run_retrieval_eval.py`.
- `tests/test_eval_runner.py` — 16 tests.
- `evidence/experiments/exp-20261113T063331Z-e17e9c1/` — the live model comparison (`manifest.json`, `rows.csv`, `traces.jsonl`, `answers.jsonl`, `grades.json`, `answers_audit.json`, `summary.json`).
- `reports/phase-07-model-and-retrieval-eval.md` — this report.

## 4. Files Modified

- `ENGINEERING_LOG.md` — one appended entry.
- No earlier-phase module, test, evidence file or `eval/questions.json` was changed.

## 5. Technical Decisions

All items below are **[PROPOSED]** until the owner accepts them.

1. **Comparison question subset, chosen by a stated rule.** This is selection only; no question text was changed. The set is q03, q07, q10, q12, q13 and q21. q16–q20 are refused before any model call, so they could not compare models; q21 does reach the model.
2. **Interleaved order** across models, with one guard per model.
3. **No prompt budget in evaluations**, so there are no degraded answers.
4. **RPM pacing cap of 10 per model**, local and assumed; RPM is not measured.
5. **`first_token_ms` ≈ `total_ms`**: answers are buffered and verified, and the runner is the client.
6. **TTFT in the comparison is reported without the guard's quota wait.** It still includes retrieval and prompt build, so retrieval is shown separately.
7. **Rows keep `answer_grade` blank.** Proposed grades live in `grades.json` until the owner reviews them. Aggregation does not read `grades.json` yet (§11.4).

## 6. Commands Actually Executed

```bash
git status --short ; git log --oneline -4 ; sed/grep over IMPLEMENTATION_PLAN.md, ARCHITECTURE.md §10-11,
  eval/grader.py, eval/questions.py, retrieval/*, providers/probing.py, aggregation.py
python -c "load_settings(): print booleans and non-secret values only"   # key configured, MAX_REAL_CALLS 40
git check-ignore -v .env
python -m ruff check ... ; timeout 300 python -m pytest tests/test_eval_runner.py -q   # iterations, see §9
timeout 900 python -m pytest -q ; python -m ruff check .                              # 451 passed
timeout 600 python scratchpad/p07_standin_smoke.py   # offline, real indexes + calibrated stand-in, scratch/ only
timeout 120 python scripts/run_model_comparison.py --source real --max-real-calls 24   # dry run
timeout 1500 python scripts/run_model_comparison.py --source real --live --max-real-calls 24   # LIVE, once
python (read-only audit and summary extraction) ; grep for gsk_/bearer/authorization/org_ in the run dir
timeout 900 python -m pytest -q ; python -m ruff check .                              # final
```

`python` is `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`, with `HF_HUB_OFFLINE=1` set.

## 7. Test Results

After the final code edit:

- `python -m pytest -q`: **451 passed, 0 failed, 0 skipped** (17.77 s). That is 435 before P07 plus 16 new.
- `python -m ruff check .`: **All checks passed!**

The offline tests cover:
- row format and spans;
- traces;
- `answers.jsonl`, `grades.json`, the audit and the summary;
- rubric proposals on a synthetic question set: correct, safe refusal → `refused_wrongly`, LLM refusal, early refusal → `refused_correctly`;
- every stop rule;
- the "no degradation, concurrency 1" lanes;
- dry run and safeguard refusals with no run created;
- real-call counting into the manifest;
- no key leakage into the manifest.

Offline smoke against the calibrated stand-in, using the real indexes and models in `scratch/`: 18 rows, 0 real calls, exit 0.

## 8. Actual Output and Generated Artifacts

**Live run `exp-20261113T063331Z-e17e9c1`**
- Manifest: `source` real, `cache_mode` off, `status` complete, `real_call_count` **18**, 0 retries.
- No stops; `not_run` is empty; all 18 rows `status=ok`.
- `git_dirty` true (P07 not yet committed).
- Every token count is provider-reported (`usage_source=provider`).

Comparison table. n = 6 requests per model; grade columns are **PROPOSED by the rubric, pending owner review**.

| Model | TTFT excl. wait p50 / max (ms) | tokens in / out (mean) | truncated at 512 | verified / blocked / LLM refusal | citation pass | proposed correct (answerable, n=5) | q21 (unanswerable) |
| :-- | :-- | :-- | -: | :-- | -: | -: | :-- |
| gpt-oss-20b | 2350.6 / 2576.4 | 1730.8 / 397.0 | **3 of 6** | 2 / 3 / 1 | 0.4 | 0 / 5 | refused correctly |
| gpt-oss-120b | 2567.4 / 2791.0 | 1730.8 / 395.5 | **3 of 6** | 2 / 3 / 1 | 0.4 | 0 / 5 | refused correctly |
| qwen3.8-27b | 2217.4 / 3277.0 | 1742.7 / 97.0 | 0 of 6 | 6 / 0 / 0 | 1.0 | 1 / 5 | **answered** (graded wrong) |

Notes, each read from the run's own files:

1. **The comparison is confounded by truncation.**
   - On q07, q12 and q13, both gpt-oss models spent the entire `max_completion_tokens=512` on reasoning. They returned `finish_reason=length` with **empty content**, so all six became safe refusals (`empty_answer`).
   - Mean reasoning was about 1,805 characters per gpt-oss call. qwen produced no reasoning, about 97 output tokens, and never truncated.
   - The 512 limit (P05 default) was not reached in P02-LIVE because those prompts were tiny; with RAG prompts it is.
2. **TTFT here includes retrieval.** `retrieval_ms` was 607–2,790 ms per request (hybrid + rerank). That is most of the 2.2–2.6 s "TTFT", so the model-only differences are small.
3. **Quota waiting worked.** gpt-oss-20b waited up to 35.6 s once for TPM headroom (the guard's header-synced headroom); there were no 429s. Final guard view: adapted chars/token 3.60 (gpt-oss) and 3.57 (qwen); 6 requests per model.
4. **Most "wrong" proposals are the rubric's literal matching and need owner review.** For example:
   - gpt-oss-20b on q03 says "8 attention heads … key and value dimensions of 64" and cites the gold p.5, but the required-fact alternatives (`8 heads|h = 8|…`, `= 64|dimension of 64|…`) did not match that wording;
   - several answers state the passages' latency figures (160 ms / 200 ms) without the expected `230ms` comparison fact.

   Owner review decides these; `grade_answers.py review` records each decision.
5. **Verifier gap found (q21, qwen).** qwen answered "The provided passages do not state how WavLM performs … [P1]". That is a refusal phrased as a cited sentence. The P05 verifier marked it `VERIFIED` (a tagged sentence with no ungrounded numbers), and the rubric proposes `wrong`. No change was made; this needs an owner decision (§11.2).
6. **Daily headroom.**
   - RPD bound: 750 answers per model per day (1000 × 0.75, header basis).
   - TPD bound: **not computable**, because TPD is not exposed and was not measured.
   - Tokens used in this run: gpt-oss-20b 12,767; gpt-oss-120b 12,758; qwen 11,038 (guard day totals).

## 9. Errors and Root-Cause Analysis

1. **Test bug:** `read_rows` takes a `Path`, but the test passed text.
2. **Real CLI bug:** a dry run (`--source real` without `--live`) also reported the cap and key problems, so it returned exit 2 instead of 0. Root cause: dry runs were treated like refused live runs.
3. **Over-strict test:** whole-row equality after the CSV round trip failed, because `start_time` is rounded to milliseconds. Likewise `first_token_ms == total_ms` failed, because the first byte is stamped about 1 µs before the timer finishes.
4. **Misleading TTFT (found in the stand-in smoke).** "TTFT max" was 45–52 s because `provider_first_token_ms` counts from request start and so included the guard's quota wait.
5. **Edit-script assertion failure:** a scripted replacement did not match a line that `sed` had already changed, so nothing was written. The edits were re-applied by hand.

## 10. Fixes Applied

1. The test passes a `Path`.
2. A dry run now prints "would refuse: …" and exits 0; a live run with a failed safeguard prints "refused: …" and exits 2. Asserted in the tests.
3. The test compares request IDs, and checks `0 ≤ total − first ≤ 0.05 ms`.
4. Added `provider_ttft_excl_wait_ms` and `quota_wait_ms` to the summary and the table, with the basis stated. Tests assert them.
5. Applied with the Edit tool after reading the current text.

Each fix was validated by the test runs above; the final full suite passed 451.

## 11. Outstanding Issues

1. **Model choice is open.** The truncation confound means the gpt-oss models were not compared fairly at 512 tokens. Options for the owner:
   - (a) choose `qwen/qwen3.8-27b` from this evidence (never truncated, all citations passed, about a quarter of gpt-oss's output tokens);
   - (b) re-run the comparison for the gpt-oss models with `--max-completion-tokens 1024` or `--reasoning-effort low`, costing about 12 more calls.
2. **Verifier gap on "refusal-like" cited sentences** (P05 module). Changing it is an architecture-level decision under CLAUDE.md §7.
3. **Rubric literal matching** under-credits paraphrased correct facts. The question content and fact alternatives are the owner's; I made no edits. The owner reviews every graded row.
4. **Final grades do not yet flow into rows.** Aggregation does not read `grades.json`; that step belongs with P11 and needs a decision on how reviewed grades enter canonical rows.
5. **Not run yet:** Table 3 (step 5) and its grading (step 6).
6. **Real-call budget for Table 3.** About 63 first attempts plus retry headroom. With 18 used today, Day 2 would total about 81+, over the *proposed* ≤ 80. The owner should set the cap, or split Table 3 across Days 2 and 3 as the plan allows. `MAX_REAL_CALLS` (40) must also be raised in `.env`, or Table 3 run in parts.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| Runner + both scripts; cache off, sequential, ProviderGuard pacing, `--source`, `--max-real-calls`, daily fraction 0.75 | Met |
| Offline test against ScriptedProvider / stand-in | Met (16 tests + stand-in smoke) |
| Live model comparison into `evidence/experiments/<run_id>/`, with the comparison table | Met (18 real calls, cap 24) |
| Real-run counts recorded | Met (`real_call_count` 18 in the manifest) |
| Owner model selection | **Pending owner** |
| Table 3 live run (21 × 3) | **Not started** (needs model selection and a cap) |
| Proposed grades in `grades.json` / `answers_audit.json` | Met for the comparison run; Table 3 pending |
| Owner grade review stored before rows become canonical | **Pending owner** |
| pytest and ruff after the final edit | Met: 451 passed; ruff clean |

## 13. Remaining Risks

- **TPD is unknown.** Table 3 on one model is about 63 calls × roughly 2,100 tokens ≈ 130k tokens. If the model's TPD is near that, a daily 429 would stop the run; the guard maps it to `daily_budget_exhausted` and stops that lane.
- **Rubric proposals understate quality.** Until the owner has reviewed them, any "correct rate" here is a lower bound from literal matching, not a quality figure.
- **TTFT figures mix retrieval and model time.** The write-up must report retrieval separately.

## 14. Recommended Next Phase

Continue **P07 step 5** after the owner decides:

1. the model (option a or b in §11.1);
2. the real-call cap for Table 3, and `MAX_REAL_CALLS`;
3. `max_completion_tokens` / `reasoning_effort` for Table 3;
4. the §5 items;
5. the verifier gap (§11.2);
6. the owner review of the 18 proposed grades.

Nothing further has been started.
