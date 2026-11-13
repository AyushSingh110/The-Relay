# Phase 10 Implementation Report — Load Harness and Scenarios

> **Phase ID:** `p10`
> **Date:** `2026-11-13 13:55 IST`
> **Status:** `Completed`. All five canonical scenarios, burst cache on/off, retry amplification budget on/off, and one real steady run were executed.
> - **The patience distribution and `deadline_ms` are [PROPOSED]** (ARCH §16.4 and §16.10 are still open), so every result depends on them.
> - Verified in Conda env `the-relay` (Python 3.12.16): 461 passed, 0 failed, 0 skipped; ruff clean.
> - **Real Groq calls: 10** (cap 12).
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P10 and the phase prompt:

1. Build the load harness: client-side TTFT and end-to-end timing; a patience distribution with disconnect on expiry (`timeout` / `client_gave_up`); and the join of client rows with server traces.
2. Run steady, burst (cache on and off), sustained overload, per-minute limit halved, daily budget exhausted, and retry amplification (budget on and off) against the calibrated stand-in `cal-20261113T022403Z-3f687c6`.
3. Run one owner-approved real steady run (about 10 calls, `--max-real-calls 12`, `qwen/qwen3.8-27b`).
4. Write each run to its own `evidence/experiments/<run_id>/`.

Plan acceptance criteria: a 10-request stand-in smoke into `scratch/`; abandonment classified correctly; aggregation of the smoke run validates.

## 2. Implementation Summary

- **`patience.py`.** A seeded `PatienceModel` (log-normal or fixed, clipped) with an analytic `quantile()`.
  - Default **[PROPOSED]**: median 20 s, σ 0.6, floor 1 s.
  - `DEFAULT_DEADLINE_MS` = 30,000 **[PROPOSED]**: a fixed service-level deadline sent to the server, while patience stays private to the client.
- **`workloads.py`.**
  - Seeded arrivals (`poisson`, `burst`, `fixed_rate`), with phases, sequential phases, and random or in-order question draw.
  - `SCENARIOS` holds the eight specs: six stand-in scenarios, `real_steady` and `smoke`. Each carries its cache mode, retrieval setting, deadline, patience, stand-in overrides, guard overrides and retry-budget switch.
- **`eval/load_questions.json`.** **Question IDs only**, as pools over the owner's `eval/questions.json` (SHA-256 `ce4dfe41…` recorded). Text is read from the owner's file at run time. `paraphrases` is empty: paraphrase variants are question content and are left to the owner.
- **`load_runner.py`.**
  - **System under test:** the P08 app (`create_app`) under uvicorn on 127.0.0.1, with admission queue, cache + SingleFlight, pipeline and ProviderGuard. It talks over HTTP to the stand-in (a second uvicorn on 127.0.0.1) or to Groq.
  - **Admission capacity** is derived per run by Little's law (ARCH §15): `floor(TPM / (60 × tokens_per_answer) × patience median)`. The input values and their basis are recorded.
  - **Client:** streams `POST /ask` over httpx. `first_token_ms` is the first byte of a delivered `answer` event. At patience expiry the client cancels and closes the connection.
  - **After the run:** the runner waits for the server to drain (no flights, empty queue), then joins rows with `traces.jsonl` by request ID.
    - Completed requests take server fields from the `final` event.
    - Abandoned requests take spans from the server `request` record.
    - An abandoned **leader** takes its provider spend from its `flight` record, so token sums stay exact.
    - Abandoned rows are `timeout` / `client_gave_up` / `NOT_RUN`.
  - **`summary.json`:**
    - p50/p95/p99 and n for first token (delivered) and total time (all and delivered);
    - status and error-code counts and given-up count;
    - cache-status counts;
    - provider-call rows and retries, cross-checked against the stand-in's `/admin/stats`;
    - tokens, mean spans, a breakdown of the slowest delivered request, results per phase, the capacity derivation, the queue's peak depth and the final guard state.
- **`scripts/run_load_tests.py`.**
  - Writes `manifest.json`, `rows.csv`, `traces.jsonl`, `arrivals.json` and `summary.json`.
  - **Only `real_steady` may use `--source real`**, and it needs `--live`, a cap ≥ planned requests and ≤ `MAX_REAL_CALLS`, and a key. Without `--live` it is a dry run.

