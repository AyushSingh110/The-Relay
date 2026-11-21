# Phase V2-09: Provider router and cost tracking

- **Date:** 2026-11-21 IST
- **Status:** completed, pending owner review.
- **Real Groq calls:** 0 (cap 20). Routing, fallback, escalation and cost were proven
  offline: against real stand-in apps over HTTP (`httpx.ASGITransport`, no network), and
  with scripted providers.
- **Frozen evidence:** `results.csv` sha256 still starts with `8dfb10dfaadb`.
  `git status` shows no change under `evidence/`.

## 1. Objective

One model's limit or weakness must no longer decide the outcome (ARCHITECTURE_V2 §7):

- several lanes, each with its own ProviderGuard;
- fallback when the primary would make the request wait too long, or its circuit is open;
- exactly one escalation retry for a truncated or empty answer;
- an honest "unavailable" reply when no lane can serve;
- a per-lane price list that fills the `cost_usd` column v1 left blank
  (ARCHITECTURE_V2 §11).

## 2. Implementation Summary

**`control/pricing.py` (new)**

- `LanePricing` holds US dollars per million tokens.
- `cost_usd = (tokens_in·input_per_m + tokens_out·output_per_m) / 1e6`. A lane with no price
  gives `None`.
- `total_cost` returns 0.0 when no call was made, and `None` as soon as any part is unknown.
- `LaneUse` is one ledger entry per model call: lane, provider, model, purpose
  (`answer`, `escalation` or `rewrite`), tokens, usage source and cost.
- `RouteRecord` holds the serving lane, `fallback`, `escalated`, the escalation reason,
  every call, the skipped lanes with their reasons, and `retry_after_ms`.
- `RouteNote` is a ContextVar set inside the generation task. It lets a cancelled stream
  still name its lane; `MeteredProvider` already uses the same pattern.

**`ProviderGuard.predict()`** is a side-effect-free forecast of `acquire`. It returns the
quota wait in ns, or the refusal: `circuit_open`, `daily_budget_exhausted` or
`payload_too_large`.

**`control/router.py` (new)**

- `ProviderLane` holds:
  - the provider, model and its own guard;
  - the price, `max_completion_tokens`, `is_reasoning_model` and `reasoning_effort`;
  - `escalate_to`.
- `ProviderRouter` gives each lane its own `GuardedGenerator`. All lanes share one retry
  policy and retry budget. The routing rules are:
  1. The primary lane, if it can admit the request within `max_wait_ms`.
  2. Otherwise the next lane, in configured order, that can.
  3. Otherwise the lane that frees up first. Its guard waits up to the deadline, exactly as
     v1's single lane did.

  No lane can admit at all, or every wait ends after the deadline: `NoLaneAvailableError`,
  with the primary lane's reason as its code and `retry_after_s`.
- `escalate(failed_lane, reason, request)` returns the lane's `escalate_to` lane only if that
  lane can admit the request and the shared retry budget grants one retry.
- `fits()` reports whether any lane has the token headroom; it drives degraded top-k.
- `RoutedGenerator` is now the pipeline's generator. For each call it:
  - picks a lane;
  - moves on to the next candidate if a lane refuses locally at the moment of the call
    (the quota was taken in between);
  - records the route and cost;
  - for a buffered answer that was truncated or empty, makes the single escalation retry.

  `purpose="rewrite"` keeps the rewriter's own 64-token budget and never escalates.

**Pipeline and streaming**

- `GenerationResult.route` and `PipelineOutcome.route` carry the route.
- `escalation_reason()` classifies a result:
  - `finish_reason == "length"` with content is `truncated`;
  - length with no content but some reasoning is `reasoning_exhausted` (the v1 P07
    gpt-oss case);
  - an empty answer that ended normally is `empty`.
- `merge_escalation()` sums both calls' tokens, cost and attempts. It counts one retry.
  If the retry failed, the first answer is kept.
- `TokenStream(prior=…, reason=…)` and `Pipeline.escalate_streaming()` handle escalation
  in streaming.
