# Phase 02-LIVE Implementation Report — Step 2: Baseline Probes

> **Phase ID:** `p02-live` (Step 2: `baseline`)
> **Date:** `2026-11-12 08:48 IST` (run `2026-11-12T03:18:16.680Z` → `03:18:24.324Z`)
> **Status:** `Completed`. 15 of 15 planned calls were sent and all 15 succeeded. No early stop.
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Run the owner-approved P02-LIVE baseline once, with a hard cap of 15 real calls:
- shortlist: `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`;
- per model: 3 short calls, one 400-word prompt and one 1,600-word prompt, with `max_completion_tokens=256`.

Then report measurements, headers and the security scan.

Out of scope, and not done:
- `discover` again, `minute-limit`, extra models, or exploratory, replacement or retry calls;
- model selection, quality claims, P03.

## 2. Implementation Summary

No code changed. The steps were:

1. **CLI syntax** checked with `--help`.
2. **Dry run with the real `.env`**, with every approved value passed explicitly. The plan was checked in code against the approved plan:
   - 15 calls;
   - exactly the 3 models;
   - 5 calls each, labelled `short, short, short, prompt_400w, prompt_1600w`;
   - all `POST /chat/completions` with `max_completion_tokens=256`;
   - `live_blocked_by: []`.

   Result: `PLAN MATCHES APPROVED 15-CALL PLAN: True`.
3. **One live run with `--max-real-calls 15`.**
4. **Security scan** of the evidence, then extraction of the measurements from the raw `traces.jsonl`.

**Real requests sent: exactly 15. There were no failed attempts and no retries.** The script retries nothing, and every call returned HTTP 200 with a complete stream.

**Day-1 cumulative real calls: 16 of 40** (1 discovery + 15 baseline).

## 3. Files Created

- `evidence/groq_probes/probe-20261112T031816Z-2b5d017/`:
  - `manifest.json`: `status=complete`, `real_call_count=15`, `scenarios=["probe_baseline"]`;
  - `rows.csv`: 15 `probe_baseline` rows;
  - `traces.jsonl`: 15 sanitised records with the raw response headers;
  - `summary.json`.
- `scratch/baseline_dryrun.json`: the dry-run plan (gitignored scratch).
- `reports/phase-02-live-baseline.md`: this report.

## 4. Files Modified

- `ENGINEERING_LOG.md`: one entry appended. No source, test or architecture file was changed.

## 5. Technical Decisions

- **Explicit flags.** The approved values were passed as explicit flags rather than relying on defaults, so the command is self-documenting.
- **Sandbox** lifted for the single live command only, for network access.
- **Prompt content.** Prompts are deterministic synthetic filler (P02 decision). Token-per-character ratios therefore describe *this filler*, not paper text.
- **Requests used `temperature=0.0`** and no `reasoning_effort`, so each model used its default reasoning behaviour.

## 6. Commands Actually Executed

```bash
PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe
$PY scripts/probe_groq.py --help
$PY scripts/probe_groq.py baseline --model openai/gpt-oss-20b --model openai/gpt-oss-120b \
    --model qwen/qwen3.8-27b --calls-per-model 3 --prompt-words 400,1600 \
    --max-completion-tokens 256 --max-real-calls 15 > scratch/baseline_dryrun.json   # dry run, exit 0
# (plan verified programmatically -> True)
$PY scripts/probe_groq.py baseline --model openai/gpt-oss-20b --model openai/gpt-oss-120b \
    --model qwen/qwen3.8-27b --calls-per-model 3 --prompt-words 400,1600 \
    --max-completion-tokens 256 --max-real-calls 15 --live                           # ONCE, exit 0
# Evidence security scan + measurement extraction (Python; secrets never printed).
```

Live output:

```json
{"attempted_calls": 15, "outcome": "completed", "planned_calls": 15, "real_calls_sent": 15,
 "rows_written": 15, "run_dir": "evidence\\groq_probes\\probe-20261112T031816Z-2b5d017",
 "run_id": "probe-20261112T031816Z-2b5d017", "stopped_reason": null}
```

