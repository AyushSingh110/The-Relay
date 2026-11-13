# Phase 09 Implementation Report — Stand-In Service and Calibration

> **Phase ID:** `p09`
> **Date:** `2026-11-13 07:54 IST`
> **Status:** `Completed`. Verified in Conda env `the-relay` (Python 3.12.16): 435 passed, 0 failed, 0 skipped; ruff clean. **0 Groq calls.** Network use: loopback (`127.0.0.1`) only.
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P09, `ARCHITECTURE.md` §12 and the owner's phase prompt:

- **Stand-in service.** A separate FastAPI service with OpenAI-compatible `chat/completions` (SSE with a usage chunk) and `models` endpoints, plus `/admin/config`, `/admin/reset` and `/admin/stats`.
  - Behaviour is calibrated or seeded: time to first token (TTFT), inter-token delay, slowdown above a concurrency knee, injected 5xx errors, stragglers and stalled streams.
  - It enforces RPM / TPM / RPD / TPD, answering with 429, `retry-after` and the Groq header names and meanings.
  - Answers are extractive and cite `[P1]..[Pk]`.
- **`scripts/calibrate_standin.py`.**
  - It reads the P02-LIVE evidence and writes `evidence/standin_calibration/<run_id>/` (`manifest.json`, `profile.json`, `fit_report.json`).
  - The fit report includes the stand-in vs measured quantile comparison and the "least like the real thing" list.
  - Without evidence it falls back to `uncalibrated_defaults`.
- **Tests** drive the stand-in through `HttpOpenAIProvider`.
- Constraint: 0 real Groq calls.

## 2. Implementation Summary

- **`standin/profile.py`**
  - A frozen `StandinProfile` with, per model:
    - latency: lognormal header delay plus a prompt-size slope, an inter-token gap, and a reasoning-token count;
    - tokens: template overhead plus characters per token.
  - Global settings: `Limits` (RPM/TPM/RPD/TPD, reservation rule), `LoadModel` (knee, slowdown), `FaultModel` (5xx, stragglers, stalls), `time_scale`, `seed`.
  - Every parameter has a provenance note (`measured` / `derived` / `header` / `assumed` / `not measured` / `scenario`).
  - Functions:
    - `uncalibrated_defaults()`;
    - `calls_from_traces()`, which keeps only successful inference calls with usage;
    - `fit_profile()` / `fit_model()`;
    - `header_checks()`, which tests the token-bucket reading of Groq's headers on every probe call;
    - seeded sampling helpers.
- **`standin/server.py`**: `create_standin_app(profile, clock=, sleep=)`.
  - Streams one SSE chunk per reasoning token, then one per answer word, then a `finish_reason` chunk, then a usage chunk (`usage` and `x_groq.usage`), then `[DONE]`. Non-streaming JSON is also supported.
  - **Limits.** Continuous-refill token buckets (limit per 60 s or per 86,400 s), checked in the order RPD, TPD, RPM, TPM.
    - A breach returns 429 with a Groq-style body ("… (TPM): Limit L, Used U, Requested R. Please try again in Xs."), an integer `retry-after`, and the six `x-ratelimit-*` headers.
    - A request larger than the whole token limit returns 413.
    - Header semantics follow the P02-LIVE evidence: `*-requests` is the daily bucket and `*-tokens` the minute bucket; `reset-*` is the time until the bucket is full again, formatted like Groq (`1m26.4s`, `7m12s`, `2.28s`).
    - TPM/TPD are charged prompt + `max_completion_tokens` at admission, and the unused part is refunded when the stream completes.
  - **Load and faults.**
    - The load factor `1 + slowdown × max(0, active − knee)` multiplies the header delay and token gaps.
    - Stragglers multiply both by `straggler_multiplier`.
    - Stalls pause after N answer pieces and then close the stream without `[DONE]`.
    - 5xx errors are returned before any quota is charged.
    - Randomness is seeded per request (`seed:index`).
  - **Answers.** Up to 2 passage sentences with the most question-word overlap, each tagged `[Pi]`. `INSUFFICIENT_EVIDENCE` when nothing overlaps; `OK` for prompts without passages.
  - **Admin.**
    - `/admin/config` accepts a partial, validated override, recorded with provenance `scenario` and listed in `overrides`. Identity fields such as the label cannot be changed.
    - `/admin/reset` refills the buckets, zeroes the counters, restarts the seed sequence, and can exhaust named budgets (for the daily-exhausted scenario) or restore the base profile.
    - `/admin/stats` reports requests, statuses, 429s by metric, injected faults, peak active requests, tokens and bucket levels.
  - **Running it:** `python -m relay.standin.server --port 8001 --profile <profile.json>`. The `serve_on_loopback()` helper is used by the script and the tests.
