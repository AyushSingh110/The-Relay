# Phase 11 Implementation Report — Tables, Forensics and Submission Audit

> **Phase ID:** `p11`
> **Date:** `2026-11-13 14:40 IST`
> **Status:** `Completed`. `results.csv` is built and recomputable; the tables are computed; the audit passed.
> - **`answer_grade` is blank in all 375 rows, by owner decision**: all 80 grades are pending owner review, and `results.csv` will be rebuilt with `--replace` afterwards.
> - Verified in Conda env `the-relay` (Python 3.12.16): `pytest -v` 467 passed, 0 failed, 0 skipped; ruff clean.
> - **Real Groq calls: 0.**
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P11 and the phase prompt:

1. Implement `tables.py`, `compute_tables.py` and tests.
2. Populate `evidence/canonical_runs.json` with the approved runs, excluding superseded ones.
3. Build `results.csv` with `scripts/build_results.py`.
4. Compute Tables 1, 2a, 2b and 3 into `scratch/computed_tables.md`, with linear percentiles in ms, N stated, one-decimal %, and "not measured" when N = 0. Table 2b includes the slow-request forensics.
5. Audit:
   - a secret scan over tracked files and evidence;
   - `git check-ignore` confirming that evidence and `results.csv` are trackable;
   - `pytest -v` and ruff;
   - byte-identical recomputation.

## 2. Implementation Summary

- **Pre-flight conflicts, decided by the owner** through a question dialog before any file was written:
  1. **The calibration run is cited outside `canonical_runs.json`.** `build_results.py` rejects run kinds without rows. The tables header and this report cite `cal-20261113T022403Z-3f687c6`.
  2. **The P02-LIVE baseline probe run `probe-20261112T031816Z-2b5d017` is included**, as ARCH §13.2 (owner-decided) requires: 15 `probe_baseline` rows. The discovery probe run has 0 rows.
  3. **Build now with blank grades and rebuild after review.** All 80 grades were `pending`.
- **`experiments/tables.py`** recomputes everything from `results.csv`. Each run's `manifest.json` supplies labels, `summary.json` the harness-measured duration, capacity and provider attempts, `grades.json` the grades and `traces.jsonl` the forensics evidence.
  - **Percentiles:** linear interpolation (type 7), `p50 / p95 / p99 (N=n)`.
  - **Formatting:** `fmt_pct` gives one decimal with counts; "not measured" when N = 0.
  - **Grades:** the owner's reviewed grade is used first, otherwise the rubric's proposal, and every quality table says which.
  - **Tables:** a probe table, Table 1, 2a, 2b (latency plus slowest-request decomposition) and Table 3.
- **`scripts/compute_tables.py`** writes `scratch/computed_tables.md`. `--check` recomputes and compares bytes.

## 3. Files Created

- `src/relay/experiments/tables.py` — table computation and formatting.
- `scripts/compute_tables.py` — CLI with `--check`.
- `tests/test_tables.py` — 6 tests.
- `results.csv` (repository root) — 375 rows from 12 canonical runs; built only by `scripts/build_results.py`.
- `scratch/computed_tables.md` — gitignored output, as the plan specifies.
- `reports/phase-11-tables-and-submission-audit.md` — this report.

## 4. Files Modified

- `evidence/canonical_runs.json` — previously `{"schema_version": 1, "runs": []}`; now lists 12 runs, at the owner's instruction.
- `ENGINEERING_LOG.md` — one appended entry.
- No module, test or evidence run file from earlier phases was changed.

## 5. Technical Decisions

All items below are **[PROPOSED]** unless marked as owner decisions.

1. **Canonical runs** (owner decisions 2026-11-13):
   - `probe-20261112T031816Z-2b5d017` (probe baseline);
   - `exp-20261113T063331Z-e17e9c1` (model comparison);
   - `exp-20261113T064542Z-c482696` (Table 3);
   - load runs: steady `…074251Z`, burst cache on `…074759Z`, burst cache off `…074807Z`, overload `…074816Z`, tpm_halved at 8/min `…080532Z`, daily_exhausted `…075725Z`, retry budget on `…075917Z`, retry budget off `…080155Z`, real steady `…081218Z`.
   - **Excluded:** the superseded `…075226Z` (tpm_halved at 4/min) and the 0-row discovery run.
   - The calibration run `cal-20261113T022403Z-3f687c6` is cited, not listed.