## 3. Files Created

- `src/relay/experiments/patience.py`, `src/relay/experiments/workloads.py`, `src/relay/experiments/load_runner.py`.
- `scripts/run_load_tests.py`.
- `eval/load_questions.json` (IDs only).
- `tests/test_load_harness.py` — 10 tests.
- `evidence/experiments/` — 10 new runs (§8). `exp-20261113T075226Z-c26f18c` is **superseded** (§9.3).
- `reports/phase-10-load-harness-and-scenarios.md` — this report.

## 4. Files Modified

- `ENGINEERING_LOG.md` — one appended entry.
- No earlier-phase module, test or evidence file was modified; `eval/questions.json` was untouched.
- `serve_on_loopback` (P09) is reused unchanged.

## 5. Technical Decisions

All items below are **[PROPOSED]** until the owner accepts them.

1. **Patience:** log-normal, median 20 s, σ 0.6, floor 1 s (ARCH §16.4).
2. **`deadline_ms`:** 30,000 for every request (ARCH §16.10).
3. **Load retrieval setting:** `small_fast`, at 35 ms retrieval against about 1.3 s for `balanced_rerank`, so the provider stays the bottleneck. No Table 3 setting has been chosen yet.
4. **Tokens per answer for capacity.**
   - Steady and real runs: **1,513** (P07 real `small_fast`).
   - Later stand-in runs: **809.1**, measured in the stand-in steady run `exp-20261113T074251Z-c26f18c`. The stand-in counts tokens differently (8.35 characters per token against about 3.6 real).
   - Derived capacities: 1 and 3.
5. **Guard as configured:** concurrency 2, TPM 8000 and RPD 1000 (headers), RPM not set, fraction 0.75. Daily-exhausted uses RPD 4 to simulate an exhausted guard; ARCH §9.4 allows this, and the real quota is never drained.
6. **Stand-in:** calibrated profile; knee 4 and slowdown 0.25 are assumed P09 defaults. Overrides: TPM 4000 (tpm_halved) and 30 % 503s (retry amplification).
7. **The harness and both servers share one process and one event loop**, communicating over loopback HTTP. This is simpler, but client timing shares CPU with the server (§13).
8. **An abandoned row's verdict is `NOT_RUN`** even if verification ran for others, because `ResultRow` requires `BLOCKED` to go with `citation_verification_failed`, and nothing was delivered to that client.
9. **The real run is restricted to `real_steady`** with distinct, in-order questions, so no real call is spent on a duplicate.

## 6. Commands Actually Executed

```bash
git status --short ; git log --oneline -3 ; sed/grep over IMPLEMENTATION_PLAN.md, ARCHITECTURE.md §15-16,
  ASSIGNMENT.md, aggregation.py, retry.py, api/app.py, tools.py
python (writes eval/load_questions.json from question ids; prints the source sha256 only)
python -c "build_arrivals preview"  ; python -m ruff check ...
timeout 300 python -m pytest tests/test_load_harness.py -q -x ; (x3) ; timeout 900 python -m pytest -q
timeout 600 python scripts/run_load_tests.py --scenario smoke --evidence-dir scratch/p10_smoke_evidence
python -c "build_results(scratch evidence, check_only=True)"
timeout 1200 python scripts/run_load_tests.py --scenario steady
for each of: burst --cache on | burst --cache off | overload | tpm_halved | daily_exhausted |
             retry_amplification --retry-budget on | retry_amplification --retry-budget off:
  timeout 1500 python scripts/run_load_tests.py --scenario ... --tokens-per-answer 809.1 \
      --tokens-per-answer-basis "measured in stand-in steady run exp-20261113T074251Z-c26f18c ..."
timeout 1500 python scripts/run_load_tests.py --scenario tpm_halved --tokens-per-answer 809.1 ...   # re-run, 8/min
python (read-only: daily_exhausted flight/request records)
timeout 120 python scripts/run_load_tests.py --scenario real_steady --source real --max-real-calls 12        # dry run
timeout 1200 python scripts/run_load_tests.py --scenario real_steady --source real --live --max-real-calls 12  # LIVE, once
python scratchpad/p10_tabulate.py <10 run ids> ; grep secret scan per run ; build_results(check_only) over 10 runs
timeout 900 python -m pytest -q ; python -m ruff check .                                      # final
```

