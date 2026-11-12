# Phase 02-LIVE Implementation Report — Step 1: Groq Model Discovery

> **Phase ID:** `p02-live` (Step 1 only: `discover`)
> **Date:** `2026-11-12 08:41 IST` (run started `2026-11-12T03:11:08Z`)
> **Status:** `Completed` — discovery succeeded; **exactly 1 real request** sent
> **Owner Approval:** `Owner Approved (2026-11-12)`

---

## 1. Objective

Run the owner-authorised P02-LIVE Step 1 command exactly once:
`python scripts/probe_groq.py discover --live --max-real-calls 1`.

That is one `GET /openai/v1/models` request to Groq. Then inspect and security-scan the evidence, and report it.

Not in scope, and not done:
- `baseline`, `minute-limit` or any inference request;
- model selection;
- re-running the command.

## 2. Implementation Summary

No application code was changed. Before the run I applied the owner's decisions to the docs (ENGINEERING_LOG entry "p02-live — owner approvals and decisions recorded") and then checked:
- the test suite passed;
- `.env` is ignored by Git and untracked;
- the settings needed for a live run were present. Only non-secret values and booleans were printed;
- a no-network dry run of `discover --max-real-calls 1` reported no blocking safeguard (`live_blocked_by: []`).

I then executed the authorised command once. It completed and wrote a new evidence run directory. I then scanned that evidence for credentials.

## 3. Files Created

- `evidence/groq_probes/probe-20261112T031108Z-2b5d017/`, created by the probe script:
  - `manifest.json` (1364 bytes);
  - `rows.csv` (357 bytes, header only: `GET /models` is not inference, so it produces no row by design);
  - `traces.jsonl` (9452 bytes, one record);
  - `summary.json` (649 bytes).
- `reports/phase-02-live-discovery.md`: this report.

## 4. Files Modified

- `reports/phase-01-foundation-contracts.md`, `reports/phase-02-provider-client.md`: Owner Approval → `Owner Approved (2026-11-12)`.
- `ARCHITECTURE.md`:
  - §9.4: safety fraction 0.75, owner-decided;
  - §13.1: `traces.jsonl` (and `rows.csv`) for probe runs;
  - §13.4: new row "Upstream daily-scope 429 → rate_limited / `daily_budget_exhausted`, retryable=false";
  - §16: item 5 resolved; Day-1 ceiling 40; approved P02 defaults recorded.
- `IMPLEMENTATION_PLAN.md`:
  - the Day-1 ceiling of 40 is marked owner-decided, and the safety fraction 0.75;
  - the P02-LIVE outputs and verification now name `traces.jsonl`.
- `.env.example`, `src/relay/config.py`: comment only (`[PROPOSED]` → `[OWNER-DECIDED 2026-11-12]` for the safety fraction). The value was already 0.75.
- `ENGINEERING_LOG.md`: two entries appended.

## 5. Technical Decisions

- **Interpreter.** I ran the command with the `the-relay` interpreter (`C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`) from the project root. That is equivalent to `python` in the activated env.
- **Sandbox.** The tool sandbox was lifted for that single command only, so that network access could not cause an unrecoverable failure of the one authorised attempt.
- **Pre-check.** A dry run without `--live` was used first to confirm that no safeguard would refuse the live call. It sends nothing.
- **Security scan.** The scan prints only booleans and counts, never matched values. It checks:
  - whether the exact key value occurs;
  - for `gsk_…`, `Bearer …`, the word "authorization", `org_…`, `sk-…`, `api_key`, "cookie", `user_` / `proj_` / `acct_` ids, and email addresses.

## 6. Commands Actually Executed

```bash
PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe
$PY -m pytest -q -p no:cacheprovider        # 163 passed in 2.19s
$PY -m ruff check .                         # All checks passed!
git check-ignore -v .env                    # .gitignore:2:.env  .env   (exit 0 = ignored)
git ls-files --error-unmatch .env           # not tracked
$PY - <<'EOF'   # load_settings(".env"); print non-secret values + booleans only
...
EOF
#   provider_mode=scripted, groq_base_url=https://api.groq.com/openai/v1,
#   groq_model=openai/gpt-oss-20b, max_real_calls=40, daily_safety_fraction=0.75,
#   key configured=True, gsk_ prefix=True, whitespace/quotes=False
$PY scripts/probe_groq.py discover --max-real-calls 1      # dry run: dry_run 1 [] True
# THE authorised live command, executed exactly once:
$PY scripts/probe_groq.py discover --live --max-real-calls 1    # exit=0
# Evidence inspection and secret scan (Python; prints counts/booleans only).
```