2. **Linear percentiles in all tables**, per P11. P07 and P10 used nearest rank, so some values differ from those reports. For example, qwen's comparison TTFT p50 is 2,481.8 ms (linear) against 2,217.4 ms (nearest rank).
3. **TTFT in Tables 1 and 3 excludes the guard's quota wait**, and Table 3 adds a model-only TTFT that also excludes retrieval. Load tables use client first-token time.
4. **The forensics split is taken around the provider's first content token.** Answers are buffered and verified, so all server time precedes the client's first byte. The table reports before and after the provider's first token, plus client overhead (`total_ms − server_total_ms`).
5. **Unanswerable questions** are q16–q21, matching `eval/questions.json`, where `answerable=false` for exactly these IDs.

## 6. Commands Actually Executed

```bash
git status --short ; git log --oneline -3 ; sed -n '/^### P11/,/^---/p' IMPLEMENTATION_PLAN.md
cat evidence/canonical_runs.json ; sed -n 87,112p src/relay/telemetry/aggregation.py
wc -l evidence/groq_probes/*/rows.csv ; python -c "grade review status per run"
(AskUserQuestion: calibration run, probe run, grades -> owner chose the recommended option for each)
python -m ruff check src/relay/experiments/tables.py scripts/compute_tables.py
python (writes evidence/canonical_runs.json with the 12 runs; asserts 075226Z excluded)
python scripts/build_results.py --check          # checked, 375 rows, would_change true
python scripts/build_results.py                  # written
python scripts/build_results.py --check ; python scripts/build_results.py   # checked / unchanged
sha256sum results.csv                            # twice, identical
python scripts/compute_tables.py ; python scripts/compute_tables.py --check
timeout 300 python -m pytest tests/test_tables.py -q
python scratchpad/p11_secret_scan.py             # prints locations and classes, never values
git check-ignore -q <evidence files, results.csv, .env, scratch/, data/papers/*.pdf> ; git ls-files .env
sed (test lines with org IDs masked, for classification)
timeout 900 python -m pytest -v > scratch/p11_pytest_v.txt ; python -m ruff check .
python scripts/build_results.py --check ; python scripts/compute_tables.py --check ; sha256sum results.csv
```

## 7. Test Results

- **`python -m pytest -v`** (after the final edit): **467 passed, 0 failed, 0 errors, 0 skipped** (24.27 s). That is 461 before P11 plus 6 new.
- **`python -m ruff check .`:** All checks passed!
- **`tests/test_tables.py`** (6 tests):
  - the linear percentile definition;
  - formatting (N, one decimal, "not measured");
  - Table 3 preferring reviewed grades and labelling proposals;
  - Table 2a capacity and throughput and Table 2b forensics, with exact decomposition values;
  - determinism and the calibration citation;
  - the script's write and `--check` (identical, then tampered → exit 1).

## 8. Actual Output and Generated Artifacts

### 8.1 `results.csv`

- `build_results.py` → `{"outcome": "written", "rows": 375, "runs": 12}`.
- Re-check → `would_change: false`; rebuild → `unchanged`.
- SHA-256 `8dfb10dfaadb3f9dc0e9a753ebab0fba02e02938d3bbbf2791d98c08e4544efd` on both builds and after the final tests.
- **375 rows, 375 unique request IDs; 106 real and 269 stand-in.**
- Rows per scenario: `probe_baseline` 15, `model_comparison` 18, `retrieval_eval` 63, `steady` 19, `burst` 60, `overload` 86, `tpm_halved` 40, `daily_exhausted` 18, `retry_amplification` 46, `real_steady` 10.
- That is 15 + 18 + 63 + 279 load rows, where 279 = the 299 checked in P10 minus the 20 superseded rows.
- **`answer_grade` non-blank: 0**, by owner decision.