- **`scripts/calibrate_standin.py`**
  - Loads every *complete* probe run and fits the profile.
  - Replays each probe call shape (model, prompt characters, `max_completion_tokens`) sequentially on loopback through `HttpOpenAIProvider`, with rate limits switched off.
  - Compares n / min / p50 / p90 / max for header time, first content, total time, prompt tokens and completion tokens.
  - Ranks the largest differences and writes the run directory.

## 3. Files Created

- `src/relay/standin/__init__.py` — package docstring.
- `src/relay/standin/profile.py` — profile contract, defaults, fit, header checks, sampling.
- `src/relay/standin/server.py` — the stand-in service, limiter, faults, extractive answers, admin, CLI entry point.
- `scripts/calibrate_standin.py` — offline calibration and replay comparison.
- `tests/test_standin.py` — 26 tests.
- `evidence/standin_calibration/cal-20261113T022150Z-3f687c6/` — first calibration run. **Superseded**, produced with the pacing bug in §9.1; kept unmodified.
- `evidence/standin_calibration/cal-20261113T022403Z-3f687c6/` — calibration run made after the fix (**use this one**).
- `reports/phase-09-standin-service-and-calibration.md` — this report.

## 4. Files Modified

- `ENGINEERING_LOG.md` — one appended entry.
- No earlier-phase source, test or evidence file was modified.

## 5. Technical Decisions

All items below are **[PROPOSED]** until the owner accepts them.

1. **Token buckets with continuous refill.** The P02-LIVE headers support this reading:
   - all 15 `reset-requests` values equal `(limit − remaining) × 86400 / limit`;
   - 11 of 15 `reset-tokens` values equal `(limit − remaining) × 60 / limit`;
   - the 4 that do not are qwen calls 2–5, which report 9.9 s and 17.665 s where the formula predicts 1.583–16.695 s.
