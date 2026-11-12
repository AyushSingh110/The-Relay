# Phase 02 Implementation Report — Offline Provider Client and Probe Tooling

> **Phase ID:** `p02`
> **Date:** `2026-11-11 16:46 IST`
> **Status:** `Completed` (offline scope only). Verified in Conda env `the-relay` (Python 3.12.16, httpx 0.28.1, pytest-asyncio 1.4.1.post1): 163 passed, ruff clean. **No real Groq call was made.**
> **Owner Approval:** `Owner Approved (2026-11-12)`

---

## 1. Objective

`IMPLEMENTATION_PLAN.md` P02 asks for:
- an `HttpOpenAIProvider` for Groq and the Stand-In;
- header capture and parsing;
- error mapping;
- model listing;
- a capped probe script;
- MockTransport tests for all of it.

The owner's phase prompt sets two limits:
- **offline only.** Real Groq calls stay disabled and no quota is spent;
- **P02-LIVE is out of scope.**

## 2. Implementation Summary

A new package, `src/relay/providers/`, holds all provider-specific code. Other modules depend only on its neutral types (`ChatRequest`, the event models, `CompletionResult`, `TokenUsage`, `ModelCatalog`) and on its typed errors.

**Integration with P01.**
- The errors subclass the existing `RelayError`.
- Error codes come from the existing `relay.schemas.ErrorCode` vocabulary. A row's `status` is derived from the existing `ERROR_CODES_BY_STATUS`, so no second mapping table exists.
- Secrets are scrubbed with the existing `relay.redaction`.
- Timings use the existing `relay.telemetry.spans.ns_to_ms` / `MonotonicClock`.
- Probe evidence is written through the existing `run_store` (`create_run`, `append_row`, `append_trace`, `record_real_calls`, `finalize`).
- Probe rows are the existing `ResultRow`, so they aggregate through the unchanged `build_results.py`.

**How the provider client works (`HttpOpenAIProvider`).**
- **One client.** Each instance owns one `httpx.AsyncClient`. The base URL comes from `Settings`: Groq uses `groq_base_url` and `groq_api_key`, with `source="real"`; the Stand-In uses `standin_url` with no key and `source="stand-in"`.
- **No hidden retries.** The default transport is `httpx.AsyncHTTPTransport(retries=0)`. Redirects are not followed. The client contains no retry code: each `stream_chat`, `complete_chat` or `list_models` call sends exactly one HTTP request.
- **Call cap.** A `RealCallBudget` is consumed *before* each request is sent. Constructing a `source="real"` provider without one raises `ConfigurationError`.
- **Timeouts:**
  - httpx `connect` / `read` / `write` / `pool`;
  - a **total** per-call deadline that covers waiting for headers, reading the body and the whole stream;
  - a **stall** timeout: the maximum gap between SSE *data* events. Keep-alive comments do **not** reset it, so a server that only sends keep-alives is still detected as stalled.
  - All defaults are `[PROPOSED]`: connect 5 s, read 30 s, write 10 s, pool 5 s, stall 30 s, total 120 s.
- **Cancellation.** If the consumer stops early or is cancelled, a `finally` block closes the response. `CancelledError` is never caught.

**How streaming and SSE parsing work.**
- **Request.** `stream_chat` sends `stream: true` and `stream_options.include_usage: true`. A non-2xx response is read and mapped to a typed error. A 2xx response whose content type is not `text/event-stream` is rejected as a protocol error.
- **`relay.providers.sse.SSEDecoder`** implements the WHATWG event-stream rules:
  - Line handling. Lines arrive from `aiter_lines()` (LF, CRLF or CR); an event ends at a blank line; `:` comment lines are ignored.
  - Field handling. Multiple `data:` lines are joined with `\n`; one leading space after the colon is stripped; `event:` names are read; `id` and `retry` are ignored.
  - End of stream. A final event without a trailing blank line is still dispatched, so `data: [DONE]` at EOF works.