`python` is `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`, with `HF_HUB_OFFLINE=1` set.

## 7. Test Results

- **Final**, after the last edit: `python -m pytest -q` → **461 passed, 0 failed, 0 skipped** (16.57 s); `python -m ruff check .` → **All checks passed!**
- `tests/test_load_harness.py` (10 tests) passed on its first run and 3 more times in a row (7.4–7.7 s). It covers:
  - patience seeding and quantiles;
  - arrival shapes, order and determinism;
  - the canonical scenarios building from the real pools, with real steady using 10 distinct questions;
  - the join for completed rows, abandoned leader vs follower, never-seen requests and client errors;
  - SSE parsing and percentiles;
  - **a 10-request end-to-end smoke with aggregation validation**;
  - **abandonment**: 3 of 3 rows `timeout`/`client_gave_up`, with the leader's estimated spend kept and followers paying 0;
  - the script refusing `--source real` for non-real scenarios, and its dry run.
- **Acceptance smoke in `scratch/`:** 10 requests, 10 `ok`, 8 provider calls equal 8 stand-in requests. `build_results(check_only=True)` → `checked`, 10 rows.
- **Aggregation validation of all 10 P10 evidence runs together:** `checked`, 299 rows. No duplicate IDs; every row passed `ResultRow` validation.

## 8. Actual Output and Generated Artifacts

The stand-in runs use the calibrated profile; their latency means stand-in latency, **not Groq's**. The real run is marked `real`.

Percentiles are nearest-rank; p95/p99 equal the maximum when n < 20 / n < 100. First token is over delivered requests; total is over all requests.

| Scenario (run) | Source | Sent | Duration (s) | Offered /min | First token p50 / p95 / p99 ms (n) | Total p50 / p95 / p99 ms (n) | ok | rate-limited | timeout (given up) | error | provider calls | retries |
| :-- | :-- | -: | -: | -: | :-- | :-- | -: | -: | -: | -: | -: | -: |
| steady (`…074251Z`) | stand-in | 19 | 281.5 | 4.07 | 698.1 / 835.7 / 835.7 (19) | 701.1 / 840.1 / 840.1 (19) | 19 | 0 | 0 | 0 | 19 | 0 |
| burst, cache on (`…074759Z`) | stand-in | 30 | 2.4 | 1049.6 | 604.5 / 1484.7 / 1492.2 (29) | 607.1 / 1497.6 / 1505.6 (30) | 29 | 1 | 0 | 0 | **6** | 0 |
| burst, cache off (`…074807Z`) | stand-in | 30 | 3.0 | 1049.6 | 1338.1 / 1783.4 / 1783.4 (8) | 33.3 / 1738.2 / 1786.5 (30) | **8** | **22** | 0 | 0 | 8 | 0 |
| overload (`…074816Z`) | stand-in | 86 | 246.5 | 21.86 | 807.7 / 24079.3 / 29817.4 (58) | 703.1 / 21698.4 / 29819.2 (86) | 58 | 19 | 9 (9) | 0 | 56 | 0 |
| tpm_halved, 8/min (`…080532Z`) | stand-in | 40 | 299.7 | 8.43 | 714.4 / 19097.4 / 19560.3 (31) | 715.4 / 19099.5 / 20539.3 (40) | 31 | 4 | 5 (5) | 0 | 30 | 0 |
| daily_exhausted (`…075725Z`) | stand-in | 18 | 106.3 | 10.19 | 13.6 / 740.2 / 740.2 (7) | 43.9 / 743.3 / 743.3 (18) | 7 | 11 | 0 | 0 | **3** | 0 |
| retry amplification, budget on (`…075917Z`) | stand-in | 23 | 152.5 | 9.51 | 717.9 / 13363.3 / 13363.3 (19) | 720.2 / 13367.8 / 17130.3 (23) | 19 | 0 | 2 (2) | 2 | 22 | 6 |
| retry amplification, budget off (`…080155Z`) | stand-in | 23 | 153.2 | 9.51 | 736.2 / 12869.5 / 13322.1 (21) | 765.7 / 13325.8 / 17119.4 (23) | 21 | 0 | 2 (2) | 0 | 22 | 9 |
| **real steady** (`…081218Z`) | **real** | 10 | 271.1 | 2.22 | 814.4 / 3455.6 / 3455.6 (10) | 816.0 / 3457.4 / 3457.4 (10) | 10 | 0 | 0 | 0 | 10 | 0 |