## 7. Test Results

No code changed in this step, so the suite was not re-run. The last run (P02-LIVE Step 1, after its final edits) was 163 passed and ruff clean. The run's own validation passed: every row went through strict `ResultRow` validation when written, and the manifest finalised as `complete`.

## 8. Actual Output and Generated Artifacts (REAL Groq measurements)

**Legend:**
- **MEASURED (client)**: timed by the probe script on the client side, from just before sending the request.
- **PROVIDER-REPORTED**: values Groq returned in `usage`; these are authoritative token counts but not our timing.
- **INTERPRETATION**: a reading of header meaning; not verified.

Each model has n = 5 calls with mixed prompt sizes. These are **descriptive values, not percentiles**, and must not be read as p50/p95.

### 8.1 Per-call measurements

Times are in ms, MEASURED (client), from send to: response headers (`hdr`), first reasoning delta (`1st R`), first content delta (`1st C`) and stream end (`total`). Tokens are PROVIDER-REPORTED.

| # | Model | Prompt | hdr | 1st R | 1st C | total | in | out | reasoning | finish | content |
| -: | :--- | :--- | -: | -: | -: | -: | -: | -: | -: | :--- | :--- |
| 1 | gpt-oss-20b | short | 595.7 | 599.9 | 646.9 | 659.6 | 95 | 51 | 41 | stop | `OK` |
| 2 | gpt-oss-20b | short | 471.1 | 471.8 | 520.1 | 521.3 | 95 | 51 | 41 | stop | `OK` |
| 3 | gpt-oss-20b | short | 321.7 | 325.5 | 374.1 | 378.8 | 95 | 51 | 41 | stop | `OK` |
| 4 | gpt-oss-20b | 400 w | 228.4 | 265.9 | 320.5 | 323.4 | 499 | 50 | 40 | stop | `OK` |
| 5 | gpt-oss-20b | 1600 w | 552.1 | 627.4 | 688.6 | 690.7 | 1699 | 64 | 54 | stop | `OK` |
| 6 | gpt-oss-120b | short | 510.0 | 511.2 | 603.1 | 609.7 | 95 | 53 | 43 | stop | `OK` |
| 7 | gpt-oss-120b | short | 521.8 | 531.3 | 631.1 | 638.3 | 95 | 53 | 43 | stop | `OK` |
| 8 | gpt-oss-120b | short | 495.0 | 503.9 | 601.6 | 609.4 | 95 | 53 | 43 | stop | `OK` |
| 9 | gpt-oss-120b | 400 w | 550.1 | 577.3 | 678.4 | 684.8 | 499 | 53 | 43 | stop | `OK` |
| 10 | gpt-oss-120b | 1600 w | 678.5 | 679.2 | 735.2 | 738.5 | 1699 | 53 | 43 | stop | `OK` |
| 11 | qwen3.8-27b | short | 272.3 | — | 273.3 | 275.8 | 38 | 2 | — | stop | `OK` |
| 12 | qwen3.8-27b | short | 259.4 | — | 261.0 | 263.5 | 38 | 2 | — | stop | `OK` |
| 13 | qwen3.8-27b | short | 276.6 | — | 280.2 | 283.9 | 38 | 2 | — | stop | `OK` |
| 14 | qwen3.8-27b | 400 w | 390.6 | — | 391.3 | 393.2 | 443 | 2 | — | stop | `OK` |
| 15 | qwen3.8-27b | 1600 w | 269.1 | — | 378.4 | 384.6 | 1643 | 2 | — | stop | `OK` |

"—" means the field was not reported or not observed: qwen streamed no reasoning deltas and its usage has no `reasoning_tokens` field.

### 8.2 Per-model summary (MEASURED (client) times; PROVIDER-REPORTED tokens; n = 5 each)