- **Event sequence.** Each data message is parsed as JSON and validated against a lenient wire model (strict types, unknown fields ignored). The provider then yields `ResponseOpened → (ReasoningDelta | ContentDelta)* → UsageReported? → StreamCompleted`.
- **Deltas.** Reasoning arrives as `delta.reasoning` (Groq) or `delta.reasoning_content` and is kept separate from `delta.content`. That separation lets provider TTFT exclude reasoning (ARCH §7.2).
- **Usage-only chunk.** The final `choices: []` chunk is handled.
- **Errors inside the stream.** A missing `[DONE]` raises `ProviderStreamIncompleteError`. An in-stream `{"error": …}` or `event: error` becomes a typed error.
- **Timestamps.** Every event carries `at_ns`; `ResponseOpened` also carries `sent_ns`.
- **Buffering.** `collect_stream()` buffers a stream into a `CompletionResult` (verify-before-delivery, ARCH §7.1). It records `first_reasoning_ns` and `first_content_ns`, so `first_content_ms` is the provider TTFT.
- **Non-streaming.** `complete_chat` sends `stream: false` and validates the JSON body; it has no TTFT, so `first_content_ms` is `None`.

**How token usage and rate-limit information are captured.**
- **Usage → `TokenUsage`** (frozen, strict):
  - prompt, completion and total tokens;
  - `completion_tokens_details.reasoning_tokens`;
  - Groq's `queue_time`, `prompt_time`, `completion_time` and `total_time`, kept for stand-in calibration.
- **Where usage is read from.** The top-level `usage` field is preferred, with a fallback to Groq's `x_groq.usage`. `location` records which was used. If usage is reported more than once, the first report wins.
- **Headers.**
  - **Raw capture.** Every response's headers are captured as a lower-cased dict; repeated headers are joined with `", "`. The dict is sanitised: `set-cookie` and any header whose name contains authorization or key wording is redacted, and `gsk_…`, bearer and `org_…` values are scrubbed.
  - **Parsed snapshot.** `RateLimitSnapshot` parses the six `x-ratelimit-*` headers plus `retry-after`, which may be seconds, a duration or an HTTP date. Duration strings such as `2m59.56s`, `7.66s`, `120ms`, `1h2m3s` and bare numbers are supported.
  - **Raw values kept.** Every `x-ratelimit-*` and `retry-after` header is kept verbatim in `raw`, including unknown ones.
  - **Malformed values.** These are listed in `parse_issues` and never raise, so a bad header cannot discard a valid answer.
  - **Naming.** Field names mirror the header names (`limit_requests`, not "rpm"). Every snapshot carries `semantics="documented_unverified"`.

**Which errors are handled, and how** (`relay.providers.errors`; `retryable` is a *hint* for P06 and the client never acts on it):

| Condition | Exception | `error_code` → status | retryable hint |
| :--- | :--- | :--- | :--- |
| 429, minute scope or unknown | `ProviderRateLimitError` | `upstream_rate_limited` → rate_limited | yes |
| 429, daily scope (RPD/TPD) | `ProviderRateLimitError` | `daily_budget_exhausted` → rate_limited | **no** |
| 401 / 403 | `ProviderAuthError` | `provider_auth_error` → error | no |
| 413 | `ProviderPayloadTooLargeError` | `payload_too_large` → error | no |
| other 4xx (400, 404, 422, …) | `ProviderBadRequestError` | `internal_error` → error | no |
| 5xx, Groq 498 capacity, in-stream error | `ProviderServerError` | `upstream_error` → error | yes |
| read timeout / stall / total | `ProviderTimeoutError(phase)` | `provider_stalled` → timeout | yes |
| connect/write/pool timeout, connect error, connection drop | `ProviderConnectionError(reason)` | `upstream_error` → error | yes |
| bad JSON, schema mismatch, wrong content-type | `ProviderProtocolError` | `upstream_error` → error | no |
| stream ended before `[DONE]` | `ProviderStreamIncompleteError` | `upstream_error` → error | yes |
| call cap reached (nothing sent) | `RealCallBudgetExceededError` | — | — |

How 429s are classified:
- **Scope.** Body wording is checked first: `(RPD)` / `(TPD)` / "per day" → daily; `(RPM)` / `(TPM)` / "per minute" → minute. Otherwise the headers are used, labelled `headers_documented_unverified`: remaining-requests = 0 → daily, remaining-tokens = 0 → minute. If neither applies, the scope is `unknown`.
- **Retry delay.** The `retry-after` header is used when present; otherwise the delay comes from the body's "try again in …".
- **Quoted numbers.** "Limit / Used / Requested" values in the body are kept in `detail`.