2. **Header meanings:** `*-requests` = RPD bucket, `*-tokens` = TPM bucket. This is "consistent", the same standing as in the P02-LIVE report, not formally verified.
3. **Reservation rule:** prompt + `max_completion_tokens` charged at admission, the unused part refunded at stream end. This is the conservative reading of the unreconciled P02-LIVE observation. It is configurable (`tpm_charge`, `settle_to_usage`).
4. **Limit check order:** daily before minute, so a request that breaks both learns about the long wait. Groq's order is unknown.
5. **429s and 5xx consume no quota.** An injected 5xx happens before the charge. Groq's behaviour here is unknown.
6. **RPM and TPD are `None` (not enforced) in the calibrated profile**, because neither was measured. Experiments set them through `/admin/config`, and they are recorded as `scenario`.
7. **Limits are global, not per model** (Groq's are per model).
8. **Answers are at most 2 extractive sentences.** Output tokens are counted at an assumed 4 characters per token.
9. **Replay runs with limits switched off**, so the comparison covers latency and tokens only. The 15 probe shapes with a 256-token reservation each would otherwise exceed the 8000 TPM bucket within seconds under the stand-in's reservation rule, while Groq returned no 429.
10. **Stream pacing uses cumulative deadlines on `time.perf_counter`** (§9.1).
11. **Probe runs that are not `complete` are skipped.** The discovery run, `probe-20261112T031108Z-2b5d017`, contains only a models call, so it contributes nothing.
12. **Sigma is floored at 0.05** (gpt-oss-120b's measured log-SD is 0.0191 from n = 5).

## 6. Commands Actually Executed

```bash
git status --short ; git log --oneline -3
sed -n 160,168p IMPLEMENTATION_PLAN.md ; sed -n 273,303p ARCHITECTURE.md ; sed -n 211,248p ARCHITECTURE.md
grep -n -i "stand-in" ASSIGNMENT.md
cat evidence/groq_probes/*/summary.json ; python (read-only tabulation of traces.jsonl timings, usage, headers)
sed/grep over http_provider.py, headers.py, base.py, prompt.py, verifier.py, run_store.py, probe_groq.py
python -c "fit_profile on the real baseline traces"    # read-only check of the fit
python -m ruff check src/relay/standin scripts/calibrate_standin.py tests/test_standin.py
timeout 300 python -m pytest tests/test_standin.py -q -x
for i in 1..5: timeout 200 python -m pytest tests/test_standin.py -q
timeout 900 python -m pytest -q        # 1 intermittent failure, then 3 passing runs (see §9.2)
for i in 1..6: timeout 900 python -m pytest -q   # after the test fix
timeout 300 python scripts/calibrate_standin.py --evidence-dir evidence --seed 0 --replay-repeats 3   # run 1
python (read-only: per-call replay gaps; asyncio sleep accuracy; time.get_clock_info)
for i in 1..3: timeout 200 python -m pytest tests/test_standin.py -q   # after the pacing fix
timeout 120 python scratchpad/p09_pacer_check.py      # regression test discriminates
timeout 300 python scripts/calibrate_standin.py --evidence-dir evidence --seed 0 --replay-repeats 3   # run 2
python (read-only summary of run 2) ; grep -rl "gsk_|authorization|Bearer" evidence/standin_calibration/
timeout 900 python -m pytest -q ; python -m ruff check .   # final
```

`python` is `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`.

## 7. Test Results

After the final source edit:

- `python -m pytest -q`: **435 passed, 0 failed, 0 skipped** (11.54 s). That is 409 before P09 plus 26 new.
- `python -m ruff check .`: **All checks passed!**
- Stability:
  - `tests/test_standin.py` passed 5 times before the pacing fix and 3 times after it (25 and then 26 tests).
  - The full suite passed 6 times in a row after the timing-assertion fix (434 each, before the pacing test was added).

Acceptance criteria, all driven through `HttpOpenAIProvider` against uvicorn on 127.0.0.1:

| Criterion | Test(s) | Observed |
| :-- | :-- | :-- |
| SSE + usage | `test_stream_delivers_reasoning_then_cited_content_and_usage` | reasoning then content then usage; prompt / reasoning / total tokens consistent; answer `VERIFIED` by the P05 verifier |
| RPM 429 + `retry-after`, then recovery | `test_rpm_limit_returns_429_with_retry_after_then_recovers` | scope `minute`, metric `RPM`, `retry-after` 30; still 429 at +29 s; OK at +30 s |
| TPM 429 + reservation, then recovery | `test_tpm_limit_uses_the_reservation_and_recovers` | remaining 190 after a 310-token reservation; settled to usage; 429 with `Limit 500, Requested 310`; 413 above the limit; OK after `retry-after` |
| RPD 429, verified header semantics, recovery | `test_rpd_limit_is_daily_not_retryable_and_recovers_after_its_window` | remaining 2/1/0; reset 28,800 / 57,600 / 86,400 s (header `8h`); scope `daily`, `daily_budget_exhausted`, not retryable; OK after 28,800 s |
| Slowdown above knee | `test_latency_slows_above_the_concurrency_knee` | peak active 4; slowest ≥ 200 ms vs solo < 200 ms |
| Injected 5xx | `test_injected_5xx_is_a_retryable_server_error_and_costs_no_quota` | 503 becomes a retryable `ProviderServerError`; RPD bucket unchanged |
| Slow stragglers | `test_stragglers_are_much_slower` | straggler ≥ 190 ms and ≥ 100 ms slower |

Also covered:
- stalls (client stall timeout, and a close without `[DONE]`);
- truncation (`finish_reason=length`);
- non-streaming calls and `/models`;
- 404 and 400 responses;
- the P05 `Pipeline` end to end against the stand-in;
- admin exhaust, reset, config overrides and validation;
- duration formatting;
- the fit, header checks and the no-evidence fallback;
- seeded sampling and quantiles;
- the calibration script with and without evidence (on `tmp_path`);
- the pacing regression test.

## 8. Actual Output and Generated Artifacts

The stand-in values below come from the offline replay. The measured values are re-read from the P02-LIVE traces. **None of this is a new Groq measurement.**

**Run `cal-20261113T022403Z-3f687c6`**
- Manifest: `kind` standin_calibration, `status` complete, `source` stand-in, `seed` 0, `real_call_count` 0, `git_dirty` true (P09 not yet committed).
- Fit: 15 probe calls, all from `probe-20261112T031816Z-2b5d017`; label `calibrated`.
- Replay: 45 calls, 0 errors.
- Header checks: requests 15/15 matched, tokens 11/15 matched.

Fitted profile (`profile.json`); limits are `tpm 8000`, `rpd 1000`, `rpm null`, `tpd null`:

| Model | header median (ms) | sigma | ms per 1k prompt tokens | inter-token (ms) | reasoning tokens | prompt tokens = base + chars / c |
| :-- | -: | -: | -: | -: | -: | :-- |
| gpt-oss-20b | 458.075 | 0.4216 | 55.333 | 1.065 | 41 | 84 + chars / 8.3566 |
| gpt-oss-120b | 498.88 | 0.05 (floor) | 105.721 | 2.0296 | 43 | 84 + chars / 8.3566 |
| qwen3.8-27b | 272.29 | 0.1681 | 0.015 | 2.581 | 0 | 27 + chars / 8.3514 |

p50 comparison: measured n = 5 per model; stand-in n = 15 per model.

| Model | headers ms (meas / stand-in) | first content ms | total ms | completion tokens |
| :-- | :-- | :-- | :-- | :-- |
| gpt-oss-20b | 471.1 / 510.2 | 520.1 / 543.1 | 521.3 / 543.4 | 51 / 42 |
| gpt-oss-120b | 521.8 / 540.5 | 631.1 / 634.4 | 638.3 / 635.0 | 53 / 44 |
| qwen3.8-27b | 272.3 / 294.0 | 280.2 / 295.8 | 283.9 / 296.5 | 2 / 1 |

- p90 and max are in `fit_report.json`. The stand-in's tail is wider than measured for gpt-oss-20b: p90 total 921.6 vs 690.7 ms, because its sigma (0.42) is fitted from 5 points.
- The largest p50 differences in `fit_report.largest_differences` are all **completion tokens** (qwen ratio 0.5, gpt-oss about 0.82). Groq counts about 9–10 more completion tokens than reasoning + visible content; the stand-in does not model them.
- **Superseded run** `cal-20261113T022150Z-3f687c6`: same fit, but its gpt-oss first-content p50 was 524.9 / 555.3 ms because of the §9.1 bug. Kept unmodified; do not cite it.
- **Secret scan** of `evidence/standin_calibration/` for `gsk_`, `authorization` and `Bearer`: no matches.

## 9. Errors and Root-Cause Analysis

1. **Stream pacing was wrong on Windows (real bug, found in calibration run 1).**
   - Symptom: in the replay, gpt-oss content started 8–21 ms after the headers, where the profile's 41–43 reasoning tokens at 1.07–2.03 ms should take about 44–87 ms.
   - Root cause: the event loop's clock is `time.monotonic` = `GetTickCount64`, resolution 15.625 ms (from `time.get_clock_info`). One `asyncio.sleep` of about 2 ms per token returns early or late by up to a tick, and the error accumulates. Measured: 43 × `sleep(2.03 ms)` took 656 ms in isolation, and about 17 ms inside the server.
2. **Intermittent full-suite failure** of `test_stragglers_are_much_slower` (1 of 4 runs).
   - Root cause: the assertion `straggler ≥ 3 × normal`. The normal call's time includes connection set-up and scheduling jitter that the profile does not scale, so a loaded machine can inflate it.
3. **Wrong test expectation:** the token-fit intercept is 37.5, which `round` makes 38. The test expected 37.
4. **Lint findings:** SIM905 (string split), I001 / E501 (import layout), SIM117 (nested `with`).

## 10. Fixes Applied

1. Added `_Pacer`: each stream waits for `start + cumulative token time` on `time.perf_counter`, so errors self-correct.
   - New test `test_many_short_token_gaps_add_up_to_the_profiled_stream_time` expects 100 ms within an 80–140 ms window.
   - A scratch check confirmed it discriminates: 107.1 / 94.2 / 92.4 ms with the fix; 19.0 / 14.5 / 9.8 ms with naive sleeps.
   - Calibration was re-run as a new directory; the first run is kept and labelled superseded here.
2. Rewrote the timing assertions to use the deterministic sleep floor plus a gap, not ratios. Six consecutive full runs then passed.
3. Corrected the test expectation to 38, with a comment.
4. Fixed the lint findings; ruff is clean.

## 11. Outstanding Issues

1. **The calibration rests on 15 sequential calls with tiny outputs.** P02-LIVE §11.4 already said so. Re-calibrating from P07/P10 real runs would improve it, and the script takes any new complete probe run automatically. It does not yet read experiment runs.
2. **The extra completion tokens are not modelled:** Groq reports about 9–10 more than reasoning + content.
3. **Not measured, so scenario inputs:** knee, slowdown, 5xx rate, stragglers, stalls, RPM, TPD.
4. **The 429 format was never observed on Groq.**
5. **Superseded calibration run** `cal-20261113T022150Z-3f687c6` remains in `evidence/`. The owner decides whether it stays tracked; it is not to be selected anywhere.
6. **Not wired into the Relay yet.** Nothing yet sets `Settings.standin_url` usage end to end; that is P10.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| OpenAI-compatible stand-in with SSE + usage, `/models`, admin endpoints | Met |
| Calibrated latency, knee slowdown, 5xx, stragglers, stalls | Met (latency calibrated; the others are labelled `assumed`) |
| RPM/TPM/RPD/TPD 429 with `retry-after` and Groq headers, then recovery | Met (TPD covered via admin exhaust) |
| Extractive answers citing `[Pi]` | Met (verified by the P05 verifier in tests) |
| Calibration reads P02-LIVE evidence, writes `standin_calibration/<run_id>/` | Met |
| `uncalibrated_defaults` label without evidence | Met (tested) |
| Quantile comparison and "least like the real thing" list | Met (`fit_report.json`, 11 items, plus the computed largest differences) |
| Driven through `HttpOpenAIProvider` | Met |
| pytest and ruff after the final edit | Met: 435 passed; ruff clean |
| Real Groq calls | 0 |

## 13. Remaining Risks

- **Load results reflect assumptions.** Any overload result from the stand-in reflects the assumed knee and failure settings. The write-up must say so, and these are the entries at the top of the "least like the real thing" list.
- **The reservation rule may be stricter than Groq's.** If so, the stand-in sheds earlier than Groq would at the same TPM.
- **Global buckets** make multi-model runs unrepresentative.
- **Timing accuracy is bounded by about one 15.6 ms tick per sleep on Windows**, so per-token timing finer than that is approximate.

## 14. Recommended Next Phase

**P10 — Load Harness and Scenarios**, against the stand-in started with `evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json`.

Owner decisions needed first:
1. the §5 items (especially 3, 6 and 9);
2. the scenario values for knee, slowdown, fault rates and RPM/TPD;
3. the patience distribution and `deadline_ms` (ARCH §16 items 4 and 10);
4. whether to keep the superseded calibration run.

P07 (real evaluation) still needs its own approval. No next phase has been started.
