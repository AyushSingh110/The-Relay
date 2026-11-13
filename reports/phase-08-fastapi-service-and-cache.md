# Phase 08 Implementation Report — FastAPI Service, Exact Cache and SingleFlight

> **Phase ID:** `p08`
> **Date:** `2026-11-13 07:27 IST`
> **Status:** `Completed`. Verified in Conda env `the-relay` (Python 3.12.16): 409 passed, 0 failed, 0 skipped; ruff clean. **0 Groq calls. No network use beyond one loopback smoke check.**
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P08 and the owner's phase prompt, build the service layer:

- `create_app` wiring pipeline + ProviderGuard + admission queue + exact cache + SingleFlight, for one worker process;
- `POST /ask` answering as SSE, with a final event that carries server spans and request metadata;
- client-disconnect propagation;
- the daily-exhausted behaviour: cache hits are served; misses get an honest "unavailable" reply with passage pointers and no provider call;
- server traces written to the run directory;
- an exact in-memory cache that admits only verified, non-degraded answers and is reset per run.

Also requested: mark the P05 and P06 reports `Owner Approved (2026-11-13)`. Real Groq calls: none.

## 2. Implementation Summary

Request path (ARCHITECTURE.md §5, §7, §8):

```
POST /ask -> validate -> exact cache --hit--> answer (exact_hit, 0 tokens)
                             | miss
                         SingleFlight --identical in flight--> wait for leader (coalesced, 0 tokens)
                             | leader (detached task)
                         admission queue -> pipeline (scope, tool, gate, guard, one LLM call, verifier)
                             -> cache.put if VERIFIED / REFUSAL_VERIFIED and not degraded
```

- **`control/cache.py`**
  - `ExactCache`: key is the SHA-256 of the normalised question, scoped by `(corpus_sha256, retrieval_setting, prompt_version, model_id)`. `put` refuses any verdict other than `VERIFIED` / `REFUSAL_VERIFIED`, and refuses degraded answers. LRU-bounded. `clear()` empties it.
  - `SingleFlight`: the leader's work is a detached `asyncio` task that waiters await through `asyncio.shield`. A waiter that leaves only decrements the waiter count. The task is cancelled when the count reaches zero, and the key is unregistered first so a new caller starts a fresh flight.
- **`api/app.py`**
  - `RelayService.ask` runs cache → SingleFlight → admission → pipeline for one request.
  - The answer stays buffered and verified before delivery (unchanged P05 pipeline), so the response is a short SSE document written in one piece: `answer` (status `ok`) or `unavailable` (any other status), then `final`.
  - `final` uses the `results.csv` column names, so the harness can build a `ResultRow` from it plus its own client timing.
  - A watcher task reads the ASGI `http.disconnect` message. On disconnect the request is cancelled; its flight is cancelled only if no follower remains.
  - Traces: one `request` record per HTTP request and one `flight` record per piece of leader work, through `RunHandle.append_trace`. The server writes no `results.csv` rows.
  - `RelayService.start_run(run)` clears the cache and redirects traces.
  - `GET /healthz` reports queue, cache, flight, guard and trace-failure state.
- **Daily exhaustion** needs no special branch: the cache is checked first, so hits are served; a miss runs retrieval, the guard refuses locally (`daily_budget_exhausted`, 0 attempts), and the service returns `unavailable` with pointers to the retrieved passages (IDs, pages and citation string, no passage text).

Constraints honoured: at most one LLM call per request, no hidden retries, single worker, exact cache only (no semantic matching), only `scripts/build_results.py` writes `results.csv`.

## 3. Files Created

- `src/relay/control/cache.py` — `ExactCache`, `CacheScope`, `CachedAnswer`, `SingleFlight`, `normalise_question`, `question_hash`, `corpus_digest`.
- `src/relay/api/__init__.py` — package exports.
- `src/relay/api/app.py` — `create_app`, `build_app`, `RelayService`, `AskRequest`, `AskReply`, `PassagePointer`, `MeteredProvider`, SSE rendering.
- `tests/test_cache_and_api.py` — 37 offline tests.
- `reports/phase-08-fastapi-service-and-cache.md` — this report.