What error objects contain:
- **Messages** hold only the status, the provider error code or type, and a classification. They never contain the response body or credentials.
- **`body_excerpt`** is a redacted body excerpt, at most 2000 characters, kept for evidence.
- **`sanitize_error(e)`** gives `ClassName(status_code=N)`.

**Model discovery.**
- `list_models()` sends `GET models`.
- **Strict about the envelope.** A response that is not an object with a `data` array → `ProviderProtocolError`.
- **Tolerant about entries.** Only `id` is required. `owned_by`, `created`, `active`, `context_window` and `max_completion_tokens` are each optional and type-checked one by one; a wrong type becomes `None` plus an entry in `metadata_issues`.
- **Bad or repeated entries.** Entries without an id and duplicate ids are skipped and listed in `metadata_issues`.
- **Raw entry kept.** The full original entry is preserved in `metadata`.
- **Order.** Models are sorted by id for determinism.

**Probe tooling (`scripts/probe_groq.py`, a thin CLI over `relay.providers.probing`).**
- **Modes:**
  - `discover`: one `GET /models`; no result rows.
  - `baseline`: per model (at most 3), `--calls-per-model` short calls (default 3) plus one call per `--prompt-words` size (default 400 and 1600 words of deterministic synthetic filler).
  - `minute-limit`: tiny calls (`max_completion_tokens=8`), one at a time.
- **Dry run by default.** Without `--live`, the script prints the plan and the safeguards that would block a live run. It sends nothing and writes nothing.
- **A live run needs all of:**
  - `--live`;
  - `--max-real-calls N ≥ 1`;
  - a plan that fits within N;
  - `MAX_REAL_CALLS ≥ N` in the env or `.env`. This second gate defaults to 0, so a live run cannot happen by accident;
  - a configured `GROQ_API_KEY`.

  Otherwise the script exits 2 (refused) without sending anything.
- **`minute-limit`** is refused with exit 2 unless `--owner-approved-rate-limit-probe` is given, **even for a dry run**.
- **Stops during a run:**
  - the call cap (enforced per request by `RealCallBudget`, independently of the pre-flight check);
  - any daily-scope 429;
  - an auth failure;
  - any 429 outside `minute-limit`;
  - the first 429 in `minute-limit`;
  - `DAILY_SAFETY_FRACTION` of `x-ratelimit-*-requests`, which is documented as RPD but unverified.
- **Calls are sequential.**
- **Evidence** goes to a new `evidence/groq_probes/<run_id>/`:
  - `manifest.json`: `real_call_count` is updated per request.
  - `rows.csv`: one `probe_*` row per inference call, following the owner's rules (`source=real`, `retrieval_setting=none`, blank grade, `cache_status=off`, `verification_verdict=NOT_RUN`, spans blank).
  - `traces.jsonl`: one sanitised record per call, with raw headers, the parsed snapshot, timings, usage, prompt-token-per-char ratio and the error details.
  - `summary.json`: per-model n/min/median/max computed only from that run's records.

## 3. Files Created

- `src/relay/providers/__init__.py`: package marker.
- `src/relay/providers/errors.py`: the typed provider error hierarchy, with `error_code` and a retryable hint; also `RealCallBudgetExceededError` and `ScriptExhaustedError`.
- `src/relay/providers/headers.py`:
  - header capture and sanitising;
  - duration and `retry-after` parsing;
  - `RateLimitSnapshot` and `ResponseMeta`.
- `src/relay/providers/sse.py`: the incremental SSE decoder.
- `src/relay/providers/base.py`:
  - request, usage and event models;
  - `CompletionResult` and `collect_stream`;
  - `ModelInfo` and `ModelCatalog`;
  - the `ChatProvider` protocol;
  - `RealCallBudget`;
  - `ScriptedProvider` and `ScriptedReply`.
- `src/relay/providers/http_provider.py`: `HttpOpenAIProvider`, `ProviderTimeouts`, wire models, error mapping and model-catalogue parsing.
- `src/relay/providers/probing.py`: probe options, plan building, live pre-conditions, execution, row and trace construction, stop rules and the summary.
- `scripts/probe_groq.py`: the probe CLI (dry run by default).
- `tests/test_http_provider.py`: 75 provider-layer tests.
- `tests/test_probe_groq.py`: 15 probe-tooling tests.
- `reports/phase-02-provider-client.md`: this report.

