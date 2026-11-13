# Phase 07 Implementation Report — Model Comparison and Retrieval-Quality Evaluation

> **Phase ID:** `p07`
> **Date:** `2026-11-13 12:38 IST` (part A, steps 1–4: 12:20 IST; part B, steps 5–6: 12:38 IST)
> **Status:** `Completed`, with one Table 3 request failed (62 of 63 succeeded; see §9.6). **Owner grade review is pending**, so no row is graded yet.
> - Verified in Conda env `the-relay` (Python 3.12.16): 451 passed, 0 failed, 0 skipped; ruff clean.
> - **Real Groq calls: 76 in total** (comparison 18 with cap 24; Table 3 58 with cap 70).
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P07, `ARCHITECTURE.md` §10–§11 and the two phase prompts:

1. Build the evaluation runner and scripts: cache off, sequential, paced by ProviderGuard, with `--source`, `--max-real-calls` and the daily safety fraction 0.75.
2. Test them offline.
3. Run the live model comparison (6 questions × 3 models, `balanced_rerank`).
4. After the owner's model selection, which was **`qwen/qwen3.8-27b`**:
   - raise `MAX_REAL_CALLS` to at least 85;
   - run Table 3 live (21 questions × `small_fast`, `balanced_rerank`, `large_wide`, cap 70);
   - produce proposed grades;
   - summarise by preset.

## 2. Implementation Summary

- **`experiments/eval_runner.py`**
  - **Isolation.** `EvalRunner` calls the P05 pipeline in-process, one request at a time. There is no HTTP service, cache, SingleFlight or admission queue, so every row has `cache_status=off`.
  - **Per-model lanes.** Each model has its own `Pipeline` + `GuardedGenerator` + `ProviderGuard`. Guard limits:
    - TPM 8000 and RPD 1000 (P02-LIVE headers);
    - an RPM pacing cap of 10, **assumed**;
    - TPD unknown;
    - concurrency 1;
    - daily fraction 0.75.
  - **No degradation.** There is no prompt budget, so passages are never dropped; the guard waits for TPM quota instead.
  - **Per request:** a `ResultRow` with `answer_grade` blank until the owner reviews, and a full trace.
  - **Per run:** `answers.jsonl`, `grades.json` (P04 rubric, `pending`), `answers_audit.json` and `summary.json`.
  - **Stop rules:** the call cap, the daily stop, `circuit_open`, `daily_budget_exhausted` and auth errors.
- **`experiments/eval_cli.py`** is the shared driver for both scripts.
  - Dry run by default for `--source real`. A live run needs `--live`, a cap ≥ planned first attempts and ≤ `MAX_REAL_CALLS`, and a key.
  - Real calls are counted into the manifest.
  - Guard limits are recorded with their basis.
- **No code changed in part B.** Part B used the part A tooling unchanged.

## 3. Files Created

- `src/relay/experiments/__init__.py`, `eval_runner.py`, `eval_cli.py`. `eval_cli.py` was not named in the plan; it holds the CLI code shared by the two scripts.
- `scripts/run_model_comparison.py`, `scripts/run_retrieval_eval.py`.
- `tests/test_eval_runner.py` — 16 tests.
- `evidence/experiments/exp-20261113T063331Z-e17e9c1/` — model comparison (committed by the owner in `c482696`).
- `evidence/experiments/exp-20261113T064542Z-c482696/` — **Table 3**: `manifest.json`, `rows.csv`, `traces.jsonl`, `answers.jsonl`, `grades.json`, `answers_audit.json`, `summary.json`.
- `reports/phase-07-model-and-retrieval-eval.md` — this report.

## 4. Files Modified

- **`.env`** (gitignored, not tracked): only the `MAX_REAL_CALLS` line changed, `40 → 85`, at the owner's instruction. It was edited in place by a script that printed only the old and new number. Confirmed afterwards through `load_settings`, which printed `MAX_REAL_CALLS 85`, key configured True, fraction 0.75. `.env` is still gitignored.
- `ENGINEERING_LOG.md` — two appended entries (part A, part B).
- No earlier-phase module, test, evidence file or `eval/questions.json` was changed.

## 5. Technical Decisions

All items below are **[PROPOSED]** until the owner accepts them.

