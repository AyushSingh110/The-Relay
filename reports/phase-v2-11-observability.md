# Phase V2-11: Observability

- **Date:** 2026-11-21 IST
- **Status:** completed, pending owner review.
- **Real Groq calls:** 0 (cap 0). Every test runs offline with a scripted provider, an
  in-memory store and httpx `MockTransport`.
- **Frozen evidence:** `results.csv` sha256 still starts with `8dfb10dfaadb`. `git status`
  shows no change under `evidence/`.

## 1. Objective

The operator can see what the service is doing (ARCHITECTURE_V2 §11):

- Prometheus metrics at `/metrics`;
- OpenTelemetry spans aligned with v1's waiting, working and retrying spans;
- `GET /v1/requests/{id}` with the time breakdown and trace;
- sanitised transport-error categories;
- a starter dashboard.

The plan's "done when" asks for two things:

- the overload scenario is readable from the dashboard alone (queue depth, shed rate, quota
  headroom, first-sentence latency);
- no metric or span holds a secret or question text beyond a hash.

## 2. Implementation Summary

**Dependencies.** `prometheus-client>=0.20` and `opentelemetry-sdk>=1.24` are in
`[project.dependencies]` (approved, CLAUDE.md §4.1). Installed: 0.26.0 and 1.45.2.

**`telemetry/metrics.py`.** A per-app registry with:

- `relay_requests_total{status, error_code, tenant, lane}`;
- `relay_request_latency_seconds{stage=first_sentence|ttft|total, lane}`, with buckets
  0.05 to 30 s;
- `relay_tokens_total{direction, lane}`, taken from each request's cost ledger, so a
  rewrite and an escalation count on their own lanes;
- `relay_cache_events_total{event=hit|miss|coalesced}`;
- `relay_verifier_blocks_total{layer=L1|L2|L3|L4}`;
- live gauges read at scrape time: `relay_queue_depth{priority, tenant}` and
  `relay_quota_headroom_tokens{lane}` (no sample when a lane has no known TPM limit).

`GET /metrics` serves the Prometheus text format. Both v1 `/ask` and `/v1/ask` are
counted. The token spend of an answer whose client left early is counted too.

**`telemetry/tracing.py`.** The app has its own `TracerProvider`. A bounded in-memory span
store (`RecentSpans`, the last 2,000 spans) is the local exporter; `add_exporter()` can add
OTLP or a console exporter.

`RequestTrace` creates spans with explicit parents, because the work runs across tasks and
async generators:

| Span | Attributes |
| :-- | :-- |
| `request` | `request_id`, `tenant_id`, `question_hash`, `collection_id`, priority, follow-up; at the end: status, cache, lane, cost, escalation, coalescing, verdict, and v1's `waiting_ms`, `working_ms`, `retrying_ms` |
| `pipeline.rewrite` | method, rewritten, fallback, tokens, `standalone_hash` |
| `queue.wait` | `priority`, `queue_position`, over rate, shed |
| `pipeline.retrieve` | `chunks_found`, `preset` |
| `pipeline.generate` | `lane`, `model`, `tokens_in`, `tokens_out`, escalated, fallback |
| `verifier.sentence` | `sentence_idx`, `l1_pass`, `l2_pass`, `l3_pass`, `l4_score`, `blocked_layer` |
| `pipeline.verify` | the whole-answer pass, and buffered L4 scoring |

The **waterfall is computed from these spans' own durations,** so it always matches the
trace. Work on a trace that is already finished is a silent no-op. That happens when the
leader's client leaves while a follower keeps the flight going.

**`GET /v1/requests/{id}`** (`api/routes/requests.py`):

- tenant-scoped: another tenant, or an unknown id, gets 404; no key gets 401;
- returns the summary (`request_id`, `question_hash`, status, verdict, lane, `cost_usd`,
  `created_at`, cache status), the waterfall, the tokens, the verifier counts
  (checked, passed, partial) and the spans;
- reads from a new `request_log.breakdown` JSON column (migration **8**), written by the
  request-log sink.