## 4. Files Modified

- `tests/conftest.py`: the autouse network guard now also patches `httpx.AsyncHTTPTransport.handle_async_request` and `httpx.HTTPTransport.handle_request` to refuse non-loopback hosts.
  - *Why:* on Windows, asyncio's proactor sockets do not go through `socket.socket.connect`, so the P01 guard alone would not stop a real httpx call.
  - *Unchanged:* the existing socket guard. `MockTransport` is unaffected.
  - *Test:* `test_real_transport_cannot_reach_external_hosts_in_tests`.
- `README.md`: added a "Groq probes" section with the commands and safeguards.
- `ENGINEERING_LOG.md`: appended the P02 entry.
- No P01 source module was changed. `pyproject.toml` is unchanged: httpx was already a dependency, and **nothing was installed**.

## 5. Technical Decisions

1. **Plan file list.** `errors.py`, `sse.py` and `probing.py` were added beyond the plan's file list. They keep the HTTP client, the wire format and the CLI logic single-responsibility, and they make the probe logic testable without a subprocess. Probe tests sit in their own file (`tests/test_probe_groq.py`).
2. **The provider stays uniform.** Every call opens the response with `send(stream=True)` and reads the body under the total deadline, so the same timeout and error mapping cover streaming, non-streaming and `/models`.
3. **Stall vs total.** The stall timer resets only when an SSE data event is dispatched; the total timer covers the whole call. If the stall deadline is later than the total deadline, the timeout reports `phase="total"`.
4. **Retryability is a hint, not behaviour.** Retries belong to P06 ProviderGuard (ARCH §9.2/§9.5). Tests show exactly one transport request for every failure type.
5. **Which 429 errors are treated as daily.** A daily-scope upstream 429 maps to `daily_budget_exhausted` and is marked not retryable. ARCH §13.4 lists that code under "Daily budget exhausted (guard)". *Needs owner review* (§11, item 2).
6. **Unclassified 4xx → `internal_error`.** The request itself was wrong, so retrying cannot help.
7. **Timeout codes.** Connect, write and pool timeouts → `ProviderConnectionError` / `upstream_error`: no response bytes were received, so the provider did not stall. Read, stall and total timeouts → `provider_stalled`.
8. **Header parsing never raises.** Values are kept raw and reported in `parse_issues`. Envelope or JSON errors in a body, by contrast, fail loudly as `ProviderProtocolError`.
9. **`max_completion_tokens`** is sent rather than the deprecated `max_tokens`. Unset optional fields (temperature, seed, `reasoning_effort`, `reasoning_format`, `include_reasoning`) are omitted from the payload.
10. **Probe row definitions.** A probe has no Relay server in the loop:
    - `first_token_ms` is the time to the first content delta, otherwise the first reasoning delta, otherwise the completion event. The trace records which one was used (`first_token_basis`).
    - `provider_first_token_ms` counts content only.
    - The server spans are blank, which P01 validation allows.
11. **Probe token accounting:**
    - provider `usage` when reported (`provider`);
    - if the request was rejected before a stream opened: 0/0 with `none`;
    - if the stream opened but ended without usage: `estimated` with `ceil(chars / 3.0)` (`CONSERVATIVE_CHARS_PER_TOKEN`, [PROPOSED]).
12. **`GET /models` counts against the call cap.** This is conservative; whether it counts against Groq RPD is unknown.
13. **Baseline prompts use deterministic synthetic filler,** because paper text does not exist before P03. The representative sizes can be re-run with real passages later.
14. **Evidence file name.** Per-call probe records go to `traces.jsonl`, the name used by the P01 `run_store` and by ARCH §13.2, rather than the `probes.jsonl` named in ARCH §13.1 and P02-LIVE (see §11, item 1).

## 6. Commands Actually Executed

All commands ran in Git Bash with `PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe`. Nothing was installed, and `.env` was never read.

