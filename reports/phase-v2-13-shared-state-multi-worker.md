# Phase V2-13: Shared state and multi-worker

**Status:** completed, pending owner review. **Date:** 2026-11-21. **Real Groq calls:** 0 (cap 0).

## 1. Objective

Let more than one worker serve The Relay (ARCHITECTURE_V2 D7 and §13; IMPLEMENTATION_PLAN_V2 §V2-13). Three things become shared through a `StateStore`: the exact answer cache, SingleFlight coalescing, and the provider quota counters. The default stays `[state] backend = "memory"`, which needs no Redis. Done when:

- ten identical concurrent requests across two workers make one provider call;
- quota accounting stays within the provider's limit with two workers;
- killing a worker loses no accepted request silently.

## 2. Implementation Summary

- **`relay.control.state`**
  - The `StateStore` protocol covers three areas:
    - **Cache:** get, set and purge.
    - **Flight lock:** acquire, refresh, release and holder.
    - **Flight events:** publish, subscribe and follower count.
    - **Quota:** `reserve_tokens`, `settle_tokens` and `cancel_reservation`.
  - It has two implementations: `InMemoryStateStore`, the reference implementation, and `RedisStateStore`, built on `redis.asyncio`.
  - The Redis store uses atomic Lua scripts for:
    - sliding-window reservation (RPM, TPM and the daily stops, timed by the server's `TIME`);
    - settle and cancel;
    - lock refresh and release, which only the token holder can do;
    - publish: append to the flight log, then announce on Pub/Sub, in one step.
  - `SharedQuota` is one lane's view of the shared quota. A reservation completes even if its caller is cancelled. A reservation whose caller has gone is given back.
- **Cache (`ExactCache`)**
  - `share(store, ttl_s)`, plus async `lookup` and `admit`.
  - Without a shared store, these are today's `get` and `put`.
  - A read failure counts as a miss.
- **`ProviderGuard`**
  - `share(SharedQuota)`. `acquire` reserves in the shared window after the local checks pass, and waits for it or refuses it within the request's deadline.
  - `settle` corrects the shared entry to the actual usage. A reservation whose call never happened is cancelled.
  - `drain_shared` waits for pending settlements at shutdown.
- **`POST /v1/ask`**
  - With a state store, a new flight first claims the lock `flight:{sha256(question, setting, buffered)}`. The TTL is 30 s and the leader renews it every TTL/3.
  - **Leader:** mirrors every channel event, in order, to the log `flight:log:{key}:{token}` and announces it on `flight:events:{key}:{token}`. It releases the lock after its last event.
  - **Follower on another worker:** relays the log into its local channel. Pub/Sub only wakes it to read again. Followers on the same worker join that channel as before, and the final reads `cache_status: coalesced`.
  - **Silence:** a follower whose flight goes quiet checks the lease. If it is held by someone else or by nobody twice in a row, with nothing new in the log, the follower ends with `stopped {reason_class: leader_lost, partial}` and a final `internal_error` with `shed_reason: leader_lost`.
  - **Leader's own client leaves:** the leader is not cancelled while followers on other workers still listen. The admission queue keeps it too.
- **Settings and wiring**
  - The `[state]` settings: `backend`, `redis_url`, `key_prefix`, `flight_lock_ttl_s`, `lease_poll_s`, `cache_ttl_s` and `socket_timeout_s`.
  - `compose.open_state` pings Redis at start-up and fails with a `ConfigurationError` that never shows credentials. `share_state` wires the cache and every lane's guard. The start-up note reads `state: ...`.
  - A deleted collection's shared answers are purged.
- **Dependencies:** a `redis` extra (`redis>=5`). The dev dependencies add `redis` and `fakeredis[lua]`. Both are optional: the default path never imports `redis`, as a test checks.

## 3. Files Created

- `src/relay/control/state.py`
- `tests/test_multi_worker.py` (35 tests)
- `reports/phase-v2-13-shared-state-multi-worker.md`

## 4. Files Modified

- `src/relay/control/cache.py`
- `src/relay/control/provider_guard.py`
- `src/relay/api/routes/ask_v1.py`
- `src/relay/app/settings.py`
- `src/relay/app/compose.py`
- `relay.example.toml`
- `pyproject.toml`
- `ENGINEERING_LOG.md` (appended)

## 5. Technical Decisions (rulings)

1. **Redis is approved for this phase.** The phase prompt explicitly names `redis` and `fakeredis`, but CLAUDE.md §4 lists Redis as restricted and §4.1 doesn't include it. Both are optional extras, not core dependencies. **The owner should add them to CLAUDE.md §4.1.**
2. **The memory backend runs today's objects unchanged:** the cache's dict, `ReplayChannel` and the guard's deques. The interface is not threaded through the default path, which guarantees identical default behaviour. `InMemoryStateStore` passes the same contract tests as Redis and also lets two in-process workers share state.
3. **Pub/Sub plus a per-flight log.** Pub/Sub alone loses events published before a follower subscribes. Followers subscribe first, then read the log, and each message is only a wake-up. The log is kept for 60 s after release, so a follower that subscribes late still sees the whole flight.
4. **The signatures differ from the prompt's.**
   - `acquire_flight_lock` returns a `FlightLease(owned, token)` rather than a bool. The token names the leader's log and channel, and stops a late leader from releasing a newer leader's lock.
   - `reserve_tokens` returns a `QuotaDecision` (reservation, wait in ms, or refusal) rather than a bool, so the guard can wait correctly or refuse because of the daily stop.
5. **A dead leader ends with an explicit error, not a takeover.** A follower that already relayed sentences cannot take over without sending a second, different answer. The lock expires by TTL, so the next request leads. A takeover before any sentence was relayed is possible later.
6. **The failure policy differs by part.** The cache and coalescing fail open: a read failure is a miss, and a failed claim means this worker leads alone. The quota fails closed, with `SharedQuotaUnavailableError`, `error_code` `internal_error`. The frozen `ErrorCode` is unchanged.
7. **Some things stay per worker:**
   - `ProviderGuard.predict`, which the router uses, and the concurrency semaphore;
   - header sync.

   The shared window is authoritative when a call is admitted.
8. **v1's `POST /ask` and the v1 `SingleFlight` class stay in-memory only.** They are the frozen path behind `results.csv`.

## 6. Commands Actually Executed

- `pip install "redis>=5" "fakeredis[lua]>=2.20"` installed redis 8.1.0, fakeredis 2.40.0 and lupa 2.8.
- A fakeredis feature probe: Lua `TIME`, `SET NX PX` expiry, async Pub/Sub and `SCAN MATCH` all worked.
- `pytest -q` on the related existing tests: 102 passed.
- `pytest -q tests/test_multi_worker.py`
- The mutation check script (§7).
- `bash .claude/skills/relay-verify/verify.sh`, twice.

## 7. Test Results

**Final verification:**
- pytest **1007 passed** in 105.07 s;
- ruff clean;
- Jest **60 passed**;
- tsc exit 0, eslint exit 0;
- `results.csv` sha256 `8dfb10dfaadb`, OK;
- 0 changes under `evidence/` and `results.csv`.

The first full run, before the review fixes, had 1000 passed.

**`tests/test_multi_worker.py`:** 35 passed. Most tests run twice, against fakeredis and against the in-memory store.

- **Test 1, shared cache:** worker 1 answers; worker 2 gets `exact_hit` with 0 model calls and the same sentences.
- **Test 2, distributed SingleFlight:** 10 concurrent identical requests, alternating between the two workers, make exactly **1 provider call** in total.
  - All 10 finish `ok`/`VERIFIED` with identical sentences.
  - Cache statuses are 9 × `coalesced` and 1 × `miss`. All 5 requests on the worker that made no call followed the other worker's flight.
- **Test 3, global quota:**
  - **Guard level:** two guards each set to 10 RPM share one window. After 10 calls each guard's own window would still admit at once, yet the 11th is refused with `QuotaWaitExceedsDeadlineError`.
  - **Worker level:** 11 different questions across 2 workers make 10 provider calls. The 11th ends `predicted_wait_exceeds_deadline`, with `provider_called` false.
  - **Fail closed:** a severed store is refused with `internal_error`.
  - **Atomicity (contract):** 40 concurrent reservations against 10 RPM grant exactly 10.
- **Test 4, leader crash:** the leader's connection to the shared store is cut on the follower's first sentence.
  - The follower ends within 3 s with `error`/`internal_error`/`leader_lost`, `partial`, and `stopped: leader_lost`, having received only the verified first sentence.
  - The next request takes the expired lock and is answered (`miss`, 1 call).
  - A follower whose leader finishes normally is never cut off.
- **Test 5, memory default:**
  - `backend` defaults to `memory`, and `open_state` returns None.
  - A subprocess import shows `redis` is never imported on the default path.
  - Default workers keep the in-process cache and guard and coalesce 3 → 1.
  - An unreachable Redis fails start-up without printing its password.
- **Contract tests:** cache round trip, TTL expiry and purge by collection; one lock holder, expiry, and refresh and release by the holder only; a late subscriber replays every event in order; settle, the TPM wait, `payload_too_large`, the daily token and request stops, the day key, and cancel giving back the minute slot and daily count.
- **Review regressions:** a queued leader keeps serving its remote follower; a reservation cancelled in flight is given back; socket timeouts are set.

**Mutation check.** Each mechanism was disabled in turn, and the multi-worker tests were run:

| Mechanism disabled | Tests failing | Which tests |
| :-- | :-- | :-- |
| Shared flights | 6 | Tests 2 and 4, and the normal-finish test |
| Guard sharing | 6 | Every quota test |
| Shared cache lookup | 2 | Test 1 |
| Leader-lost detection | 2 | Test 4 |

The files were restored afterwards.

## 8. Actual Output and Generated Artifacts

No run directories, no evidence and no `results.csv` change. The new code, tests, configuration and this report are the only outputs.

## 9. Errors and Root-Cause Analysis

- **A bash heredoc broke** on quoting in the patch script. Written as a Python file instead; nothing had been applied.
- **pydantic rejected a dict as a `ChatMessage`** in a test helper (strict model). The helper now builds `ChatMessage`.
- **fakeredis `get_message` returned None at first.** That was the swallowed subscribe confirmation, so `_wake` loops until its deadline.
- **Code review (fresh-context agent), 5 important findings:**
  1. **Queued leader shed.** A queued leader whose own client left was shed by admission as `client_gave_up`, although followers on other workers were listening. Confirmed RED: the follower got `client_gave_up`.
  2. **No Redis socket timeouts.** A partitioned Redis could hang a call.
  3. **Reservation lost on cancel.** A reservation made while its caller was cancelled was never given back. Confirmed RED: the next reservation had to wait 59.6 s.
  4. **Aborted reservation kept its slot.** A reservation aborted after it was granted (open circuit) kept its RPM slot and daily request.
  5. **Settlements not drained** at shutdown.

  Also accepted from the low-confidence list:
  - Lua `unpack` has a stack limit on very large expired batches.
  - The day used by a correction could roll over during the reserve.

## 10. Fixes Applied

1. `_abandon` records `remote_followers`, and the admission check treats them as connected. Regression test went RED, then GREEN.
2. `socket_timeout` and `socket_connect_timeout`, configurable as `socket_timeout_s` (5 s), plus a health check. Test added.
3. `SharedQuota.reserve` shields the call; a reservation granted to a caller that has gone is cancelled. Regression test went RED, then GREEN.
4. A new `cancel_reservation` (Lua: `ZREM`, `HDEL`, `DECR`, `DECRBY`) replaces settle-to-0 on the abort path. Contract test went RED, then GREEN.
5. `drain_shared` is pushed on the exit stack, so settlements land before Redis closes.
6. Batched `HDEL`, 500 at a time.
7. The reservation's own day is used for its permit and its cancellation.

## 11. Outstanding Issues

- **Coalescing races:**
  - If a request is cancelled while it is claiming the lock, the lock stays for up to one TTL with no leader. Followers that join meanwhile end with `leader_lost`.
  - `_abandon` decides once. If remote followers leave later, the flight runs to completion and is cached, which costs one generation.
  - The admission check can still race with `_abandon`'s follower count, in the few milliseconds of the check.
- **Per-worker state:**
  - A deleted collection is known only to the worker that deleted it. Shared answers are purged, but another worker's in-flight answer can still be cached; its key carries the old collection version, so it is unreachable.
  - Per-tenant buckets in the admission queue, and the API keys' daily limits, remain per worker. They are not in the V2-13 scope.
- **v1 `POST /ask` keeps its in-memory cache** while `/v1/ask` uses Redis. The two caches are separate.
- **Not exercised:**
  - No real Redis server and no real multi-process run were used; everything ran on fakeredis in one process. A Compose run with two `relay serve` processes and Redis is the remaining manual check.
  - The ingestion worker loop runs in each process. Claiming jobs from several processes was not tested.
- **CI:** the workflow installs `.[dev]`, which now includes fakeredis. It has not run on GitHub yet.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| `StateStore` protocol with in-memory and Redis implementations | Met |
| 10 concurrent requests across 2 workers → 1 provider call | Met (test 2, both backends) |
| Global quota respected across workers | Met (test 3; 40-way atomicity) |
| Killing a worker loses no accepted request silently | Met for followers on other workers (explicit `leader_lost`). Requests held by the killed process itself lose their connection. |
| Default memory backend works with zero Redis dependency | Met (subprocess import check) |
| 972+ backend and 60 UI tests pass; ruff, tsc and eslint clean | Met: 1007 / 60 |
| Zero real Groq calls; `evidence/` and `results.csv` untouched | Met |

## 13. Remaining Risks

- **Clock and TTL assumptions:**
  - The window uses Redis server time, but each guard's local window uses its host clock. Skew only makes the local check stricter or looser for that worker's own calls; the shared check is authoritative.
  - If the leader's event loop stalls for longer than `flight_lock_ttl_s`, its followers declare it lost while it is still working.
- **Quota accounting:**
  - The router forecasts lanes from the local view, so a lane can look free and then wait on the shared window. That is bounded by the deadline.
  - Pending settlements that never land keep the larger reservation, which errs on the safe side.

## 14. Recommended Next Phase

Owner review of this phase and the rulings above. Then add Redis to CLAUDE.md §4.1. Optionally, a manual two-process Compose check with a real Redis. Every planned phase other than V2-13 is now done. V2-12's owner actions are still pending (reviewing the 80 answers, the 100 questions).