**Transport categories.** `transport_category()` sets `ProviderError.category` to one of:
`DNS_FAILURE`, `CONNECT_TIMEOUT`, `TLS_ERROR`, `READ_TIMEOUT` or `STREAM_DISCONNECT`, plus
`CONNECT_FAILURE` and `OTHER_TRANSPORT`.

- The category is decided from exception types and their cause chain (`socket.gaierror`,
  `ssl.SSLError`, httpx classes). DNS is also recognised from the resolver's own text, which
  is inspected and never logged.
- `sanitize_error` now appends it, for example
  `ProviderConnectionError(category=DNS_FAILURE)`, so every existing log line that uses it
  is categorised. A stream ending before `[DONE]` is `STREAM_DISCONNECT`.

**UI.** The Status page refreshes every 5 s. A new **Quota headroom** table, one row per
lane, comes from the new `/healthz` `quota` list and replaces the "coming in V2-11" row. It
shows tokens left this minute against the limit, requests today, and the daily-stop and
circuit state.

**Dashboard.** `docs/dashboards/starter_dashboard.json` is a Grafana dashboard with 8 panels:

- QPS by status;
- delivery rate and shed rate (%);
- queue depth by priority;
- quota headroom per lane;
- first-sentence p50 and p95;
- total latency p50 and p95;
- verifier rejections by layer;
- tokens by lane, and the cache share.

**Workflow tooling (the owner's request to save tokens):**

- a project skill, `.claude/skills/relay-verify`, whose `verify.sh` runs every
  end-of-phase check and prints only a summary;
- 3 memory notes: environment, workflow, and progress with cross-phase conventions.

## 3. Files Created

- `src/relay/telemetry/metrics.py`
- `src/relay/telemetry/tracing.py`
- `src/relay/api/routes/requests.py`
- `docs/dashboards/starter_dashboard.json`
- `tests/test_v2_11_observability.py`: 19 tests
- `.claude/skills/relay-verify/SKILL.md` and `verify.sh`
- `reports/phase-v2-11-observability.md` (this file)

## 4. Files Modified

- `pyproject.toml`: the two dependencies.
- **Providers:**
  - `src/relay/providers/errors.py`: `TransportCategory` and `ProviderError.category`;
  - `src/relay/providers/http_provider.py`: `transport_category` and the mapping;
  - `src/relay/errors.py`: `sanitize_error` adds the category.
- `src/relay/api/app.py`: `RelayService.metrics` and `tracing`, `GET /metrics`, the
  `/healthz` `quota` list, and v1 `/ask` in the metrics.
- `src/relay/api/routes/ask_v1.py`:
  - the spans;
  - `_respond` now wraps `_answer` with `aclosing`;
  - breakdowns in the request records;
  - metrics through `AskV1Context.record`;
  - the block-layer metric;
  - the cancelled flight's spend in the metrics.
- `src/relay/control/admission.py`: `depth_by_class` and the waiter's priority.
- **Store:** `src/relay/store/migrations.py` (migration 8) and `src/relay/store/repositories.py`
  (`breakdown` and `get_by_request_id`).
- `src/relay/app/request_log.py`: the breakdown is mapped to JSON.
- `src/relay/app/compose.py`: the requests router is mounted.
- **Web:** `web/src/api.ts`, `web/src/pages/StatusPage.tsx`, `web/src/styles.css`,
  `web/src/test/fakeApi.ts`, and `web/src/__tests__/pages.test.tsx` (the placeholder test
  is replaced, plus a refresh test).
- `tests/test_v2_10_tenants.py`: the noisy-neighbour measure, see §9.
- `src/relay/app/static/*`: the rebuilt UI.

## 5. Technical Decisions

These are my rulings. Each says what it costs if wrong.

1. **`/metrics` is unauthenticated, like `/healthz`,** because Prometheus scrapers usually
   carry no key. Its labels include tenant ids, so it **must stay on a private network**: the
   service binds loopback by default. Cost if wrong: anyone who can reach the port sees
   tenant ids and traffic volume, but no content.
2. **`relay_verifier_blocks_total{layer="L4"}` is exported but always 0.** L4 is advisory
   and never blocks; layer L4 is shown so the dashboard makes that visible.
3. **The verifier layers follow the V2-07 checks:**
   - L1, structure: empty answer, no citations, an uncited sentence;
   - L2, grounding: unknown tag, paper, arXiv or page mismatch, ungrounded number or
     identifier;
   - L3, refusal form.

   A truncated answer is not a verifier block.
4. **Waterfall stages are span durations,** so they match the trace exactly. In streaming,
   per-sentence checks happen during generation and are inside `generation_ms`.
   `verification_ms` is the whole-answer pass; in buffered mode it also includes L4 scoring.
   The sending of `accepted` and the flight start are in `total_ms` but in no stage, so the
   stages do not add up to the total.
5. **The request view comes from `request_log`,** which the sink writes in the background.
   A request that has just ended can take a moment to appear.
6. **The tenant label is used as asked.** Cardinality is bounded by the number of tenants.
7. **Two categories were added to the prompt's five:** `CONNECT_FAILURE` (for example,
   refused) and `OTHER_TRANSPORT`, so nothing is put in the wrong category.