Live command output (verbatim):

```json
{
  "attempted_calls": 1,
  "outcome": "completed",
  "planned_calls": 1,
  "real_calls_sent": 1,
  "rows_written": 0,
  "run_dir": "evidence\\groq_probes\\probe-20261112T031108Z-2b5d017",
  "run_id": "probe-20261112T031108Z-2b5d017",
  "stopped_reason": null
}
```

## 7. Test Results

- Before the run: `pytest` → 163 passed, 0 failed, 0 skipped; `ruff check .` → All checks passed!
- After the final edits (comment-only changes to `config.py` and `.env.example`, plus docs): re-run results are recorded in the ENGINEERING_LOG execution entry for this step.

## 8. Actual Output and Generated Artifacts (REAL Groq data)

**Discovery succeeded:** HTTP `200`, `status=ok`, 0 metadata issues, total client time **559.269 ms**. That is a single observation and not a latency estimate.

**Run ID and directory:**
- run ID: `probe-20261112T031108Z-2b5d017`
- directory: `evidence/groq_probes/probe-20261112T031108Z-2b5d017/`

**Manifest:**
- `status=complete`, `real_call_count=1`, `source=real`, `scenarios=["probe_discover"]`, `git_commit=2b5d017`;
- `git_dirty=true`, because the uncommitted doc edits from this step were in the working tree.

**Real requests sent: exactly 1.** The evidence agrees: manifest `real_call_count=1`, summary `real_calls_sent=1`, `attempted_calls=1`, budget `max_real_calls=1`.

### 8.1 Discovered models (11; all as listed by Groq on 2026-11-12)

| Model ID | owned_by | active | context_window | max_completion_tokens | input → output modalities |
| :--- | :--- | :--- | ---: | ---: | :--- |
| `allam-2-7b` | SDAIA | true | 4096 | 4096 | text → text |
| `canopylabs/orpheus-arabic-saudi` | Canopy Labs | true | 4000 | 50000 | text → speech |
| `canopylabs/orpheus-v1-english` | Canopy Labs | true | 4000 | 50000 | text → speech |
| `meta-llama/llama-prompt-guard-2-22m` | Meta | true | 512 | 512 | text → text |
| `meta-llama/llama-prompt-guard-2-86m` | Meta | true | 512 | 512 | text → text |
| `openai/gpt-oss-120b` | OpenAI | true | 131072 | 65536 | text → text |
| `openai/gpt-oss-20b` | OpenAI | true | 131072 | 65536 | text → text |
| `openai/gpt-oss-safeguard-20b` | OpenAI | true | 131072 | 65536 | text → text |
| `qwen/qwen3.8-27b` | Alibaba Cloud | true | 131072 | 16384 | text, image → text |
| `whisper-large-v3` | OpenAI | true | 448 | 448 | audio → transcription |
| `whisper-large-v3-turbo` | OpenAI | true | 448 | 448 | audio → transcription |

**Metadata observations:**
- Every entry has `created`.
- Groq also returns duplicate-named fields `context_length` and `max_output_length`, which equal `context_window` and `max_completion_tokens` for all 11 models.
- Extra fields vary by model: `name`, `public_apps`, `input_modalities`, `output_modalities`, `pricing`, `supported_features`, `supported_sampling_parameters`, `hugging_face_id`, `model_card`. This confirms that metadata is not uniform across models.
- A `pricing` object exists for 8 of the 11 models; it is absent for `allam-2-7b` and both Whisper models.
- The full entries are preserved verbatim in `traces.jsonl`.
- **Not listed by `/models`:** no RPM/TPM/RPD/TPD limits.

### 8.2 Observed sanitised response headers (`GET /models`)

```
alt-svc: h3=":443"; ma=86400
cache-control: private, max-age=0, no-store, no-cache, must-revalidate
cf-cache-status: DYNAMIC
cf-ray: a5925900aebeb232-DEL
connection: keep-alive
content-encoding: gzip
content-type: application/json
date: Thu, 12 Nov 2026 03:11:09 GMT
server: cloudflare
set-cookie: [REDACTED]
strict-transport-security: max-age=15552000
transfer-encoding: chunked
vary: Origin
via: 1.1 google
x-groq-region: dls
x-request-id: req_01m76vsq7ze76s17yf5dd314hc
```