## 4. Files Modified

- `src/relay/telemetry/spans.py` (P01 module) — **additive only**: new read-only method `ServerSpanTracker.snapshot()`. No existing line changed. Reason in §5.8.
- `reports/phase-05-pipeline-and-verifier.md`, `reports/phase-06-provider-guard-and-admission.md` — `Owner Approval` line set to `Owner Approved (2026-11-13)`.
- `ENGINEERING_LOG.md` — two appended entries (approvals; P08).

`ARCHITECTURE.md` was not edited. Its `[PROPOSED]` tags on the now-approved P05/P06 defaults are unchanged.

## 5. Technical Decisions

All items below are implementation choices inside the approved architecture and are **[PROPOSED]** until the owner accepts them.

1. **Question normalisation: NFKC, whitespace collapsed, trimmed; case preserved.** `What is N?` and `what is n?` are different keys. An exact cache should not merge questions that differ in an identifier's case. Cost: fewer hits on case-only duplicates.
2. **`cache_mode=off` disables SingleFlight as well.** Every request runs its own pipeline and reports `cache_status=off`. This keeps cache-off runs free of any deduplication, which the cache-on vs cache-off comparison and the retrieval-quality runs need.
3. **Early refusals are not cached.** §8 admits "verified (or verified refusals)" only, so `EARLY_REFUSAL` is rejected along with `BLOCKED` and `NOT_RUN`. They cost no tokens anyway.
4. **HTTP status follows the outcome:** `ok` 200, `rate_limited` 429, `timeout` 504, `error` 502, `internal_error` 500, invalid request 422 (JSON body). The body is SSE in every non-422 case. A disconnected client gets status 499 with no body, which nobody receives.
5. **`unavailable` is not an answer.** It is sent for every non-`ok` status and must not be counted as a delivered first token. It carries passage pointers whenever retrieval had run (daily exhaustion, circuit open, upstream failure), and none when the request was shed.
6. **Follower rows.** A coalesced follower reports 0 tokens, 0 retries, `provider_called=false`, `usage_source=none`, blank `provider_first_token_ms`, blank `queue_depth_at_entry` and `retrieval_ms` (it never queued or retrieved), and all its server time as `waiting_ms`. The verdict, `degraded` flag and `model_id` are the leader's.
7. **Deadlines.** `deadline_ms` is optional and counted from the request's arrival. The flight runs under the leader's deadline. A follower bounds only its own wait and gets `timeout / deadline_expired` if its deadline passes first. Followers share the leader's result, including a failure.
8. **`ServerSpanTracker.snapshot()`.** A leader's tracker is driven by its flight task. If that client gives up while followers keep the flight alive, the leader's trace still needs its spans, and `finish()` would close a tracker that is still in use. `snapshot()` reads the totals without closing.
9. **The `flight` trace record is the authority for provider spend.** A leader that gave up cannot know the final spend, so its `request` record points to the flight record. When a flight is cancelled, calls already sent are reported as `provider_called=true` with estimated tokens (3.0 chars/token) rather than zero. `MeteredProvider` counts calls and characters per flight through a `ContextVar`.
10. **Trace write failures are counted and logged, not raised.** The answer is still delivered; `trace_failures` appears in `/healthz`.
11. **Cache bound:** 1024 entries, least-recently-used eviction.
12. **`create_app` takes built components** (`provider`, `retrievers`, `guard`, `queue`, `corpus_sha256`, …). No limit is assumed inside it.

## 6. Commands Actually Executed