## 6. Commands Actually Executed

The Python interpreter is `C:/Users/ASUS/anaconda3/envs/the-relay/python.exe`.

- `pip install "prometheus-client>=0.20" "opentelemetry-sdk>=1.24"`: installed 0.26.0 and
  1.45.2.
- After wiring tracing into `/v1/ask`: `pytest -q`: **934 passed**, so existing behaviour was
  unchanged.
- `pytest tests/test_v2_11_observability.py`:
  - first run: 1 failed, 15 passed, explained in §9;
  - then 16 passed.
- UI tests before the change: 2 failed (RED). After: `npx jest` **55 passed**.
- Code review: 4 low-confidence points. 3 regression tests were added; 1 was RED before its
  fix, see §9. Then 19 passed.
- Checks that the tests can fail. Each run monkeypatches the code through a pytest plugin
  module kept in the scratchpad:

  | Plugin | What it does | Result |
  | :-- | :-- | :-- |
  | `mut_nometrics` | switches metrics off | 2 failed |
  | `mut_leak` | puts the question text on the root span | 1 failed |
  | `mut_nocategory` | switches categories off | 6 failed |

- `bash .claude/skills/relay-verify/verify.sh --ui-build`, run three times:
  - first: 950 passed;
  - after the review fixes: 2 failed, 951 passed (the V2-10 noisy-neighbour tests, §9);
  - final: **953 passed** in 170.94 s.

  Final results: ruff clean; Jest 55 passed; tsc 0; eslint 0; the UI build copied 3 files;
  `results.csv` OK; 0 evidence changes.

## 7. Test Results

| Suite | Result |
| :-- | :-- |
| Backend `pytest -q` | **953 passed**, 0 failed, 0 skipped (934 before, plus 19 new) |
| `ruff check .` | clean |
| UI Jest | **55 passed** (54 before, plus 1 new, plus 1 rewritten) |
| UI `tsc` / `eslint` | exit 0 / exit 0 |

The tests the prompt asked for:

| Test | Observed |
| :-- | :-- |
| `/metrics` | Prometheus text (`text/plain`, `# TYPE … counter`). After a question and its repeat: the `ok` count on lane `primary` is 1, and 1 more on lane `none` (the cache hit). Total and first-sentence latency counts are 1; cache miss 1 and hit 1; tokens in > 0; all 4 layers present at 0. An uncited answer gives `L1` = 1. The live gauges follow their sources, and a lane without a TPM limit has no headroom sample |
| Privacy | The question, the follow-up, document text and the API key (whole and its hex part) appear in none of: the `/metrics` text, every span attribute in the local exporter, the stored breakdown JSON, or the `/v1/requests` view. Only `question_hash` is present |
| Waterfall | Each stage equals its span's duration (to 0.001 ms); `generation_ms` > 0; total ≥ the sum of the stages. Verifier: 2 checked, 2 passed, not partial; 2 `verifier.sentence` spans; generate lane `primary`. A follow-up's trace has `pipeline.rewrite` and a matching `rewrite_ms` |
| Cross-tenant | Own request 200; another tenant 404; unknown id 404; no key 401 |
| Transport | Through `HttpOpenAIProvider` over `MockTransport`: DNS → `DNS_FAILURE`, connect timeout → `CONNECT_TIMEOUT`, TLS → `TLS_ERROR`, read timeout → `READ_TIMEOUT`, remote disconnect → `STREAM_DISCONNECT`, refused → `CONNECT_FAILURE`. The logged string carries `category=…` and never the host or the resolver text |
| Review fixes | An abandoned answer's tokens are counted; a leader leaving while a follower stays (the follower gets `ok`); span work after a trace has finished is a silent no-op |