- **`SentenceStream`:** a stream that ended at the token limit now never judges or releases
  its last, cut-off unit. It ends with `StoppedEvent("truncated")`. Before this change, the
  cut-off tail was judged like any other last sentence, so a fragment that happened to carry
  a citation tag could be released.

**`/v1/ask`**

- **Streaming escalation** happens only when no sentence has been delivered yet. A second
  `status {stage: "generating", escalated: true, reason}` event announces it.
- **Every `final`** carries `lane_name`, `provider`, `model`, `escalated`, `fallback`,
  `cost_usd`, `ledger` and `retry_after_ms`.
- **Tokens and cost cover the request's whole lifecycle:** the follow-up rewrite's call is
  added. A coalesced follower or a cache hit pays only for its own rewrite.
- **`request_log`** gets `lane_name`, `cost_usd` and `escalated`.

**Store:** migration **6** adds `request_log.lane_name TEXT`, `cost_usd REAL` and
`escalated INT DEFAULT 0`.

**Settings and composition**

- Lanes are configured as `[[providers.lanes]]`. The older top-level `[[lanes]]` still works;
  setting both is an error.
- Per-lane settings: `pricing = {input_per_m, output_per_m}`, `guard = {…}` (overrides of
  `[guard]`), `escalate_to` (validated) and `is_reasoning_model`.
- `[providers] max_wait_ms` defaults to 2000.
- Compose opens one provider per lane, including an embedded stand-in per stand-in lane.
  All Groq lanes share one `RealCallBudget`, the hard cap.
- Follow-up rewrites go through the router. `[rewriter].model` pins them to the lane that
  serves that model.
- `/healthz` lists each lane's guard state when there is more than one lane.

**UI**

- The answer footer adds "answered by the X lane" for a fallback, "retried once on a
  stronger model", and the cost when it is above 0.
- `truncated` has a plain-language stop reason.

## 3. Files Created

- `src/relay/control/pricing.py`
- `src/relay/control/router.py`
- `tests/test_v2_09_router.py`: 20 tests (17, plus 3 from the review fixes)
- `tests/test_v2_09_standin_scenarios.py`: 14 tests (12, plus 2 from the review fixes)
- `web/src/__tests__/routing.test.tsx`: 3 tests
- `reports/phase-v2-09-provider-router.md` (this file)

## 4. Files Modified

- `src/relay/control/provider_guard.py`: `Admission` and `predict()`.
- `src/relay/orchestrator/pipeline.py`:
  - the route fields;
  - `Escalating`, `escalation_reason` and `merge_escalation`;
  - `TokenStream` prior, note, `truncated` and `estimated_route`;
  - `escalate_streaming`;
  - the route on the cancelled path.
- `src/relay/orchestrator/streaming.py`: the truncated-stream stop.
- `src/relay/orchestrator/rewriter.py`: `Rewrite.usage` is a `LaneUse`, and `tokens` is now a
  property. The timeout path keeps its lane through a `RouteNote`.
- `src/relay/api/app.py`: `create_app(router=…)`, `single_lane_router`, `RelayService.router`
  and per-lane `/healthz`.
- `src/relay/api/routes/ask_v1.py`: streaming escalation, `_route_fields`, `_charge`, the
  final and request-record fields, and the cancelled spend.
- `src/relay/store/migrations.py`: migration 6.
- `src/relay/store/repositories.py`: the `RequestLogEntry` and `insert` fields.
- `src/relay/app/request_log.py`: `log_fields` maps the new columns.
- `src/relay/app/settings.py`: `LanePricingSettings`, `LaneGuardSettings`,
  `ProvidersSettings`, the `[[providers.lanes]]` alias and validators. The default stand-in
  lane is priced 0, because it is simulated.
- `src/relay/app/compose.py`: per-lane providers and guards, the router, the shared real-call
  budget, `rewrite_generator`, `lane_pricing` and the start-up lines.
- `relay.example.toml`: `[providers]` and `[[providers.lanes]]`, with pricing and the commented
  options. No Groq price is filled in.
