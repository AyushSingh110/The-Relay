# Phase 06 Implementation Report — ProviderGuard and Admission Queue

> **Phase ID:** `p06`
> **Date:** `2026-11-13 07:09 IST`
> **Status:** `Completed`. Verified in Conda env `the-relay` (Python 3.12.16): 372 passed, 0 failed, 0 skipped; ruff clean. **0 Groq calls. No network use.**
> **Owner Approval:** `Owner Approved (2026-11-13)`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P06, build the control plane.

**ProviderGuard, the sole quota owner:**
- a concurrency semaphore;
- local RPM and TPM windows;
- header sync per the verified semantics;
- a local daily counter and the daily safety stop;
- token reservation and settlement from provider usage, with an adaptive `chars_per_token`;
- a retry policy with full jitter, `retry-after` and a deadline share;
- a retry budget with an on/off switch;
- a minimal circuit breaker;
- degraded top-k when token headroom is tight.

**Admission queue:**
- capacity derived from service rate × patience quantile;
- predictive shedding;
- deadline and disconnect checks.

Out of scope, and not done: the HTTP service and cache (P08), the stand-in (P09), any real provider call.

## 2. Implementation Summary

The new package is `src/relay/control/`.

**`retry.py`**
- **`RetryPolicy`:**
  - Backoff is full jitter: the n-th retry waits `uniform(0, min(max_delay, base × 2^(n−1)))`, and at least `retry-after` when the provider sent one.
  - `decide()` refuses to retry, with a reason, when the error is not retryable, when the scope is daily, when the attempt limit is reached, when the wait would exceed `deadline_share` of the remaining deadline, or when the budget says no.
- **`RetryBudget`:** within a rolling window, retries may not exceed `ratio × first_attempts + min_retries`. The budget is shared across requests. `enabled=False` switches it off for the retry-amplification experiment, and every decision records whether the budget was enforced.

**`provider_guard.py`**
- **`ProviderGuard`** protects one model. It holds:
  - **Concurrency:** an `asyncio.Semaphore`.
  - **Minute windows:** sliding 60-second windows of request timestamps (RPM) and token charges (TPM).
  - **Header sync:** `x-ratelimit-*-requests` updates *daily* state only; `x-ratelimit-*-tokens` updates the *per-minute* token state. Provider-reported remaining tokens override the local view until the header's reset time. **RPM comes only from configuration; no header is ever read as RPM.**
  - **Daily stop:** local daily request and token counters, with rollover on a new UTC day. The stop fires at `daily_safety_fraction` (0.75) of the RPD or TPD limit, when the header shows that much used, or after an upstream daily-scope 429.
  - **Reservation:** `estimate = ceil(prompt_chars / chars_per_token) + max_completion_tokens`.
  - **Settlement:**
    - usage reported → charge `prompt + completion`;
    - stream opened without usage → the reservation **stays charged** and tokens are reported as `estimated`;
    - rejected before any response → released.
  - **Adaptive `chars_per_token`:** starts at the owner-approved 3.0, moves towards the observed `prompt_chars / prompt_tokens` with a 0.9 safety margin, and is bounded to [1.0, 6.0].
  - **Circuit breaker:** N consecutive server, timeout, connection or incomplete-stream failures open it for a fixed cooldown. Then exactly one trial call is allowed. A 429 is quota feedback and does not count as a failure.
  - **`fits(request)`:** whether a request fits the current token headroom. This drives degraded top-k.
- **Local refusals** are typed `GuardError`s that carry existing error codes. None of them touches the provider:

  | Refusal | Error code |
  | :--- | :--- |
  | Daily safety stop | `daily_budget_exhausted` |
  | Circuit open | `circuit_open` |
  | Quota wait longer than the deadline | `predicted_wait_exceeds_deadline` |
  | Deadline already passed | `deadline_expired` |
  | Request larger than the whole minute limit | `payload_too_large` |

- **`GuardedGenerator`** implements P05's `AnswerGenerator`. For each attempt it takes a permit, makes exactly one provider call, settles, syncs headers, feeds the circuit breaker and applies the retry policy. Retries are visible: they are counted in `retries` and timed in the `retrying` span. Tokens are summed across attempts.

**`admission.py`**
- **`derive_capacity`:**

  ```
  service_rate = min(RPM/60, TPM/(60 × tokens_per_answer), slots/service_time)
  capacity     = max(1, floor(service_rate × patience_quantile))
  ```

  It returns the inputs, every bound, the binding bound and the result, so the number can be recomputed from a run manifest.
- **`AdmissionQueue`** is FIFO through `max_active` service slots. It:
  - sheds `queue_full` on entry;
  - sheds `predicted_wait_exceeds_deadline` on entry;
  - re-checks `deadline_expired` and `client_gave_up` when a request reaches a slot;
  - cleanly removes a cancelled (disconnected) waiter;
  - tracks `queue_depth_at_entry` and peak depth.