```bash
git status --short; git log --oneline -3              # clean at start, HEAD d3c7bfa
$PY -c "import httpx, pytest_asyncio; print(httpx.__version__, pytest_asyncio.__version__)"
#   -> 0.28.1 1.4.1.post1
$PY -c "import relay.providers.http_provider, relay.providers.base; print('import ok')"
#   -> import ok
$PY -m pytest -q -p no:cacheprovider                  # run 1 -> 2 failed, 161 passed
$PY -m pytest -q -p no:cacheprovider                  # run 2 -> 163 passed; ruff -> 3 errors
$PY -m ruff check .                                   # after fixes -> All checks passed!
$PY -m pytest -q -p no:cacheprovider                  # final -> 163 passed in 2.08s
$PY -m pytest -q -p no:cacheprovider tests/test_http_provider.py tests/test_probe_groq.py
#   -> 90 passed in 1.75s
# CLI smoke (dry-run / refusal paths only; --env-file points at a non-existent file so the
# real .env is not loaded):
$PY scripts/probe_groq.py discover --env-file scratch/none.env                       # exit 0
$PY scripts/probe_groq.py baseline --model openai/gpt-oss-20b --max-real-calls 5 --env-file scratch/none.env   # exit 0
$PY scripts/probe_groq.py minute-limit --model m --minute-limit-calls 3 --max-real-calls 3 --live --env-file scratch/none.env   # exit 2
$PY scripts/probe_groq.py discover --live --max-real-calls 1 --env-file scratch/none.env   # exit 2
ls evidence                                            # README.md, canonical_runs.json (unchanged)
```