- Web files:
  - `web/src/api.ts`: the `Final` fields;
  - `web/src/answer.ts`: the `truncated` reason;
  - `web/src/components/AnswerView.tsx`: the footer.
- `src/relay/app/static/*`: the rebuilt UI bundle.

## 5. Technical Decisions

These are my rulings where the prompt, the architecture or the code disagreed.

1. **Migration 6, not "Migration 005".** Migration 5 is V2-07's `collection.l4_enabled`.
   Cost if wrong: none; the version numbers must have no gaps.
2. **`[[providers.lanes]]`, with `[[lanes]]` still accepted.** The README, demo docs, tests
   and load scripts use `[[lanes]]`. Cost if wrong: two spellings exist; using both is
   rejected.
3. **Rule 3 waits up to the deadline instead of failing at `max_wait_ms`.**
   - The prompt said to raise when no lane admits within `max_wait_ms`. ARCHITECTURE_V2 §7
     rule 4 says "if no lane can serve **before the deadline**". The architecture takes
     priority.
   - So `max_wait_ms` only decides whether to skip a lane. When every lane is busy, the
     request waits for the lane that frees first. It is refused at once with `retry_after_ms`
     when that wait would end after its deadline, or when no lane can admit at all.
   - With one lane, behaviour is exactly as before, so v1 `/ask` and the V2-14 load figures
     are unaffected.
   - Cost if wrong: without a deadline, a request can still wait as long as v1's single
     lane, up to 29.2 s observed. Clients that need a bound must send `deadline_ms`.
4. **Escalation in streaming happens only before any sentence has been delivered.**
   - The SSE contract has no "discard what you showed" event, and verified sentences have
     already reached the user.
   - A stream truncated after N delivered sentences ends partial, with stop reason
     `truncated`. The cost example in §8 shows this case.
   - Buffered mode escalates whenever the answer is truncated or empty.
   - Cost if wrong: partly delivered truncated answers are not completed by the stronger
     lane.
5. **A provider failure is not failed over within the same request.** The lane's own retries
   and circuit breaker handle it. Once the circuit opens, new requests go to the next lane.
   The plan's "primary lane failing" scenario tests exactly this. Cost if wrong: the requests
   before the circuit opens fail instead of being served by lane B.
6. **`select_lane(request, …)` takes the request, not `estimated_tokens: int`.** Each lane
   estimates with its own characters-per-token and its own completion budget. Cost if wrong:
   only a signature difference.
7. **Prices are never invented** (CLAUDE.md §5.3).
   - An unpriced lane reports `cost_usd: null`, as does any request that used one.
   - The stand-in default lane is priced 0/0 because it is simulated.
   - `relay.example.toml` leaves the Groq rates as placeholders for the owner.
   - The prices in the tests are labelled test-only.
8. **The escalation reasons are only truncated, reasoning-exhausted and empty.**
   ARCHITECTURE_V2 §7 rule 3 also mentions "refused where the gate said the evidence was
   sufficient". The plan scoped this phase to truncation, so refusal escalation is not built.
9. **The cache scope still uses the primary model's id.** An answer served by a fallback lane
   is verified and cached under the same scope. Cost if wrong: a fallback answer can be
   replayed later as a cache hit, labelled `exact_hit` with no lane.

## 6. Commands Actually Executed

The Python interpreter is `C:/Users/ASUS/anaconda3/envs/the-relay/python.exe`.

- `python -m pytest -q`, after the router replaced the single guarded generator but before
  any new test: **866 passed**, so single-lane behaviour was unchanged.
- `python -m pytest tests/test_v2_09_standin_scenarios.py -q`:
  - first run: 1 failed, because the stand-in returned HTTP 404 (the test's base URL lacked
    `/openai/v1`) and the request-log sink had not been started;
  - after the fixes: 12 passed.
- Checks that the new tests can fail. Each run monkeypatches the code through a pytest plugin
  module kept in the scratchpad:

  | Plugin | What it switches off | Result |
  | :-- | :-- | :-- |
  | `mut_noescalate` | escalation | 5 failed, 7 passed |
  | `mut_nofallback` | the guard forecast: every lane always looks free | 2 failed, 10 passed |
  | `mut_nocost` | cost: always 0 | 3 failed, 9 passed |