**`orchestrator/pipeline.py`** (P05 module, additive change)
- `AnswerGenerator.generate` and `Pipeline.run` accept an optional `deadline_ns`.
- `GenerationResult` gains `attempts` and `tokens` (totals across attempts). With these, a local refusal yields `provider_called=false`, and retries sum tokens correctly.
- An optional `budget` (anything with `fits`) lets the pipeline drop the lowest-ranked passages when headroom is tight, setting `degraded=true`.
- Behaviour without these options is unchanged: all 64 P05 tests pass unmodified.

## 3. Files Created

- `src/relay/control/__init__.py`: package marker.
- `src/relay/control/retry.py`: `RetryConfig`, `RetryBudgetConfig`, `RetryBudget`, `RetryPolicy`, `RetryDecision`.
- `src/relay/control/provider_guard.py`: `GuardConfig`, `ProviderGuard`, `Permit`, `Settlement`, `GuardSnapshot`, the `GuardError` family, `GuardedGenerator`.
- `src/relay/control/admission.py`: `CapacityInputs`, `CapacityDerivation`, `derive_capacity`, `AdmissionQueue`, `Ticket`, `AdmissionRejectedError`.
- `tests/test_control_plane.py`: 34 tests.
- `reports/phase-06-provider-guard-and-admission.md`: this report.

## 4. Files Modified

- `src/relay/orchestrator/pipeline.py`: the additive changes described in §2 (72 insertions, 18 deletions). This is a P05 module; the change is the integration point P06 needs, not a refactor.
- `ENGINEERING_LOG.md`: P06 entry appended.
- No other earlier-phase file was changed; nothing was installed.

## 5. Technical Decisions

Every numeric default is [PROPOSED] and needs owner review. None is a measurement.

1. **Header semantics follow the P02-LIVE evidence:** `*-requests` = per day (the reset grew by 86.4 s per request, with per-model counters) and `*-tokens` = per minute. That reading was "consistent, not formally verified". The guard keeps it isolated in `sync_headers`, so a correction is a one-method change.
2. **RPM and TPD are never inferred.** They are `None` (not enforced locally) unless configured from the Groq console or docs.
3. **Reservation includes the full `max_completion_tokens`.** P02-LIVE saw Groq's remaining-tokens header drop by more than the reported usage. Reserving the whole completion allowance is the conservative reading.
4. **Settlement to zero for rejected calls.** A request refused before any response, such as a 429, consumed nothing, so its reservation is released. A stream that opened without usage keeps its full reservation (ARCH §9.3).
5. **Quota waits are attributed to the `waiting` span,** not `working`. Slow-request forensics can then tell "waited for quota" from "model was slow".
6. **No new error codes.** A local quota wait that cannot finish before the deadline reuses `predicted_wait_exceeds_deadline`. A request larger than the whole per-minute limit reuses `payload_too_large`.
7. **Daily exhaustion and circuit open return a failed outcome with the retrieved passages kept.** ARCH §5 calls this an "honest unavailability notice + passage pointers"; ARCH §13.4 classifies it as `rate_limited` / `error` with nothing counted as delivered. P06 follows the table and leaves `outcome.passages` populated, so P08 can render pointers in the error body.
8. **Degraded top-k drops the lowest-ranked passages** until the estimate fits the current headroom, keeping at least one. If even one passage does not fit, the guard waits or sheds.
9. **A cancelled in-flight call keeps its reservation charged,** because tokens may already have been spent, and frees its concurrency slot.
10. **The admission queue predicts wait from a fixed service rate** (the derived one), not from live throughput.
11. **Defaults:**
    - retry: 3 attempts, base 0.5 s, cap 8 s, deadline share 0.5;
    - budget: ratio 0.2 + 3 retries per 60 s;
    - guard: concurrency 2;
    - circuit: 5 failures, 30 s cooldown;
    - adaptation: weight 0.2, margin 0.9, bounds 1.0–6.0.

## 6. Commands Actually Executed

Git Bash with `PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe`. `.env` was not read.

```bash
git status --short; git log --oneline -4              # clean; HEAD fc57d4c (P05 committed by owner)
$PY <scratch>/p06_pipeline_edit.py                    # additive pipeline edits applied
$PY -m pytest -q tests/test_pipeline_and_verifier.py  # 64 passed (P05 behaviour unchanged)
$PY <scratch>/p06_guard_edit.py                       # three guard fixes from self-review
$PY -c "import relay.control.provider_guard, relay.control.admission"   # import ok
$PY -m pytest -q tests/test_control_plane.py          # run 1: hung (test bug) -> stopped
timeout 60 $PY -m pytest -v -x tests/test_control_plane.py   # located the hanging test
timeout 120 $PY -m pytest -v tests/test_control_plane.py     # 2 failed, 32 passed
# fixes (one real queue bug, one test expectation)
$PY -m pytest -q tests/test_control_plane.py          # 34 passed
$PY -m ruff check .                                   # 1 SIM300 -> fixed
# Final, after the last edit:
$PY -m pytest -q -p no:cacheprovider -rs              # 372 passed in 4.68s
$PY -m ruff check .                                   # All checks passed!
```