Superseded and not cited: `tpm_halved` at 4/min (`…075226Z`), with 20 sent, 20 ok, 0 waiting and 0 retries (§9.3).

**Findings, each read from the run files**

1. **Burst: the cache and SingleFlight decide who gets an answer.**
   - With cache on, 30 requests over 6 hot questions became **6 provider calls**: 7 misses (6 flights plus 1 `queue_full`), 16 coalesced and 7 exact hits. **29/30 delivered.**
   - With cache off, the same burst got **8/30 delivered**: 21 `queue_full` and 1 `predicted_wait_exceeds_deadline`.
   - The derived queue capacity (3) held: peak depth 3 in both runs.
2. **Overload (about 3× service rate).**
   - Shedding worked as designed: 11 `queue_full`, 8 `predicted_wait_exceeds_deadline`, peak depth 3 = derived capacity.
   - 9 clients gave up anyway.
   - The delivered tail is long: p95 24.1 s, p99 29.8 s, close to the 30 s deadline.
   - **Slowest delivered request** (`…074816Z-c26f18c-000078`, q10): total 29,819.2 ms, of which **waiting 29,156.0 ms**, working 647.6 ms and retrying 0. Retrieval was 23.8 ms. Provider first token arrived at 29,439.2 ms of server time and was delivered at 29,817.4 ms. **The delay came before the first token**: it was the guard's wait for TPM quota.
   - The stand-in returned 0 429s.
3. **Per-minute limit halved: the guard adapted from headers alone.**
   - The provider's TPM was 4000 while the guard was configured for 8000.
   - The stand-in returned **0 429s** and there were 0 retries. The guard took the provider's `remaining-tokens` header as headroom and paced to it.
   - Mean waiting 2.5 s and worst 18.9 s; 4 predictive sheds; 5 gave up.
   - The reactive path (429 → `retry-after` back-off) **did not occur in this run**; it is covered by the P06/P09 tests.
4. **Daily budget exhausted.**
   - 3 warm-up calls used the simulated budget (guard: "local daily requests 3 >= 0.75 x 4").
   - Afterwards **4 cache hits were still served** (first token p50 13.6 ms).
   - **11 misses got 429 `daily_budget_exhausted` with 0 provider calls and 0 tokens, each with 4 passage pointers.** Stand-in requests in total: 3. This was checked in the flight traces.
5. **Retry amplification under 30 % injected 503s, budget on vs off.**
   - **Budget on:** 28 provider attempts for 22 first attempts (×1.27); 6 retries; **2 requests failed** (`upstream_error`) when the budget refused a retry.
   - **Budget off:** 31 attempts (×1.41); 9 retries; 0 failures.
   - At this low load the budget cut retry traffic by 3 attempts (about 10 %) and cost 2 failed requests. Its value shows at higher load; this run is too small to show that.
6. **Real steady (Groq, `qwen/qwen3.8-27b`).**
   - 10 real calls, 10/10 delivered, first token p50 814 ms / max 3,456 ms, 0 retries, no give-ups.
   - Mean tokens per answer was **1,512.3**, against 1,513 measured in P07, an independent consistency check.
   - Slowest (`…081218Z-c26f18c-000005`, q07): 3,457.4 ms total; **working 3,447.0 ms**, waiting 0.1 ms; provider first token at 3,093.6 ms. This was model time, not queueing.
7. **Stand-in answers.** `citation_verification_failed` safe refusals appear in stand-in runs (1–8 per run) because the stand-in's extractive answers sometimes fail the P05 verifier. They measure plumbing, not quality.
8. **Security.** All 10 runs contain 0 `gsk_` strings, no files with bearer/authorization, and no `org_` IDs.