| Model | ok / sent | First content ms min / median / max | Total ms min / median / max | Headers ms median | tokens_in Σ | tokens_out Σ (incl. reasoning) |
| :--- | :-: | :-: | :-: | -: | -: | -: |
| `openai/gpt-oss-20b` | 5 / 5 | 320.5 / 520.1 / 688.6 | 323.4 / 521.3 / 690.7 | 471.1 | 2483 | 267 |
| `openai/gpt-oss-120b` | 5 / 5 | 601.6 / 631.1 / 735.2 | 609.4 / 638.3 / 738.5 | 521.8 | 2483 | 265 |
| `qwen/qwen3.8-27b` | 5 / 5 | 261.0 / 280.2 / 391.3 | 263.5 / 283.9 / 393.2 | 272.3 | 2200 | 10 |

Notes on timing:
- **Answers were one word.** Because of that, first-content and total time are within a few ms of each other. These calls measure time-to-first-token and fixed overhead, **not** generation throughput for real answers.
- **Reasoning.** For the gpt-oss models, 40–54 reasoning tokens came before the visible `OK`, and their first reasoning delta arrived close to the response headers.
- **Call 1 latency.** Call 1 was the run's first request on a new HTTP connection. Its higher latency may include connection setup; that was **not measured separately**.
- **Groq-reported timings (PROVIDER-REPORTED, seconds, from `usage`):**
  - `queue_time`: gpt-oss-20b 0.067–0.286, gpt-oss-120b 0.313–0.374, qwen 0.048–0.154;
  - `total_time`: gpt-oss-20b 0.058–0.150, gpt-oss-120b 0.114–0.202, qwen 0.006–0.119.

  These are kept in `traces.jsonl` for stand-in calibration; they are Groq's numbers, not ours.

### 8.3 Token-per-character ratios (PROVIDER-REPORTED tokens ÷ our prompt characters; synthetic filler)

| Prompt | chars | gpt-oss-20b / 120b prompt tokens (ratio) | qwen3.8-27b prompt tokens (ratio) |
| :--- | -: | :-: | :-: |
| short | 92 | 95 (1.033) | 38 (0.413) |
| 400 w | 3453 | 499 (0.145) | 443 (0.128) |
| 1600 w | 13496 | 1699 (0.126) | 1643 (0.122) |

- **Fixed overhead.** Both gpt-oss models count about **56–57 more prompt tokens** than qwen for identical messages (95 vs 38, 499 vs 443, 1699 vs 1643). This looks like a fixed chat-template overhead. That is an INTERPRETATION; the difference itself is measured.
- **Large prompts.** On this synthetic filler: about 6.9–7.8 characters per token for the 400-word prompts and 7.9–8.2 for the 1,600-word prompts. The approved 3.0 characters-per-token fallback therefore **over-estimates** tokens for this text, which is the conservative direction. Real paper text, with numbers, symbols and LaTeX remnants, may tokenise differently; re-check after P03.

### 8.4 `max_completion_tokens=256` and streaming, per model

| Model | `max_completion_tokens=256` | Streaming (SSE + `include_usage`) |
| :--- | :--- | :--- |
| `openai/gpt-oss-20b` | Accepted (HTTP 200, no 4xx). All `finish_reason=stop`; peak completion was 64 tokens, so the limit was **never reached** and truncation behaviour is **not observed**. | Worked: a reasoning-delta stream (169–232 chars), then content; usage in the top-level `usage` field (`location=usage`) on all 5; `[DONE]` received (no incomplete-stream error). |
| `openai/gpt-oss-120b` | Accepted; all `stop`; 53 completion tokens each; limit not reached. | Worked: reasoning deltas (182 chars), then content; top-level `usage`, including `reasoning_tokens`; `[DONE]` received. |
| `qwen/qwen3.8-27b` | Accepted; all `stop`; 2 completion tokens each; limit not reached. | Worked: content deltas only (no reasoning deltas, no `reasoning_tokens`); top-level `usage`; `[DONE]` received. |

- **Request parameters.** `temperature=0.0` and `stream_options.include_usage=true` were accepted by all three models.
- **Usage placement.** The Groq `x_groq.usage` fallback was **not needed** in any call.
- **Not recorded.** The parser accepts reasoning text in either `delta.reasoning` or `delta.reasoning_content`, and the trace does not record which field Groq used. Delta counts were also not recorded per call.