### 8.2 `scratch/computed_tables.md`

11,081 bytes, SHA-256 `b8e964b3c82ef8550241e9fd171e319cfaf52c72050d98593db687ad4a187f26`; `--check` reports identical. Excerpts follow; the full tables are in the file. Grades are **PROPOSED, pending owner review**.

**Table 1 (model comparison, `exp-20261113T063331Z-e17e9c1`)**
- gpt-oss-120b: TTFT p50/p95 2,567.4 / 2,768.6 ms (N=3); 2,126.3 tokens per answer; truncated 3/6; citation pass 40.0 % (2/5); correct 0.0 % (0/5).
- gpt-oss-20b: TTFT 2,350.6 / 2,553.9 (N=3); 2,127.8 tokens; truncated 3/6; citation 40.0 %; correct 0.0 %.
- qwen3.8-27b: TTFT 2,481.8 / 3,248.3 (N=6); 1,839.7 tokens; truncated 0/6; citation 100.0 % (6/6); correct 20.0 % (1/5); refused correctly 0.0 % (0/1).

**Table 2a (selected rows)**

| Run | Delivered | Shed | Given up | Provider calls | Throughput | Capacity |
| :-- | :-- | :-- | :-- | -: | :-- | :-- |
| burst, cache on | 96.7 % (29/30) | — | — | 6 | — | 3 |
| burst, cache off | 26.7 % (8/30) | 73.3 % rate-limited | — | 8 | — | 3 |
| overload | 67.4 % (58/86) | 22.1 % rate-limited | 10.5 % | 56 | 14.1 /min delivered vs 21.9 /min offered | 3 (derived 9.9 /min); queue peak 3 |
| daily_exhausted | 38.9 % (7/18) | 61.1 % rate-limited | — | 3 | — | — |

Retry amplification: provider attempts per first attempt were **1.27 with the budget on and 1.41 with it off**.

**Table 2b (latency and forensics)**
- Overload first token p50/p95/p99: 933.9 / 23,389.1 / 26,853.9 ms (N=58).
- Real steady first token: 855.3 / 2,515.4 / 3,267.5 ms (N=10).
- **Slowest overload request** (`…074816Z-c26f18c-000078`): total 29,819.2 ms = **waiting 29,156.0**, working 647.6, retrying 0.0.
  - Retrieval took 23.8 ms.
  - **29,439.2 ms passed before the provider's first token** and 364.4 ms after it; client overhead was 15.6 ms.
  - Trace evidence: the guard's TPM headroom was nearly used up (`tpm_available 1163`, `tokens_last_minute 6837`). The delay was quota waiting, before the first token.
- **Slowest real request** (`…081218Z-c26f18c-000005`): 3,457.4 ms = waiting 0.1, working 3,447.0; 3,093.6 ms before the first token and 353.5 ms after. This was model time, not queueing.

**Table 3 (`exp-20261113T064542Z-c482696`, qwen3.8-27b)**

| Setting | Citation pass | Correct | Refused correctly | Tokens / answer | Model-only TTFT p50 | Retrieval p50 |
| :-- | :-- | :-- | :-- | -: | -: | -: |
| small_fast | 93.3 % | 40.0 % | 100.0 % | 1,512.8 | 455.8 ms | 35.0 ms |
| balanced_rerank | 100.0 % | 46.7 % | 100.0 % | 1,816.2 | 469.0 ms | 1,260.2 ms |
| large_wide | 100.0 % | 40.0 % | 83.3 % (one row not delivered) | 3,034.3 | 648.8 ms | 32.6 ms |

### 8.3 Audit

**Secret scan** (`scratch/p11_secret_scan.py`; it prints locations and classes only, never values):
- 192 files: 191 tracked, plus `evidence/` and `results.csv`.
- **The configured `GROQ_API_KEY` value occurs 0 times.**
- **Findings in `evidence/` or `results.csv`: 0.**
- 13 pattern matches, **all in `tests/`, all synthetic redaction-test inputs:**
  - 1 × the fake `FAKE_KEY` fixture (`tests/factories.py`);
  - 3 × `Authorization` headers carrying `FAKE_KEY`;
  - 9 × one placeholder org ID (`org_01a…`) that the tests assert is redacted.