**Verification commands for the owner (PowerShell):**

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
python -m pytest -v tests/test_control_plane.py
python -m pytest -q
python -m ruff check .
git status --short
git diff --stat
```

## 7. Test Results

These are from the final runs, after the last code edit:
- `python -m pytest -q`: **372 passed, 0 failed, 0 skipped** (4.68 s; `filterwarnings=error`). That is 338 earlier tests plus 34 new.
- `tests/test_control_plane.py`: **34 passed**. `tests/test_pipeline_and_verifier.py`: 64 passed, unmodified.
- `python -m ruff check .`: **All checks passed!**

What the 34 tests cover (fake clock and sleep, seeded RNG, `ScriptedProvider`):
- **Retry (8):** jitter bounds and the `retry-after` floor; 6 decision cases (not retryable, daily scope, max attempts, deadline share, two retry cases); budget throttling, window rollover and the off switch.
- **Guard (12):**
  - settlement to usage;
  - missing usage kept as `estimated`, and a rejected call released;
  - adaptive `chars_per_token` and its floor;
  - headers never read as RPM;
  - header token headroom overriding the local view, then resetting;
  - a TPM wait recorded as a waiting span;
  - an RPM wait vs the deadline;
  - an expired deadline and an oversized request;
  - `retry-after` blocking;
  - the semaphore;
  - the daily stop (local, header, tokens, rollover);
  - the circuit open → half-open → closed cycle.
- **GuardedGenerator (6):** retry then success with summed tokens and the `retrying` span; non-retryable and exhausted retries; a daily fast fail with 0 provider calls; an upstream daily 429 not retried and stopping later calls; headers reaching the guard; cancellation mid-call.
- **Pipeline integration (2):** retries and the span invariant; a fast fail with `provider_called=false`; degraded top-k.
- **Admission (6):** capacity derivation; FIFO and `queue_full`; predicted-wait shedding; deadline and disconnect re-checks; a disconnect before the provider call spending 0 tokens; invalid configuration.

## 8. Actual Output and Generated Artifacts

All outputs are from offline tests with synthetic limits. **No Groq limit, latency or throughput was measured in this phase.**

Worked examples asserted by the tests:
- **Reservation and settlement.** A 300-character prompt with `max_completion_tokens=100` reserves `ceil(300/3.0) + 100 = 200` tokens.
  - Usage of 80 + 20 settles it to 100, leaving `tpm_available` at 900 of 1000.
  - Without usage it stays at 200 and reports 100 in / 10 out as `estimated`.
- **Adaptation.** Observing 150 prompt tokens for 300 characters moves `chars_per_token` from 3.0 to `0.8×3.0 + 0.2×(0.9×2.0) = 2.76`.
- **Headers.** After `limit_requests=1000, remaining=990, limit_tokens=8000, remaining=7000`, the guard shows RPD 1000 / 990, TPM 8000 with 7000 available and **RPM still `None`**. 40 requests inside one minute are not throttled.
- **Capacity.** `rpm=30, tpm=8000, tokens_per_answer=2000, patience=20 s` gives bounds 0.5/s (RPM) and 0.0667/s (TPM), so TPM binds and `capacity = max(1, floor(1.33)) = 1`. These inputs are test values, not measurements.
- **Retries.** One failed attempt that streamed 6 characters, followed by a success with usage 80 + 20, gives `attempts=2`, `retries=1`, tokens 180 in / 22 out with `usage_source=estimated`. The `retrying` span equals the backoff sleep.

## 9. Errors and Root-Cause Analysis

1. **Admission queue kept cancelled waiters (real bug, found by a test).** A waiter whose client disconnected stayed in the deque. Queue depth was inflated, and later requests could have been shed as `queue_full` against ghosts.
2. **A test hung the first run.** It held two permits without settling them, and the guard's default concurrency of 2 correctly blocked the third acquire forever. This was a test bug; the guard behaved as designed.
3. **A wrong test expectation.** The semaphore test expected an estimate of 2, ignoring that `chars_per_token` had already adapted after the first settlement.
4. **Self-review fixes before the first run:**
   - the circuit check ran only after quota waits, so it would not have failed fast;
   - the half-open trial flag was cleared only in `record_result`, so a cancelled trial call would have left the circuit stuck.
5. **Tooling slips on my side.** Two long inline edit scripts failed shell parsing. Nothing ran in either case; the edits were re-done from script files.
6. **Ruff:** one SIM300.

## 10. Fixes Applied

1. A cancelled waiter is now removed from the queue. The disconnect test asserts depth 0, 0 provider requests and 0 reserved tokens.
2. The test helper defaults to a concurrency of 8; the semaphore test sets 1 explicitly.
3. The assertion now checks that the second acquire proceeds once the slot is freed.
4. The circuit `open` state is checked before any wait. The trial flag clears at settlement.
5. Later runs used a hard time limit, so a hang cannot stall verification.
6. Lint fix.

Re-verification: 372 passed; ruff clean (§7).

## 11. Outstanding Issues

1. **P05 report still pending review.** The owner committed P05 and started P06, but the P05 report is still marked "Pending Review" and its §11.1 decisions are unanswered. P06 builds on those values as proposed.
2. **All P06 defaults (§5 item 11) need owner approval.**
3. **Real limits are not configured anywhere yet.** RPM and TPD are unknown: they are absent from headers and not measured. The guard enforces only what it is given. P07/P08 wiring must supply values from the Groq console, with their source cited.
4. **The local daily counters start at process start.** Usage earlier the same day is visible only through the `remaining-requests` header. There is no header for tokens per day, so TPD protection is local and partial.
5. **Groq's own TPM accounting is unreconciled** (P02-LIVE). The guard's reservation is conservative but unverified against it.
6. **Not yet wired into a service.** The pieces are joined end to end only in tests (admission → pipeline → guard → scripted provider); P08 does the real wiring. Per-request deadlines exist as `deadline_ns` parameters, but nothing sets them outside tests.
7. **The admission prediction uses a static service rate.**

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P06) | Status | Evidence |
| :--- | :--- | :--- |
| Settlement: estimate replaced by usage | `PASS` | `test_settlement_replaces_the_estimate_with_provider_usage` |
| Missing usage keeps estimate, `usage_source=estimated` | `PASS` | `test_missing_usage_keeps_the_reservation_and_is_marked_estimated` |
| Header sync never treats request headers as RPM | `PASS` | `test_header_sync_never_treats_request_headers_as_rpm` |
| Daily-exhausted fast fail with 0 provider calls | `PASS` | `test_daily_exhaustion_fails_fast_with_zero_provider_calls`; pipeline integration test (`provider_called=false`) |
| Retry budget throttling (and on/off switch) | `PASS` | `test_retry_budget_throttles_and_can_be_switched_off` |
| Capacity derivation with explicit inputs | `PASS` | `test_capacity_is_derived_from_explicit_inputs` |
| Queue-full and predicted-wait shedding | `PASS` | `test_queue_is_fifo_and_sheds_when_full`, `test_predicted_wait_beyond_the_deadline_is_shed_on_entry` |
| Disconnect before provider call spends 0 tokens | `PASS` | `test_disconnect_before_the_provider_call_spends_zero_tokens` |
| Semaphore, RPM/TPM windows, local daily counters, safety stop | `PASS` | guard tests (windows, semaphore, daily stop) |
| Adaptive `chars_per_token` | `PASS` | `test_chars_per_token_adapts_conservatively_and_stays_bounded` |
| Full jitter, `retry-after`, deadline share | `PASS` | jitter and decision tests |
| Minimal circuit breaker | `PASS` | circuit test |
| Degraded top-k when headroom is tight | `PASS` | `test_tight_token_headroom_degrades_top_k` |
| Deadline and disconnect checks in admission | `PASS` | `test_deadline_and_disconnect_are_rechecked_when_a_slot_is_reached` |
| Offline, no Groq | `PASS` | no network; 0 Groq calls |

## 13. Remaining Risks

- **Unknown RPM/TPD.** Without configured values the guard cannot prevent minute-request or daily-token 429s. It reacts to them: it honours `retry-after`, and it stops on a daily 429.
- **Conservative reservations reduce throughput.** Reserving the full completion allowance (512 tokens proposed in P05) on an 8,000 TPM limit caps `large_wide` at roughly one or two calls per minute. That is an estimate; P07 measures real tokens per answer.
- **Defaults are untuned.** The retry, budget and circuit numbers have never met real failure patterns. The stand-in experiments (P10) will exercise them.

## 14. Recommended Next Phase

**P07: Model comparison and retrieval-quality evaluation.** It makes real Groq calls and needs explicit owner approval and a call cap. Alternatively, **P08** (service, exact cache, SingleFlight) needs no real calls.

**Owner decisions needed first:** the P05 §11.1 items, the P06 defaults (§5 item 11), and the real RPM/TPD values with their source.

Stopping here; no further phase starts without explicit owner approval.