- `python -m pytest tests/test_v2_09_router.py -q`: 17 passed.
- `npx jest src/__tests__/routing.test.tsx` before the UI change: 2 failed, 1 passed (RED).
  The one that passed asserts that nothing is added.
- `npx jest`: **50 passed**. `npx tsc -p tsconfig.json --noEmit`: exit 0. `npx eslint .`:
  exit 0.
- `relay ui build`: copied 3 files to `src/relay/app/static`.
- `python -m pytest -q`: **895 passed** in 129.59 s, before the review.
- `python -m ruff check .`: one E501 in the new test file. Wrapped, then "All checks passed!"
- A fresh-context code review (an agent), then 5 regression tests: **5 failed (RED)**, each
  for the reported reason. After the fixes: `tests/test_v2_09_*`: 34 passed.
- Final `python -m pytest -q`: **900 passed** in 128.79 s. `ruff check .`: All checks passed.
  `npx jest`: 50 passed. tsc and eslint: exit 0.
- `sha256sum results.csv`: `8dfb10dfaadb…`. `git status --short results.csv evidence/`:
  empty.
- A scratch script, `scratchpad/cost_example.py`, printed the §8 ledger from real stand-in
  requests.

## 7. Test Results

| Suite | Result |
| :-- | :-- |
| Backend `pytest -q` | **900 passed**, 0 failed, 0 skipped (866 before, plus 34 new) |
| `ruff check .` | clean |
| UI Jest | **50 passed** (47 before, plus 3 new) |
| UI `tsc` / `eslint` | exit 0 / exit 0 |

Stand-in scenarios, all passing (`tests/test_v2_09_standin_scenarios.py`). Each lane is a
real stand-in app reached over HTTP:

| Scenario | Set-up | What was observed |
| :-- | :-- | :-- |
| 1. Primary exhausted | Lane A at 1 RPM, lane B at 100 RPM; 2 concurrent questions | Both answers ok and VERIFIED; one served by A, one by B (`fallback: true`, model `model-b`); one stand-in call per lane; `request_log.lane_name` is a and b; `/healthz` shows each lane's own RPM window at 1 |
| 2. Truncation, streaming and buffered | Lane A cut at 20 tokens with `escalate_to = "b"` | VERIFIED from lane b; `escalated: true`; ledger `[answer@a, escalation@b]`; exactly 1 call per lane; no `stopped` event; `request_log.escalated = 1` |
| 2a. The escalation also truncates | Both lanes at 20 tokens, escalating to each other | Exactly 1 call per lane; no second escalation; verdict BLOCKED |
| 2b. Retry budget spent | Retry budget 0 | No escalation: 1 call, lane a |
| 2c. Reasoning exhaustion | Lane A: 64-token budget, 500 reasoning tokens | Escalated with reason `reasoning_exhausted`; VERIFIED from lane b |
| 3. Cost ledger | The escalation case | `cost_usd` equals the formula over the reported usage of both calls (`usage_source: provider`); `request_log.cost_usd` and tokens equal the final |
| 3a. Rewrite cost | A follow-up with the LLM rewriter routed through the lanes | Ledger `[rewrite, answer]`; tokens and cost include the rewrite; the request-log row matches |
| 3b. Unpriced lane | No price on the lane | `cost_usd` is null in the final and the row |
| 4. Every lane exhausted | Both lanes at 1 RPM after one call each; third question with `deadline_ms = 5000` | HTTP 200 SSE: `accepted` first, `sources` still sent with passages, final `predicted_wait_exceeds_deadline` with the honest message; `retry_after_ms` between 50 and 60 s; `cost_usd` 0.0; no provider call |
| Plan: primary failing | Lane A answers 500 on every call; circuit threshold 1 | The first request fails honestly; A's circuit opens; the next request is served by B with `fallback: true`; A had 1 call |
| Plan: primary daily budget spent | Lane A at 1 RPD | The second question is served by lane b |