### 8.5 Observed response headers (sanitised; identical names on all 15 inference responses)

Present on all 15:
`alt-svc`, `cache-control` (`no-cache`), `cf-cache-status`, `cf-ray`, `connection`, `content-type` (`text/event-stream`), `date`, `server` (`cloudflare`), `strict-transport-security`, `transfer-encoding`, `vary`, `via` (`1.1 google`), `x-groq-region` (`bom` on all 15; discovery saw `dls`), `x-request-id`, plus the six `x-ratelimit-*` headers.

Present on some only: `set-cookie`, which is stored as `[REDACTED]`. **No `retry-after`** header was seen; none is expected without a 429.

Example: call 11 (qwen, first call), verbatim from evidence:

```
x-ratelimit-limit-requests: 1000      x-ratelimit-remaining-requests: 999
x-ratelimit-reset-requests: 1m26.4s   x-ratelimit-limit-tokens: 8000
x-ratelimit-remaining-tokens: 7797    x-ratelimit-reset-tokens: 1.522s
x-request-id: req_01m76w6z31ectt7ysj1932x6c4   x-groq-region: bom
```

### 8.6 Rate-limit headers: observations vs interpretation

MEASURED raw values, in call order. All parsed with 0 `parse_issues`.

| Model | limit-req | remaining-req | reset-req (s) | limit-tok | remaining-tok | reset-tok (s) |
| :--- | -: | :--- | :--- | -: | :--- | :--- |
| gpt-oss-20b | 1000 | 999, 998, 997, 996, 995 | 86.4, 172.8, 259.2, 345.6, 432.0 | 8000 | 7696, 7650, 7542, 7055, 5375 | 2.28, 2.625, 3.435, 7.087, 19.687 |
| gpt-oss-120b | 1000 | 999, 998, 997, 996, 995 | 86.4 … 432.0 (same steps) | 8000 | 7649, 7586, 7519, 7249, 5392 | 2.632, 3.105, 3.607, 5.632, 19.56 |
| qwen3.8-27b | 1000 | 999, 998, 997, 996, 995 | 86.4 … 432.0 (same steps) | 8000 | 7797, 7789, 7781, 7368, 5774 | 1.522, 9.9, 9.9, 9.9, 17.665 |

**Header meanings vs the ARCH §9.1 hypotheses:**
- **`*-requests` (hypothesis: RPD).** *Consistent, not formally verified.*
  - The limit is 1000. The remaining count drops by exactly 1 per call, and each model has its **own counter**: each restarted at 999.
  - The reset grows by exactly 86.4 s per request used. That equals 86,400 s / 1,000, which matches a per-day limit that replenishes over 24 h.
  - Formal verification would mean watching replenishment over hours. Not done.
- **`*-tokens` (hypothesis: TPM).** *Consistent with a minute-scale window; accounting not reconciled.*
  - The limit is 8000 per model; resets are seconds-scale (1.5–19.7 s).
  - **The drop in `remaining-tokens` does not equal the reported `usage.total_tokens`.** For example, the first gpt-oss-20b call reported 146 total tokens, but the remaining count fell by 304. The first gpt-oss-120b call fell by 351, which equals prompt 95 + `max_completion_tokens` 256, but the other models do not follow that pattern exactly. Replenishment happens between calls, and the run lasted only about 7.6 s.
  - INTERPRETATION, *unverified*: Groq may count `max_completion_tokens` or an estimate up front. If so, the 256 setting directly reduces minute headroom. This matters for ProviderGuard reservation (ARCH §9.3) and should be checked deliberately later. It is not resolved from this data.
- **RPM and TPD:** **not present in any header, so not measured.** They must still come from the Groq console/docs, or from a 429 body if the owner approves a minute-limit probe.
- **Daily safety fraction:** the most-used request header showed 5/1000 = 0.5 % used, far below 0.75. No stop rule fired: no 429, no auth failure, no cap reached.