**`git check-ignore`:**
- Exit 1, meaning **not ignored and therefore trackable**, for `results.csv`, `evidence/canonical_runs.json`, an experiment `rows.csv`, the calibration `profile.json` and the probe `traces.jsonl`.
- Exit 0, ignored, for `.env`, `scratch/` and `data/papers/*.pdf`.
- `git ls-files .env` → 0 files.

**Recomputation:**
- `build_results.py --check` → `would_change: false`.
- `compute_tables.py --check` → identical.
- `results.csv` SHA-256 is unchanged after the final tests.

## 9. Errors and Root-Cause Analysis

1. **Three conflicts in the requested canonical list**, detected before writing:
   - the calibration run would make aggregation fail (`IncompleteRunError`);
   - omitting the probe run would contradict ARCH §13.2;
   - all grades were pending, against the P07 rule that grades are reviewed before rows become canonical.

   Root cause: the instruction's list was out of step with the aggregation contract and the architecture.
2. **E501:** one over-long line in `tables.py`.

## 10. Fixes Applied

1. All three were resolved by **owner decision** (§2). No code or architecture was changed for them.
2. The line was wrapped; ruff is clean.

## 11. Outstanding Issues

1. **`answer_grade` is blank in all rows.** After the owner reviews the 80 grades with `grade_answers.py review`, two steps remain:
   - (a) a decided mechanism to write reviewed grades into the run rows: aggregation does not read `grades.json`, and the run's `rows.csv` is evidence that must not be overwritten silently;
   - (b) `build_results.py --replace`.

   The tables already switch automatically to reviewed grades from `grades.json`.
2. **Quality figures are rubric proposals**, a literal-matching lower bound.
3. **Open items from earlier phases:**
   - P07: the failed q21 × large_wide cell and the verifier gap.
   - P10: the patience distribution and `deadline_ms` are proposals; reactive 429 back-off was not exercised under load.
4. **`scratch/computed_tables.md` is gitignored**, as specified. To submit the tables, they must be copied into the write-up or a tracked file (an owner choice).

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| `tables.py`, `compute_tables.py`, `test_tables.py` | Met |
| `canonical_runs.json` populated, superseded runs excluded | Met (12 runs; calibration cited and probe run included, by owner decision) |
| `results.csv` built and validated by `build_results.py` | Met (375 rows; byte-identical rebuild) |
| Tables 1, 2a, 2b and 3 with linear percentiles, N, one-decimal %, "not measured" | Met (`scratch/computed_tables.md`) |
| Slow-request decomposition, before vs after the first token, from rows + traces | Met (Table 2b) |
| Secret scan over tracked files and evidence | Met (0 real findings; 13 synthetic test inputs) |
| `git check-ignore`: evidence and `results.csv` trackable | Met |
| Recomputation byte-identical | Met (`results.csv` and tables) |
| `pytest -v` and ruff after the final edit | Met: 467 passed; ruff clean |
| Owner-reviewed grades in canonical rows | **Pending owner** (decided: rebuild later) |

## 13. Remaining Risks

- **Rebuilding after grade review needs a defined grade-application step** (§11.1). Without it, `results.csv` would keep blank grades.
- **Mixed percentile methods across reports** (nearest rank in P07/P10, linear here). The write-up should quote the P11 tables only.
- **Small N everywhere:** p99 is close to or equal to the maximum in most runs.

## 14. Recommended Next Step

Owner actions:
1. Review the 80 proposed grades.
2. Decide how reviewed grades enter the canonical rows (§11.1), then rebuild `results.csv` with `--replace` and re-run `compute_tables.py`.
3. Resolve the remaining P07/P10 items.
4. Write the two-page submission from the computed tables.

Nothing further has been started.