**Commands for the owner to re-verify (PowerShell):**

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
python -m pytest -v
python -m ruff check .
python -m pytest -v tests/test_http_provider.py tests/test_probe_groq.py
# Dry runs: nothing is sent. --env-file scratch/none.env skips your real .env; without it,
# .env is read but still nothing is sent.
python scripts/probe_groq.py discover --env-file scratch/none.env; $LASTEXITCODE
python scripts/probe_groq.py baseline --model openai/gpt-oss-20b --max-real-calls 5 --env-file scratch/none.env; $LASTEXITCODE
# Expected refusal (exit 2), even with --live:
python scripts/probe_groq.py minute-limit --model openai/gpt-oss-20b --minute-limit-calls 3 --max-real-calls 3 --live --env-file scratch/none.env; $LASTEXITCODE
git status --short
```

## 7. Test Results

Final runs, after the last code edit:
- `python -m pytest -q`: **163 passed, 0 failed, 0 skipped** in 2.08 s. `filterwarnings=error` is on, so no warnings were emitted. The total is the 73 P01 tests plus 90 new ones.
- Collected per file: `test_http_provider.py` 75, `test_probe_groq.py` 15, `test_schemas.py` 29, `test_foundation.py` 21, `test_run_store_and_aggregation.py` 23. P01's 73 tests count as 29 + 21 + 23 because parametrised cases expand.
- `python -m ruff check .`: **All checks passed!**

What the new tests cover:
- **Streaming:**
  - reasoning vs content deltas, the usage-only final chunk, the `x_groq.usage` fallback and the no-usage case;
  - event order;
  - the request payload (`stream`, `include_usage`, omitted optionals, auth header, path);
  - SSE split mid-line across network reads with CRLF;
  - `[DONE]` without a trailing blank line, and comments/keep-alives.
- **SSE decoder unit tests.**
- **Malformed responses:** five kinds of malformed chunk; a wrong content type; a stream incomplete before `[DONE]`.
- **Error mapping:**
  - typed in-stream error events;
  - 8 HTTP status mappings, each asserting **exactly one request**;
  - 429 minute/daily classification from the body, plus the header fallback (3 cases);
  - `retry-after` header vs body hint;
  - org-ID redaction.
- **Transport failures:** read timeout, connect timeout, connect error and remote protocol error, each with exactly one request; a connection dropped mid-stream.
- **Timeouts and cancellation:** stall after partial content; keep-alives that do not reset the stall timer; total timeout on a trickling stream; total timeout while waiting for headers; an early consumer stop closing the response.
- **No retries / no network:**
  - the default transport has `retries == 0` (checked by introspection);
  - the real transport is blocked from external hosts in tests.
- **Non-streaming:** a parsed body; three malformed bodies; error mapping.
- **Model discovery:**
  - heterogeneous metadata, wrong types, a missing id and duplicates;
  - wrong envelope shapes;
  - an auth error.
- **Call budget and config:**
  - the budget is enforced before sending, including `/models`;
  - a real provider requires a budget;
  - the base URL must be absolute.
- **Header parsing:** 14 duration cases; HTTP-date `retry-after`; malformed headers kept raw; header capture with case-folding, joining and redaction.
- **Secrets and models:**
  - no key or org ID in errors, metadata or `repr(provider)`;
  - the `ChatRequest` payload;
  - `ScriptedProvider` replay, mid-stream failure and exhaustion.
- **Probe tooling:**
  - **Safeguards:** a dry run sends nothing and writes nothing; `minute-limit` is refused without the flag (dry and live); each live safeguard refusal works (plan exceeds cap, env cap lower than CLI cap, no key, no cap); the 3-model limit is enforced.
  - **Baseline end to end:** rows, traces, summary and manifest, with no key or Authorization header in the evidence.
  - **Stop rules:** `minute-limit` stops at the first 429 and redacts the org ID; the daily safety fraction stops the run; the budget stops execution even when the pre-flight check is bypassed.
  - **Discover:** writes no rows.
  - **Failed calls:** timeouts, incomplete streams and 503s become valid rows (including `estimated` usage).
  - **Aggregation:** probe rows aggregate through `build_results`.

## 8. Actual Output and Generated Artifacts

- **Offline only.** Every provider response in this phase came from `httpx.MockTransport` fixtures. Their header and body values are synthetic, and **no Groq measurement exists yet**.
- **Dry-run output (observed).** For `discover`, `planned_calls: 1`, blocked by: `--max-real-calls must be at least 1`, `plan needs 1 calls but --max-real-calls is 0`, `GROQ_API_KEY is not configured`.
- **Baseline dry run** with `--max-real-calls 5`: `planned_calls: 5`, blocked by the `MAX_REAL_CALLS ... is 0` gate and the missing key.
- **Refusal (observed).** `minute-limit … --live` without approval: exit 2 with the approval problem listed.
- **Evidence directory.** `evidence/` still holds only `README.md` and `canonical_runs.json`. Test runs wrote to pytest `tmp_path` only.

## 9. Errors and Root-Cause Analysis

1. **`test_default_is_a_dry_run_that_sends_nothing` failed (test bug).** The test expected a `MAX_REAL_CALLS` problem with a CLI cap of 0 and an env cap of 0. With both at 0, the env gate is correctly satisfied (0 ≥ 0) and the "at least 1" check reports the problem instead. The test's expectation was wrong; the code was right.
2. **`test_probe_rows_aggregate_into_results` failed (test bug).** It used `summary.rows`, but the P01 `BuildSummary` field is `row_count`.
3. **Ruff, 3 findings:**
   - a `str.split()` constant flagged with a list-literal suggestion (`probing.py`);
   - two E501 long lines in `tests/test_http_provider.py`.
4. **Drafting bug, found before any test run.** `collect_stream` used `x or at_ns` to record the first timestamp, which would treat a monotonic value of 0 as unset. I replaced it with an `is None` check.

## 10. Fixes Applied

1. The dry-run test now passes `--max-real-calls 5` with env `MAX_REAL_CALLS=0` and asserts that exactly one problem, the env gate, is reported. That is stronger than the original test.
2. Changed to `summary.row_count`.
3. Replaced the split with a tuple literal and wrapped the two long lines.
4. Changed to an `is None` check.

Re-verification: full suite 163 passed; ruff clean (§7).

## 11. Outstanding Issues (for owner review)

1. **Evidence file name conflict in the docs.** ARCH §13.1 and IMPLEMENTATION_PLAN P02-LIVE name `probes.jsonl`; ARCH §13.2 and the P01 `run_store` use `traces.jsonl`. I used `traces.jsonl` and did not edit the approved docs. *Decision needed:* align the docs on `traces.jsonl` (recommended), or rename.
2. **Daily-scope upstream 429 → `daily_budget_exhausted`.**
   - *Alternative:* `upstream_rate_limited`, with the scope kept only in the trace.
   - *Recommendation:* keep the current mapping and add an ARCH §13.4 row "Upstream daily-limit 429 → rate_limited / daily_budget_exhausted".
3. **Unverified until P02-LIVE:**
   - all header meanings and the header-based 429 scope fallback;
   - whether Groq sends usage at top level, in `x_groq` or both;
   - the reasoning delta field name for `gpt-oss`;
   - acceptance of `max_completion_tokens`, `reasoning_effort` and `include_usage` for each candidate model;
   - the exact 429 body wording that the regexes rely on;
   - whether Groq's stream ends with `[DONE]`.
4. **The daily safety fraction only sees `x-ratelimit-*-requests`** (documented RPD). TPD is not visible in headers, so token-per-day protection in the probe script rests on the small hard call cap.
5. **`[PROPOSED]` values introduced:**
   - the timeout defaults (§2);
   - `CONSERVATIVE_CHARS_PER_TOKEN = 3.0`;
   - the baseline defaults (3 short calls, prompt sizes 400 and 1600 words, `max_completion_tokens=256`; the plan is 5 calls per model, 15 for 3 models, within the Day-1 ≤ 40 budget);
   - the minute-limit `max_completion_tokens=8`.
6. **`complete_chat` (non-streaming) provides no TTFT.** It exists for completeness; the pipeline will use `stream_chat`.
7. **Editor diagnostics.** Pyright "could not be resolved" diagnostics persist because VS Code is not using the `the-relay` interpreter; this is not a code issue.

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P02) | Status | Evidence |
| :--- | :--- | :--- |
| `httpx.AsyncClient`, no retry wrapper, transport `retries=0` | `PASS` | `test_default_transport_has_zero_retries`; exactly 1 request in every failure test |
| Explicit connect/read/total timeouts + per-chunk stall timeout | `PASS` | `ProviderTimeouts`; the stall, keep-alive, total-stream and total-headers tests |
| `stream=True` + `include_usage`; final `choices: []` usage chunk | `PASS` | `test_stream_parses_reasoning_content_usage_and_headers` (payload + usage) |
| Separate reasoning vs content deltas | `PASS` | event-order test; `first_reasoning_ns` ≤ `first_content_ns` |
| Parse all `x-ratelimit-*` + `retry-after` + durations; keep raw header dict; label unverified | `PASS` | header tests; `semantics == "documented_unverified"`; `raw` and `meta.headers` asserted |
| Map 429 (minute vs daily), 413, 401/403, 5xx, timeouts to typed errors | `PASS` | status mapping (8 cases), 429 classification tests, transport tests |
| Model listing, heterogeneous metadata | `PASS` | `test_list_models_tolerates_heterogeneous_metadata`, wrong-shape tests |
| `probe_groq.py` `discover` / `baseline` / `minute-limit`, under `--max-real-calls` and daily safety fraction | `PASS` (offline) | probe tests via MockTransport; live execution against Groq not performed (P02-LIVE) |
| `minute-limit` refuses without the flag | `PASS` | `test_minute_limit_refused_without_owner_flag[False/True]` + CLI smoke exit 2 |
| Cap enforcement | `PASS` | `test_budget_is_enforced_before_any_request_is_sent`, `test_budget_stops_execution_even_without_preflight`, preflight refusals |
| Sanitisation (no auth, org IDs redacted) | `PASS` | secrets test, `minute-limit` org redaction, evidence scans for key/`authorization` |
| Writes `evidence/groq_probes/<run_id>/` | `PASS` (offline) | baseline/discover tests, using `tmp_path` |
| Offline `pytest`, `ruff check .`, no live calls | `PASS` | 163 passed; ruff clean; no Groq request sent; the httpx guard blocks external hosts |

## 13. Remaining Risks

- **Wire-format surprises in P02-LIVE.** Groq's format may differ from the documented one: field names, 429 wording, or usage placement. The parsers are tolerant and keep everything raw, but the regex-based classification may need adjusting from real evidence.
- **Daily-quota exposure.** TPD cannot be observed from headers; the protection is the hard cap plus the owner's per-run approval.
- **Timeout defaults are untuned.** An overly tight stall or total timeout could misclassify slow successes as `provider_stalled` until they are calibrated.

## 14. Recommended Next Phase

**P02-LIVE: Model Discovery and Baseline Probes.** It is an owner-approved operational run and must not start without explicit approval. Prerequisites:
1. The owner puts `GROQ_API_KEY` in `.env` and sets `MAX_REAL_CALLS` to the approved cap (proposed ≤ 40 for Day 1).
2. The owner explicitly approves `discover` (1 call).
3. The owner confirms a shortlist of up to 3 models, then approves `baseline` with an explicit `--max-real-calls`.
4. The owner decides on §11 items 1–2.
5. The minute-limit probe is not run unless separately approved.

Stopping here for owner review.