## 9. Errors and Root-Cause Analysis

None. All 15 requests returned HTTP 200 with a complete stream and provider usage.

## 10. Fixes Applied

None required.

## 11. Outstanding Issues

1. **Token accounting.** `remaining-tokens` vs `usage` is unreconciled (§8.6). Whether Groq reserves `max_completion_tokens` against TPM is unverified.
2. **Limits not exposed in headers.** RPM and TPD values are **not measured**. Table 1 cells for them must say "not measured" unless taken, with citation, from the Groq console/docs.
3. **No truncation observed.** No call reached `max_completion_tokens`, so behaviour at the limit (`finish_reason=length`, especially for reasoning models that may spend the budget on reasoning) is unobserved.
4. **What the timings describe.** Latency is from n = 5 sequential calls per model with tiny outputs and synthetic prompts, at one time of day. It is not a latency distribution and not representative of real answers. Stand-in calibration (P09) should draw on later real runs (P07/P10) as well.
5. **Not recorded in traces:** which reasoning field Groq used, and per-call delta counts. Neither is needed so far.
6. **`git_dirty=true`** in the manifest, because the Step-1 doc edits are uncommitted. The code run was commit `2b5d017`, plus comment-only edits.
7. **No model selected, and no quality claims.** This run measures calibration only; every answer was `OK`.

## 12. Acceptance-Criteria Status

| Criterion | Status | Evidence |
| :--- | :--- | :--- |
| Dry run with real `.env`; plan = 3 approved models, ≤ 15 calls | `PASS` | programmatic check `True`; `planned_calls=15`; `live_blocked_by=[]` |
| Live baseline executed exactly once with `--max-real-calls 15` | `PASS` | single invocation; `real_calls_sent=15`; manifest `real_call_count=15` |
| No discover / minute-limit / extra / retry calls | `PASS` | only one `baseline --live` command; 15 trace records = 15 planned |
| Day-1 total ≤ 40 | `PASS` | 1 + 15 = 16 |
| Per-model latency, first token, tokens, success, headers reported | `PASS` | §8.1–§8.6 |
| `max_completion_tokens=256` and streaming per model explained | `PASS` | §8.4 |
| Raw headers kept in sanitised evidence format | `PASS` | `traces.jsonl` `response.headers` + parsed `rate_limits` |
| Evidence security scan | `PASS` | §13 |
| Header meanings verified | `PARTIAL` | consistent with RPD/TPM hypotheses; not formally verified; token accounting unreconciled |

## 13. Remaining Risks

**Evidence security-scan result: PASS.** The scan printed booleans and counts only.

| File | Bytes | Exact key present | Pattern hits |
| :--- | -: | :--- | :--- |
| `manifest.json` | 1462 | False | none |
| `rows.csv` | 3581 | False | none |
| `summary.json` | 2451 | False | none |
| `traces.jsonl` | 34099 | False | 1 × "cookie": the `set-cookie` header **name**. Unredacted cookie headers: **0** |

The patterns scanned were `gsk_…`, `Bearer …`, `authorization`, `org_…`, `sk-…`, `api_key`, `cookie`, `user_` / `proj_` / `acct_` ids, and email addresses.

**Risks carried forward:**
- **TPM headroom.** If Groq reserves `max_completion_tokens`, TPM headroom is tighter than usage suggests. With an 8000 TPM limit per model, a ~1,700-token RAG prompt plus 256 reserved leaves room for only about 4 such calls per minute per model. That is an *estimate* derived from observed headers, not a measured throughput.
- **Real answers.** Real answers need far more than 2–64 completion tokens, and reasoning models also spend tokens on reasoning. `max_completion_tokens=256` may truncate real cited answers; this needs checking in P05/P07.

## 14. Recommended Next Phase

Owner review of this report. Remaining owner decisions:
- whether to run the optional minute-limit probe, which needs separate approval;
- the final model, after the P07 quality comparison; this calibration run does not decide it.

The next build phase in the plan is **P03 (PDF ingestion)**. It will not start until the owner says so.

Stopping here.