## 9. Errors and Root-Cause Analysis

1. **Scripted edits failed twice** (`workloads.py` and `load_runner.py`) because backslash continuations did not match inside Python heredocs. Nothing was written either time. The first attempt's arrivals preview had therefore run on unedited code.
2. **Pyright "argument missing" warning** on `join_row`: stale diagnostic. Ruff and the tests confirm the call passes `retrieval_setting`.
3. **The tpm_halved scenario was mis-sized (a real design error).** At 4/min × about 809 tokens per answer, the load (about 3.2k tokens/min) never reached even the halved 4,000 TPM. The first run (`…075226Z`) showed 0 waiting, 0 429s and 0 retries, so it did not test the limit.

## 10. Fixes Applied

1. Applied the edits with the Edit tool, then re-ran the preview and lint.
2. No change needed.
3. Changed the rate to **8/min**, between the halved capacity (about 4.9/min) and what the guard believes (about 9.9/min), with the reason in a code comment. Re-ran into a new directory (`…080532Z`). The first run is kept unmodified and marked superseded here.

The final suite passed 461.

## 11. Outstanding Issues

1. **Patience distribution and `deadline_ms` are proposals.** Results change if the owner chooses different values; the runs are seeded and cheap to repeat.
2. **Stand-in latency and knee are assumptions** (P09). Overload tails reflect the stand-in and TPM pacing, not Groq.
3. **Reactive 429 back-off was not exercised under load.** If wanted, a scenario with header sync disabled, or a stand-in that sends no remaining-tokens header, would force it.
4. **The retry-amplification run is small** (23 requests, 30 % errors). A larger or higher-rate run would show the budget's effect more clearly.
5. **Paraphrase variants for the load pools** are left to the owner (`paraphrases: {}`).
6. **The superseded `tpm_halved` run** remains in `evidence/`; it must not be selected for `canonical_runs.json`.
7. **`canonical_runs.json` has not been updated.** Selecting runs is an owner decision (P11).

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| `workloads.py`, `patience.py`, `load_runner.py`, `run_load_tests.py`, `load_questions.json`, tests | Met |
| Client-side TTFT and end-to-end timing | Met |
| Patience with disconnect → `timeout`/`client_gave_up` | Met (tests and 18 given-up rows across runs) |
| Join of client rows with server traces | Met (abandoned leaders keep flight spend) |
| Steady | Met |
| Burst, cache on vs off | Met (6 vs 8 calls; 29 vs 8 delivered) |
| Sustained overload: Little's-law capacity and predictive shedding | Met (capacity 3, peak 3, 19 shed) |
| Per-minute limit halved: throttle | Met as **proactive header-driven throttling**; no 429 occurred (§11.3) |
| Daily budget exhausted: 429 with pointers, 0 provider spend | Met (11 rows, 0 calls, 4 pointers each) |
| Retry amplification, budget on vs off | Met (×1.27 vs ×1.41) |
| Real steady run (about 10 calls, cap 12) | Met (10 calls) |
| Each run in its own evidence directory with manifest, rows, traces, summary | Met (plus `arrivals.json`) |
| 10-request smoke into `scratch/`; abandonment rows; aggregation validates | Met |
| pytest and ruff after the final edit | Met: 461 passed; ruff clean |

## 13. Remaining Risks

- **Shared event loop.** The harness, the Relay and the stand-in run in one process, so client-measured times include some scheduling contention. Client overhead was 10–35 ms on the slowest requests.
- **Small samples:** n = 10–86 per run. p99 equals the maximum in every run.
- **The stand-in's extractive answers trigger some verifier blocks**, inflating `citation_verification_failed` in the stand-in rows; this is not quality evidence.
- **Daily-exhausted used a simulated guard budget** (RPD 4), by design. A real daily 429 was never observed.

## 14. Recommended Next Phase

Owner decisions first:
1. Patience distribution and `deadline_ms`.
2. Whether to add the reactive-429 scenario and a larger retry-amplification run.
3. Which runs become canonical.
4. The open P07 items: grade review, the q21 × large_wide cell, the verifier gap, and how grades flow into rows.

Then **P11 — tables, forensics and submission audit**. Nothing has been started.