```bash
# inspection
git status --short
sed -n 151,175p IMPLEMENTATION_PLAN.md
sed -n 104,156p ARCHITECTURE.md ; sed -n 176,210p ARCHITECTURE.md ; sed -n 304,341p ARCHITECTURE.md
cat src/relay/telemetry/spans.py src/relay/config.py
grep / sed over provider_guard.py, run_store.py, tools.py, base.py, tests/*.py, pyproject.toml

# approvals: python heredoc replacing the Owner Approval line in the P05 and P06 reports

# implementation checks
python scratchpad/p08_spans.py            # adds snapshot() to spans.py
python scratchpad/p08_edit1.py            # adds model_id to RelayService
python -c "import relay.api.app"
python -m ruff check src/relay/api src/relay/control/cache.py src/relay/telemetry/spans.py
timeout 300 python -m pytest tests/test_cache_and_api.py -q -x
for i in 1 2 3 4 5; do timeout 120 python -m pytest tests/test_cache_and_api.py -q; done
timeout 60 python scratchpad/p08_uvicorn_smoke.py

# final verification (after the last source edit)
timeout 900 python -m pytest -q
python -m ruff check .
```

`python` is `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`. One early `bash` heredoc failed to parse ("unexpected EOF") and wrote nothing; the files were then written directly.

## 7. Test Results

Run after the final source edit:

- `python -m pytest -q`: **409 passed, 0 failed, 0 skipped** in 5.63 s (372 before P08 + 37 new).
- `python -m ruff check .`: **All checks passed!**
- `tests/test_cache_and_api.py` run five more times in a row: 37 passed each time (1.21–1.27 s).

Acceptance criteria and the tests that cover them:

| Criterion | Test | Result |
| :-- | :-- | :-- |
| 10 concurrent identical misses → 1 provider call, 9 coalesced rows with 0 tokens | `test_ten_identical_concurrent_misses_make_one_provider_call` | pass: 1 call; 1 `miss` + 9 `coalesced`; all 10 finals build valid `ResultRow`s; token sums equal the single usage (900 / 40) |
| One follower disconnecting does not cancel the leader | `test_follower_disconnect_does_not_cancel_the_leader` | pass: follower gets 499, 0 provider cancellations, leader and other follower get 200 |
| Blocked answers never cached | `test_blocked_answers_are_never_cached` | pass: 2 identical requests → 2 provider calls, cache empty |
| Degraded answers never cached | `test_degraded_answers_are_never_cached` | pass: 2 identical requests → 2 provider calls, cache empty |
| Daily-exhausted path | `test_daily_budget_exhausted_serves_hits_and_points_to_passages` | pass: hit served (200); miss gets 429 `unavailable` with 4 pointers; provider calls stay at 2 |

Other behaviour tested: leader disconnect with a follower still waiting; sole client disconnect cancels the provider call and frees the queue slot; follower deadline; cache off; early refusal; full queue shed; upstream failure after retries; retrieval failure; eight invalid payloads; `start_run`; trace write failure; `httpx.ASGITransport`; cache and SingleFlight unit tests; `snapshot()`.

## 8. Actual Output and Generated Artifacts

All output below is offline, from scripted providers. Nothing here is a Groq measurement.

- **No evidence or result artifacts were generated.** `evidence/` and `results.csv` are untouched. Test traces went to pytest temporary directories.
- **Loopback smoke check** (scratch script, real `uvicorn` on `127.0.0.1`, scripted provider held open):

```
client gave up after 0.3 s (ReadTimeout)
provider call cancelled: 1 | in flight: 0 | queue active: 0
status: 200 | text/event-stream; charset=utf-8
event: answer\ndata: {"citations": [{"chunk_id": "A_p3_c0", " ...
health: {'status': 'ok', 'run_id': None, 'cache_mode': 'exact', 'cache_entries': 1, 'in_flight': 0,
         'queue_depth': 0, 'queue_active': 0, 'queue_capacity': 16, 'daily_stop': None,
         'circuit': 'closed', 'trace_failures': 0}
```

  This confirms that disconnect detection works under a real ASGI server, not only under the test driver. It is a one-off check, not part of the test suite.

- **Response shape:**

```
event: answer
data: {"citations": [{"chunk_id", "page_end", "page_start", "paper_id", "tag"}], "refused": false, "text": "..."}

event: final
data: {"cache_status", "degraded", "error_code", "model_id", "provider_called", "provider_first_token_ms",
       "queue_depth_at_entry", "request_id", "retrieval_ms", "retrieval_setting", "retries",
       "retrying_ms", "server_total_ms", "status", "tokens_in", "tokens_out", "usage_source",
       "verification_verdict", "waiting_ms", "working_ms"}
```