## 8. Actual Output and Generated Artifacts

Below is a cost example printed by `scratchpad/cost_example.py` from real stand-in requests.
The prices are **test-only**: A at 0.50/1.50 and B at 2.00/6.00 USD per million tokens.

**Question 1:** "What is the latency of Moshi?". Lane a is cut at 20 tokens and escalates
to b.

| Call | Lane | Tokens in | Tokens out | Cost (USD) |
| :-- | :-- | --: | --: | --: |
| answer | a | 1,243 | 19 | 0.000650 |
| escalation | b | 1,243 | 72 | 0.002918 |
| **total** | | **2,486** | **91** | **0.003568** |

- Answer call: (1243·0.50 + 19·1.50)/1e6 = 0.00065.
- Escalation call: (1243·2.00 + 72·6.00)/1e6 = 0.002918.
- Final: `status: ok`, `verdict: VERIFIED`, `lane_name: b`, `escalated: true`.
- `request_log` row: `b 2486 91 0.003568 True`.

**Question 2:** a follow-up in the same conversation, "And how does it model the
conversation?".

| Call | Lane | Tokens in | Tokens out | Cost (USD) |
| :-- | :-- | --: | --: | --: |
| rewrite | a | 353 | 1 | 0.000178 |
| answer | a | 1,245 | 20 | 0.0006525 |
| **total** | | **1,598** | **21** | **0.0008305** |

- Row: `a 1598 21 0.0008305 False`.
- This question delivered one verified sentence, then hit the 20-token limit. It ended
  partial, with stop reason `truncated` and **no escalation**, because a sentence was already
  out (decision 4). Its verdict is BLOCKED and status ok, the same partial shape as V2-05.
- The stand-in cannot rewrite (it answers "OK"), so the rewrite fell back to the original
  question. Its usage was still charged.

## 9. Errors and Root-Cause Analysis

1. **A patch-script heredoc turned the `\n` escapes in migration 6's SQL into real newlines**
   (a syntax error). Fixed with the Edit tool; the suite then passed.
2. **The stand-in scenario tests first got HTTP 404.** The test lane's base URL lacked the
   stand-in's `/openai/v1` prefix. Fixed in the test helper.
3. **The request-log sink in the test harness was never started**, so its records were
   dropped. The harness now starts it before the first request.
4. **`test_the_example_file_parses_to_the_defaults` failed** once the example's stand-in lane
   had a price. The default lane is now priced 0/0 too, because it is simulated.
5. **One E501 line in the new test file.** Wrapped.
6. **A UI edit through a bash heredoc was blocked** by a shell-safety check: the `$${…}`
   template literal was unparseable to it. Nothing was deleted. The same edits were made with
   the Edit tool.

**Findings from the code review.** Each was confirmed against the code and given a
regression test that failed first.

7. **Important: a failed streaming escalation was reported as a citation-verification
   refusal.**
   - What went wrong: when the escalation lane failed mid-call, the client got
     `citation_verification_failed` instead of `upstream_error`.
   - Root cause: `merge_escalation` keeps the first answer when the retry failed, which is
     right for buffered mode. In streaming the merged result therefore had no error, so
     `SentenceStream` and `complete_streaming` judged the empty retry text.
   - Fix: `merge_escalation(…, keep_prior_on_failure=False)` for a stream, so the retry's own
     failure is the outcome. Both calls' spend still counts.
   - Test: `test_a_failed_streaming_escalation_is_reported_as_a_provider_failure`.
8. **Minor: a coalesced follower inherited the leader's lane and escalation fields.** Its
   `request_log` row had `lane_name` set and `escalated = 1` at cost 0, which double-counts
   lane and escalation figures. A follower's routing fields are now empty, like a cache hit's.
   Test: `test_a_coalesced_follower_records_no_lane_of_its_own`.