### 8.3 Observed rate-limit headers

**None.** The `GET /models` response contained **no `x-ratelimit-*` and no `retry-after` header**. The parsed snapshot is entirely null, with `raw={}` and `parse_issues=[]`.

So this run verifies **none** of the ARCH §9.1 header hypotheses. They can only be checked on inference responses (the baseline step). This also suggests that `/models` calls may not be rate-limit-metered in the same way, but that is **not verified**.

## 9. Errors and Root-Cause Analysis

None. The single live request succeeded on the first and only attempt.

## 10. Fixes Applied

None required.

## 11. Outstanding Issues

1. **No rate-limit headers on `/models`.** Header semantics (§9.1), RPM/TPM/RPD/TPD values, latency spread and token-per-character ratios all remain **not measured**. They need the owner-approved `baseline` step.
2. **Free-tier limits.** `/models` exposes neither free-tier limits nor a tier label. Per-model limits still have to come from the Groq console/docs and from inference headers.
3. **Possible shortlist candidates, for owner decision only.** Of the listed models, the text→text chat models with ≥ 131072 context are `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`. `gpt-oss-safeguard-20b` and the prompt-guard models are safety classifiers, and the speech/audio models do not fit the task. No model has been selected; that is the owner's decision (ARCH §10).
4. **`git_dirty=true` in the manifest.** The doc edits from this step were uncommitted when the run started. The evidence still records commit `2b5d017` for the code that ran, and no code differed from that commit apart from comment-only edits.

## 12. Acceptance-Criteria Status

| Criterion | Status | Evidence |
| :--- | :--- | :--- |
| Authorised command executed exactly once | `PASS` | one invocation; `real_calls_sent=1`, manifest `real_call_count=1` |
| No baseline / minute-limit / inference request | `PASS` | only `discover` was run; `rows.csv` header-only; plan = `GET /models` |
| Discovery succeeded, models parsed | `PASS` | HTTP 200, 11 models, 0 metadata issues |
| Evidence written to a new run directory | `PASS` | `evidence/groq_probes/probe-20261112T031108Z-2b5d017/` |
| No key / authorization / org ID / credential leaked | `PASS` | §13 scan: exact key absent in all 4 files; 0 hits for every credential pattern; only `set-cookie` *name* present, value `[REDACTED]` |
| `.env` ignored and untracked | `PASS` | `git check-ignore` → `.gitignore:2`; `git ls-files` → not tracked |
| Header semantics verified | `PENDING` | no rate-limit headers on `/models`; requires baseline |

## 13. Remaining Risks

**Evidence security-scan result.** Per file: exact key present? / pattern hits.

| File | Exact key present | Pattern hits |
| :--- | :--- | :--- |
| `manifest.json` | False | none |
| `rows.csv` | False | none |
| `summary.json` | False | none |
| `traces.jsonl` | False | 1 × the word "cookie" — the `set-cookie` header **name**; value confirmed `[REDACTED]` |

The patterns scanned were `gsk_…`, `Bearer …`, `authorization`, `org_…`, `sk-…`, `api_key`, `cookie`, `user_` / `proj_` / `acct_` ids, and email addresses. **No sensitive value was found or printed.**

The `x-request-id` and `cf-ray` values are per-request routing identifiers, not credentials. They are kept for traceability.

**Risks carried forward:**
- The baseline step is where real tokens are first consumed. The safeguards (call cap, `MAX_REAL_CALLS=40`, daily safety fraction 0.75, stop on any 429) only take effect from response headers, and those must first be observed.
- Daily token limits are still not observable from headers.

## 14. Recommended Next Phase

**P02-LIVE Step 2 (`baseline`) only after owner review and approval.** Before that run, the owner needs to:
1. confirm a shortlist of at most 3 models from §8.1;
2. approve an explicit `--max-real-calls`. The default plan is 5 calls per model, so 15 for 3 models, within the Day-1 ceiling of 40, of which 1 is already used.

Stopping here; no model has been selected and no baseline call was made.