## 9. Errors and Root-Cause Analysis

1. **Bash heredoc parse failure** ("unexpected EOF while looking for matching quote") on the first attempt to write `cache.py` and the spans edit in one command. Root cause: the known shell pitfall with long quoted Python inside heredocs. Nothing was written by the failed command.
2. **Design gap found while writing the service:** `RelayService` first took the model id for cancelled-flight records from the cache scope, which would have been blank with the cache off. Found by reading, before any test ran.

No test failed during the phase: the new test file passed on its first run.

## 10. Fixes Applied

1. Files written with the file-writing tool and scratch scripts instead of heredocs. Verified by import check, ruff and tests.
2. `RelayService` takes an explicit `model_id`. Covered by `test_sole_client_disconnect_cancels_the_provider_call` (`model_id == "test-model"` in the cancelled flight record).

## 11. Outstanding Issues

1. **No production composition root.** Nothing yet loads the real indexes, builds the provider from `Settings`, or picks guard limits and queue capacity. Those need values that are still open (RPM and TPD, final model, patience quantile). `create_app` is ready to receive them; P10 has to supply them.
2. **The server writes traces, not rows.** The harness (P10) must build rows from the `final` event, and for a client that gave up it must read the server traces.
3. **Abandoned leader spend.** The harness must take a gave-up leader's tokens from the `flight` record. If a leader disconnects before its flight task has run at all, there is no flight record; that means zero spend.
4. **Cancelled-flight tokens are estimates** at 3.0 chars/token for every call the flight made, even if an earlier attempt had reported real usage.
5. **Followers inherit the leader's deadline and failures** (§5.7). A follower with a longer deadline can receive the leader's `deadline_expired`.
6. **Trace appends are synchronous file writes on the event loop.** They are small, but they are not offloaded.
7. **`start_run` must be called while no request is in flight**; this is documented, not enforced.
8. **P07 has not been run** (real Groq evaluation; needs owner approval, limits and a model choice).
9. **`ARCHITECTURE.md` still tags the approved P05/P06 defaults as `[PROPOSED]`.**

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| 10 concurrent identical misses → 1 provider call, 9 coalesced rows with 0 tokens | Met |
| One follower disconnecting does not cancel the leader | Met |
| Blocked answers never cached | Met |
| Degraded answers never cached | Met |
| Daily-exhausted path (hits served; misses unavailable with pointers, 0 provider calls) | Met |
| `create_app` wiring pipeline + guard + admission + cache + SingleFlight | Met (components supplied by the caller; see §11.1) |
| `POST /ask` SSE with a final event carrying spans and metadata | Met |
| Disconnect propagation | Met (ASGI driver tests and loopback uvicorn check) |
| Server traces to the run directory | Met |
| Cache reset per run | Met (`start_run`) |
| pytest and ruff after the final edit | Met: 409 passed; ruff clean |
| Real Groq calls | 0 |

## 13. Remaining Risks

- **Stale answers across configuration changes** are prevented only by the cache scope (corpus, setting, prompt version, model). A change to retrieval code with the same corpus and setting name would not change the key within a process; `start_run` clears it between runs.
- **Case-sensitive keys** may lower the hit rate in the cache-on load scenario if the workload varies case.
- **A burst of followers behind a slow leader** all fail together if the leader fails. This is inherent to coalescing and should be visible in the burst scenario results.
- **Disconnect detection depends on the ASGI server delivering `http.disconnect`.** Verified with uvicorn 0.54.0 on loopback only.
- **Token estimates for cancelled calls** are conservative guesses, not provider usage; they are labelled `estimated`.

## 14. Recommended Next Phase

**P09 — Stand-In Service and Calibration**, which needs no real Groq calls. Before or alongside it, the owner should decide:

1. the §5 items above (in particular 1, 2, 4 and 7);
2. whether `ARCHITECTURE.md` should be updated to mark the P05/P06 defaults as owner-decided;
3. the still-open inputs for the composition root and P07: RPM and TPD with their source, the final model, and the patience distribution.

No next phase has been started.