## 8. Actual Output and Generated Artifacts

An example `/v1/requests/{id}`, captured from a real run with the scripted provider (10 ms
per content piece; ids shortened):

```json
{"request_id": "v1ask-a4b8e568…", "question_hash": "4e0557a0c983…", "status": "ok",
 "verdict": "VERIFIED", "lane": "primary", "cost_usd": null, "escalated": false,
 "cache_status": "miss", "first_sentence_ms": 35.763,
 "waterfall": {"rewrite_ms": 0.0, "queue_ms": 0.0, "retrieval_ms": 24.67,
               "generation_ms": 95.756, "verification_ms": 1.003, "total_ms": 155.518},
 "tokens": {"in": 1316, "out": 25},
 "verifier": {"sentences_checked": 2, "sentences_passed": 2, "partial": false},
 "trace": [
  {"name": "request", "start_ms": 0.0, "duration_ms": 155.518, "attributes": {"tenant_id": "a136…", "question_hash": "4e05…", "priority": "interactive", "waiting_ms": 30.259, "working_ms": 91.868, "retrying_ms": 0.0, "status": "ok", "lane": "primary", "verdict": "VERIFIED", "…": "…"}},
  {"name": "queue.wait", "start_ms": 33.087, "duration_ms": 0.0, "attributes": {"priority": "interactive", "queue_position": 0, "over_rate": false}},
  {"name": "pipeline.retrieve", "start_ms": 33.087, "duration_ms": 24.67, "attributes": {"chunks_found": 4, "preset": "small_fast", "early": false}},
  {"name": "pipeline.generate", "start_ms": 57.757, "duration_ms": 95.756, "attributes": {"lane": "primary", "model": "test-model", "tokens_in": 1316, "tokens_out": 25, "mode": "streaming"}},
  {"name": "verifier.sentence", "start_ms": 115.504, "duration_ms": 0.0, "attributes": {"sentence_idx": 1, "l1_pass": true, "l2_pass": true, "l3_pass": true}},
  {"name": "verifier.sentence", "start_ms": 153.513, "duration_ms": 0.0, "attributes": {"sentence_idx": 2, "l1_pass": true, "l2_pass": true, "l3_pass": true}},
  {"name": "pipeline.verify", "start_ms": 153.513, "duration_ms": 1.003, "attributes": {"verdict": "VERIFIED", "mode": "streaming"}}]}
```

`cost_usd` is null because this test lane has no price (V2-09 rule).

An excerpt of `/metrics` after that request (the tenant id is shortened):

```text
relay_requests_total{error_code="none",lane="primary",status="ok",tenant="a136…"} 1.0
relay_request_latency_seconds_count{lane="primary",stage="first_sentence"} 1.0
relay_request_latency_seconds_count{lane="primary",stage="ttft"} 1.0
relay_request_latency_seconds_count{lane="primary",stage="total"} 1.0
relay_tokens_total{direction="in",lane="primary"} 1316.0
relay_tokens_total{direction="out",lane="primary"} 25.0
relay_cache_events_total{event="hit"} 0.0
relay_cache_events_total{event="miss"} 1.0
relay_cache_events_total{event="coalesced"} 0.0
relay_verifier_blocks_total{layer="L1"} 0.0
relay_verifier_blocks_total{layer="L2"} 0.0
relay_verifier_blocks_total{layer="L3"} 0.0
relay_verifier_blocks_total{layer="L4"} 0.0
```

The live gauges have no samples in this run: nothing was queued, and the test lane has no
TPM limit. `test_queue_depth_and_quota_headroom_are_read_live` shows samples such as
`relay_queue_depth{priority="batch",tenant="acme"} 3.0` and
`relay_quota_headroom_tokens{lane="fast"} 6200.0`.