1. **Comparison subset, chosen by a stated rule:** q03, q07, q10, q12, q13, q21. This is selection only.
2. **One guard per model, interleaved order, no prompt budget.**
3. **RPM pacing cap of 10 per model**, assumed.
4. **`first_token_ms` ≈ `total_ms`**, because answers are buffered and verified.
5. **TTFT excludes the guard's quota wait.** Part B also reports model-only TTFT, which additionally excludes retrieval.
6. **Rows keep `answer_grade` blank** until the owner reviews.
7. **Model ID correction in part B.** The prompt's command used `--model qwen3.8-27b`. The selected model, and the ID Groq listed in P02-LIVE discovery, is `qwen/qwen3.8-27b`; the bare form appears 0 times in the discovery evidence. The run used `qwen/qwen3.8-27b`. Every other argument was used exactly as given.

## 6. Commands Actually Executed

Part A (steps 1–4):

```bash
python -c "load_settings(): non-secret values only" ; git check-ignore -v .env
timeout 300 python -m pytest tests/test_eval_runner.py -q   # iterations, §9.1-9.5
timeout 600 python scratchpad/p07_standin_smoke.py           # offline, scratch/ only
timeout 120 python scripts/run_model_comparison.py --source real --max-real-calls 24          # dry run
timeout 1500 python scripts/run_model_comparison.py --source real --live --max-real-calls 24  # LIVE, once
```

Part B (steps 5–6):

```bash
python (in-place edit of the MAX_REAL_CALLS line in .env; prints "MAX_REAL_CALLS: 40 -> 85" only)
python -c "load_settings(): key_configured True | MAX_REAL_CALLS 85 | daily_safety_fraction 0.75"
git check-ignore -q .env
grep -o '"qwen/qwen3.8-27b"' / grep -c '"qwen3.8-27b"' evidence/groq_probes/probe-20261112T031108Z-2b5d017/traces.jsonl
timeout 120 python scripts/run_retrieval_eval.py --model qwen/qwen3.8-27b --source real --max-real-calls 70 \
    --settings small_fast,balanced_rerank,large_wide                                 # dry run
timeout 3600 python scripts/run_retrieval_eval.py --model qwen/qwen3.8-27b --source real --live \
    --max-real-calls 70 --settings small_fast,balanced_rerank,large_wide             # LIVE, once
python scratchpad/p07_table3_analysis.py evidence/experiments/exp-20261113T064542Z-c482696   # read-only
python (read-only: failed/retried traces; reason classification of proposed grades)
python scripts/grade_answers.py propose --run-id exp-20261113T064542Z-c482696 \
    --answers <run>/answers.jsonl --out scratch/p07_regrade_grades.json ; cmp <run>/grades.json scratch/...
python scripts/grade_answers.py summary --grades <run>/grades.json
grep -c "gsk_" / grep -l -i "bearer|authorization" / grep -o "org_..." over the run dir
timeout 900 python -m pytest -q ; python -m ruff check .                                     # final
```

`python` is `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`, with `HF_HUB_OFFLINE=1` set.

## 7. Test Results

- **Final**, run after part B, where no code changed: `python -m pytest -q` → **451 passed, 0 failed, 0 skipped** (14.35 s); `python -m ruff check .` → **All checks passed!**
- **Part A** added 16 tests. They cover:
  - row format, spans and traces;
  - all artifacts;
  - rubric mechanics on a synthetic question set;
  - every stop rule;
  - no-degradation lanes;
  - dry-run and refusal paths;
  - real-call counting;
  - no key leakage.
- **Offline stand-in smoke:** 18 rows, 0 real calls.
- **Grader reproduction:** `grade_answers.py propose` regenerated the Table 3 `grades.json` **byte for byte** ("IDENTICAL").

## 8. Actual Output and Generated Artifacts

### 8.1 Model comparison (part A): `exp-20261113T063331Z-e17e9c1`

18 real calls, all `ok`, 0 retries. Grades are proposed, pending review.

| Model | TTFT excl. wait p50 / max (ms) | tokens in / out (mean) | truncated at 512 | citation pass | proposed correct (n=5) | q21 |
| :-- | :-- | :-- | -: | -: | -: | :-- |
| gpt-oss-20b | 2350.6 / 2576.4 | 1730.8 / 397.0 | 3 of 6 | 0.4 | 0 | refused correctly |
| gpt-oss-120b | 2567.4 / 2791.0 | 1730.8 / 395.5 | 3 of 6 | 0.4 | 0 | refused correctly |
| qwen3.8-27b | 2217.4 / 3277.0 | 1742.7 / 97.0 | 0 of 6 | 1.0 | 1 | answered (wrong) |

The gpt-oss models spent all 512 completion tokens on reasoning for q07, q12 and q13, producing empty content and therefore safe refusals. **The owner selected `qwen/qwen3.8-27b`.**