9. **Minor: retry-budget accounting.**
   - Every `GuardedGenerator` call recorded a "first attempt". Escalation calls and skipped
     lanes therefore enlarged the budget.
   - A single-lane request refused by the router before reaching the guard recorded none,
     unlike before V2-09.
   - Fix: the `RoutedGenerator` now records exactly one first attempt per request, never for
     an escalation. The lane generators use `record_first=False`.
   - Tests: `test_the_retry_budget_counts_one_first_attempt_per_request` and
     `test_a_request_refused_by_the_router_still_counts_as_a_first_attempt`.
10. **Edge case: an already expired deadline was reported as
    `predicted_wait_exceeds_deadline`.** The guard reported `deadline_expired` before
    V2-09. `candidates()` now hands an expired request to the primary lane's guard, so the
    v1 code comes back. Test: `test_an_expired_deadline_is_reported_as_expired`.

One point from the review is not fixed. If a granted escalation is then refused locally (the
quota was taken in between), the retry it spent from the budget is not refunded. The budget
has no refund operation, and the effect is one retry at the margin.

## 10. Fixes Applied

All of them are in §9. Each was followed by a passing run of the affected tests and, at the
end, of the full suite.

## 11. Outstanding Issues

- **The main tests were written after the code.** The exceptions are the UI test and the 5
  review regression tests, which failed first. To compensate, each scenario group was
  checked to fail when its feature is disabled (§6), and a fresh-context code review was run
  (§9, items 7–10).
- **Behaviour change in single-lane `/v1/ask` streaming,** flagged by the review: a stream
  cut at the token limit used to be able to deliver its last unit. Now it never does, and it
  ends with stop reason `truncated`. v1 `/ask` (buffered) and `results.csv` are not affected.
- **No real Groq run.** Pricing for Groq lanes is not set: the owner must copy the rates
  from Groq's price list. The behaviour of a real second Groq model as a fallback or
  escalation lane is unmeasured.
- **The owner has not chosen the second lane.** ARCHITECTURE_V2 §16 open decision 3: "the
  second model lane for fallback". The default config still has one stand-in lane, and no
  second lane is enabled by default.
- **`/v1/ask` cannot return HTTP 429** when every lane is exhausted. `accepted` is already
  sent before any work (§6, 200 ms rule), so the refusal is the SSE `final` with error code,
  message, sources and `retry_after_ms`. The prompt asked for "HTTP 429 / SSE stopped".
  There is no `stopped` event, because nothing was delivered.
- **Not tested:** rule 3's long wait with no deadline (about 60 s on the real clock), and the
  ARCHITECTURE_V2 refusal escalation (not built, decision 8).
- **A rewrite that times out** has `cost_usd: null`, so the request's total is null. Its lane
  is known, but what it spent is not.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| Independent lanes, each with its own ProviderGuard | met (scenario 1; `/healthz` per lane) |
| Fallback on a long primary wait or an open circuit | met (scenario 1, the failing-primary scenario, and the daily-budget scenario from the plan) |
| A truncated answer triggers exactly one escalation, within the retry budget | met (scenarios 2, 2a and 2b; streaming only before the first sentence, decision 4) |
| `cost_usd`, `lane_name` and `escalated` in `final` and `request_log` | met (scenarios 2 and 3; migration 6) |
| Token and cost sums reconcile with provider usage | met (scenario 3: ledger sums equal the final and the row) |
| 866+ backend and 47 UI tests pass; ruff clean | met: 895 and 50 |
| `results.csv` and `evidence/` untouched | met |
| Zero real Groq calls | met: 0 |

## 13. Remaining Risks

- With two real Groq lanes, the shared `MAX_REAL_CALLS` cap is reached faster: escalations
  and rewrites each spend a call.
- A fallback model of lower quality answers silently. The user sees only "answered by the X
  lane" and the verification still applies.
- `max_wait_ms = 2000` is not measured; it is a starting point.

## 14. Recommended Next Phase

1. Owner review of this report.
2. The owner chooses the second lane and its Groq prices. Optionally, a capped live check
   of fallback and escalation (a few of the 20 allowed calls, with owner confirmation).
3. Then V2-10: tenants, API keys and fair admission.