## 9. Errors and Root-Cause Analysis

1. **The first `/metrics` test counted 0 for lane `primary`.** Its made-up question
   ("…zebra-quokka…") was refused up front as out of scope, so no model was called. The
   metrics were right; the test's question was wrong. It now uses an in-scope unique
   question.
2. **"Request log write failed unexpectedly" in tests.** The test's sink was never closed,
   so its writer ran after the in-memory store closed. The fixture now closes every sink
   before the store.
3. **Code review: no high-confidence bugs. Of 4 lower-confidence points, 3 were fixed:**
   - *A leader leaving while a follower stays:* later span work on the closed trace logged
     OTel warnings and kept spans never ended. `RequestTrace.start/set/end` are now no-ops
     once the trace is finished. Test `test_span_work_after_a_trace_is_finished_is_a_silent_no_op`
     was RED (no `end`), then GREEN. The request-level test passed before and after: it could
     not reproduce the noise through the test client.
   - *Abandoned flights:* their tokens were missing from the metrics; they are now added in
     the cancel path (`observe_spend`). Test `test_an_abandoned_answer_still_counts_its_tokens`
     was RED (0.0), then GREEN.
   - *Inner generator cleanup:* it was deferred to garbage collection; `aclosing` now closes
     `_answer` at once. No dedicated test: the existing disconnect tests pass.
   - *`/metrics` unauthenticated:* documented as a ruling (§5.1), not changed.
4. **The V2-10 noisy-neighbour tests failed only in full-suite runs** (`assert 3 <= 2`). They
   pass on their own.
   - Root cause: B was timed from when it was sent. B's request does work before it is
     queued (the key, the collection and index checks), and on a loaded machine A's running
     answers finished meanwhile. So the test measured preprocessing, not the queue.
   - The fix measures the invariant at the queue itself. The admission order is recorded,
     and B must be the very next request admitted after it enters the queue (`ahead == 0`).
     Both queue sizes give 0. This is stricter than before (it was ≤ 2) and does not depend
     on load. No product code changed.

## 10. Fixes Applied

All of them are in §9. The final full run is in §6.

## 11. Outstanding Issues

- **The overload scenario has not been viewed on a real dashboard.** No Grafana or
  Prometheus ran here. The JSON was validated as JSON and its queries name the exported
  metrics only.
- **`/metrics` must be kept on a private network** (§5.1).
- **The request view reads the background-written request log,** so there is a short delay
  after a request ends.
- **Followers' traces have only their root span.** The pipeline spans belong to the leader's
  request.
- **Most tests were written after the code.** The exceptions are the UI tests and the review
  regression tests, which failed first. Mutation checks (§6) and the fresh-context review
  compensate.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| `/metrics` covers requests, latencies, tokens and verifier layers (plus cache, queue depth, quota) | met |
| `GET /v1/requests/{id}` returns the waterfall and trace, tenant-scoped | met |
| Transport errors are categorised | met (5 asked for, plus 2) |
| The Status page shows live metrics | met (5 s refresh; live quota per lane; queue rows from `/healthz`) |
| A starter Grafana dashboard exists | met (8 panels, covering the plan's overload view) |
| No metric or span holds text beyond a hash | met (privacy test plus a mutation check) |
| Full suite passes; ruff, tsc and eslint clean | met: 953 and 55 |
| Zero real Groq calls; `results.csv` and `evidence/` untouched | met |

## 13. Remaining Risks

- Many tenants would mean many label series in the request counters and the queue-depth
  gauge.
- `RecentSpans` keeps the last 2,000 spans in memory. The cost is bounded, but at about
  8 spans per request that is only around 250 recent requests.
- The OTel SDK is synchronous. Span work runs on the event loop and is cheap, but heavy
  exporters added later should be batched.

## 14. Recommended Next Phase

1. Owner review of this report.
2. Optionally, import the dashboard and replay the V2-14 overload scenario against the
   stand-in to confirm it reads from the dashboard alone.
3. Then V2-12: the evaluation system and CI gate.