### 8.2 Table 3 (part B): `exp-20261113T064542Z-c482696`

**Run facts**
- Manifest: `source` real, `cache_mode` off, `model_id` `qwen/qwen3.8-27b`, `status` complete, `real_call_count` **58**.
- Notes: "requests 63 of 63; stopped: no; lane stops: none".
- **63 rows**, unique request IDs, all `cache_status=off`, 0 degraded.
- **62 `ok` + 1 `error`** (§9.6).
- Provider called on 56 rows. 55 have provider-settled usage (`usage_source=provider`); the failed row has `none`, because no response was received.
- Retries: 2, both on the failed row. 55 successful calls + 3 failed attempts = 58 real calls.
- Tokens: 113,422 in and 6,581 out. That equals the guard's day total of 120,003.
- 63 traces (all `eval_request`), 62 answers, 62 grades (all `pending`), 63 audit entries.
- Secret scan: 0 `gsk_`, no bearer/authorization, no `org_` IDs.

**By retrieval preset.** n = 21 each: 15 answerable, 6 unanswerable (q16–q21). Grades are **proposed by the rubric, pending owner review**.

| Preset | provider calls | verified / blocked / LLM refusal / early refusal | citation pass | proposed correct (answerable) | unanswerable refused correctly | TTFT excl. wait p50 / p90 / max (ms) | model-only TTFT p50 / p90 / max (ms) | retrieval p50 / max (ms) | tokens in / out (mean) |
| :-- | -: | :-- | -: | -: | -: | :-- | :-- | :-- | :-- |
| small_fast | 20 | 14 / 1 / 5 / 1 | 14/15 = 0.933 | 6 / 15 = 0.40 | **6 / 6** | 495.4 / 1035.1 / 1613.2 | 450.2 / 1022.1 / 1568.3 | 35.0 / 68.0 | 1406.7 / 106.2 |
| balanced_rerank | 16 | 15 / 0 / 1 / 5 | 15/15 = 1.0 | 7 / 15 = 0.467 | **6 / 6** | 1924.5 / 3056.1 / 3058.2 | 439.8 / 786.7 / 1989.0 | 1260.2 / 2560.1 | 1686.2 / 130.1 |
| large_wide | 20 | 15 / 0 / 4 / 1 | 15/15 = 1.0 | 6 / 15 = 0.40 | **5 / 6** (q21 failed, ungraded) | 683.2 / 1003.4 / 1016.8 | 648.8 / 956.8 / 972.0 | 32.6 / 76.6 | 2915.5 / 118.8 |

TTFT sample sizes are n = 20 / 16 / 19 rows that reached the model. With n ≤ 20, p90 is a nearest-rank value; p95/p99 are not reported because they would equal the maximum.

**What the table shows**
1. **Refusal accuracy is 17 of 17 graded unanswerable rows.**
   - With `balanced_rerank`, q16–q20 were refused **before any model call** (rerank score below −5.0). That is why it made only 16 calls.
   - With the two non-rerank presets, the model itself refused q16, q17, q19 and q20 with the exact marker. q18 was refused early in all three presets.
   - q21 (bibliography trap) was refused correctly by the model under `small_fast` and `balanced_rerank`. Under `large_wide` it failed (§9.6).
2. **Citation integrity is 44 of 45 LLM answers verified.** The one block is q02 under `small_fast`: the answer contained a `0` (from a formula) that was not in the cited passages, so it became a safe refusal (`refused_wrongly`).
3. **Proposed correctness is 19 of 45 answerable.** 41 of 44 delivered answers cite a gold page. Of the 25 proposed `wrong`, **22 are flagged only for a missing required fact** under literal matching and 3 also lack a gold-page citation.
   - Several look like wording mismatches for owner review. For example, q05 under `large_wide` says "no recurrence or convolution" against the fact "no recurrence and no convolution".
   - Questions proposed wrong in all three presets: q07, q08, q09, q12, q13 and q15.
   - These are proposals, not quality figures.
4. **Latency.**
   - Model-only TTFT is similar across presets: p50 0.44–0.65 s.
   - `balanced_rerank`'s user-facing TTFT is about 1.9 s at p50 because reranking adds 1.26 s at p50.
   - `large_wide` doubles prompt tokens (2,915 vs 1,407 mean), and its TPM quota waits reached 59.2 s, so its total time with waits had a p50 of 57.0 s under single-model sequential pacing. That is a pacing artefact of TPM 8000, not model latency.

## 9. Errors and Root-Cause Analysis

Part A:

1. Test bug: `read_rows` takes a `Path`.
2. **Real CLI bug:** a dry run returned exit 2 instead of 0.
3. Over-strict assertions: the CSV round trip rounds `start_time`; the first byte is stamped about 1 µs before the timer finishes.
4. TTFT included the guard's quota wait (seen in the stand-in smoke).
5. A failed edit-script assertion; nothing was written.

Part B:

6. **One Table 3 request failed:** large_wide × q21 (`…-000063`, the last request).
   - All 3 attempts (1 + 2 retries) failed with `ProviderConnectionError`, a transport failure before any HTTP response.
   - It was not a 429 and not a daily limit: the guard showed `daily_stop` none, circuit closed and full TPM headroom.
   - 0 tokens were spent and `usage_source=none`. The pipeline classified it `status=error`, `error_code=upstream_error`.
   - Traces store only the sanitised type name, so the underlying network cause (DNS, TCP or TLS) is **not recorded and not known**.
7. **Model ID typo in the given command** (§5.7). It was prevented before any call.

## 10. Fixes Applied

1–5 (part A): fixed and tested (as in the part A entry); the final suite passed 451.

6. **Not re-run**, following the run-once practice for live calls. The row stays in the evidence as an honest `error`. A targeted re-run (`--questions q21 --settings large_wide`, at most 3 calls, as a new run directory) needs owner approval.
7. Used `qwen/qwen3.8-27b`, confirmed against the discovery evidence and by the dry run before going live.

## 11. Outstanding Issues

1. **Owner grade review** of 80 proposed grades: 18 in the comparison run and 62 in Table 3. Use `grade_answers.py review`. Until then, every correctness figure above is a literal-matching lower bound.
2. **The large_wide × q21 cell is missing** (§9.6). It can be re-run with owner approval, or reported as a failed request.
3. **Verifier gap** (part A, q21 under qwen in the comparison): a refusal written as a cited sentence passed as `VERIFIED`. In Table 3, qwen used the exact marker on every refusal, so the gap did not recur, but it remains in P05.
4. **Final grades do not yet flow into rows.** Aggregation does not read `grades.json`; this needs a decision for P11.
5. **Day-2 real calls: 76** (18 + 58), against the *proposed* ≤ 80 envelope. Both caps were set by the owner.
6. **TPD is unknown.** This model used 120,003 tokens in Table 3 with no daily 429.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| Runner + scripts; cache off, sequential, ProviderGuard pacing, `--source`, `--max-real-calls`, fraction 0.75 | Met |
| Offline test against ScriptedProvider / stand-in | Met |
| Live model comparison with comparison table | Met (18 calls) |
| Owner model selection | Met: `qwen/qwen3.8-27b` (owner, 2026-11-13) |
| `MAX_REAL_CALLS` ≥ 85 | Met (85) |
| Table 3: 21 × 3 into `evidence/experiments/<run_id>/` | Met: 63 rows recorded. **62 succeeded, 1 failed** (transport) |
| Tokens provider-settled | Met for all 55 calls that received a response; the failed call had no response |
| Traces and `answers.jsonl` generated | Met (63 traces, 62 answers) |
| Proposed grades in `grades.json` and `answers_audit.json` | Met (62 pending; reproduced byte for byte) |
| Breakdown by preset: citation rate, refusal accuracy, TTFT percentiles | Met (§8.2) |
| Real-run counts recorded | Met (18 and 58 in the manifests) |
| Owner grade review stored before rows become canonical | **Pending owner** |
| pytest and ruff after the final edit | Met: 451 passed; ruff clean |

## 13. Remaining Risks

- **Correctness figures understate quality** until the owner reviews the grades (literal fact matching).
- **Small samples:** n = 15 answerable questions per preset, so the differences between presets (6–7 correct) are within noise.
- **TTFT mixes retrieval and model time.** Both are reported; the write-up must keep them apart.
- **Transport failures are not attributable** from sanitised traces. If they recur, a sanitised error-category field (no message text) could be added, which is an owner decision.

## 14. Recommended Next Phase

Owner actions first:
1. Review the 80 proposed grades.
2. Decide whether to re-run large_wide × q21 (at most 3 calls).
3. Decide the verifier gap (§11.3) and how reviewed grades enter canonical rows (§11.4).

Then **P10 — Load Harness and Scenarios**, against the stand-in with `cal-20261113T022403Z-3f687c6/profile.json` and the owner-chosen scenario values (open since P09). Nothing has been started.
