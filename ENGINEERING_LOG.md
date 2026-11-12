# ENGINEERING_LOG.md — The Relay Central Project History

> **Governance Rule:** Append-only chronological execution history. Never overwrite, delete, or sanitize earlier failures, blockers, or deviations. Never record API keys, secrets, or sensitive credentials.
> **Note:** This log records engineering execution and decisions. It does NOT replace the unedited AI assistant conversation history required by `ASSIGNMENT.md`.

---

### Execution Entry

- **Date and time:** 2026-11-11 12:40 IST
- **Phase identifier:** `phase-00` (Architecture Review, Finalization & Governance Setup)
- **Task requested:** Review official assignment PDF, architecture diagrams, and initial architecture proposal; correct inconsistencies; establish permanent project documentation (`ASSIGNMENT.md`, `CLAUDE.md`, `ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`, `ENGINEERING_LOG.md`, `reports/IMPLEMENTATION_REPORT_TEMPLATE.md`, `reports/phase-00-architecture-review.md`) without implementing application source code.
- **Objective:** Finalize a production-oriented, verifiable, 3-day-executable system architecture and establish strict Claude Code implementation governance before any coding starts.
- **Status:** completed (awaiting owner architecture approval)
- **Files created:**
  - `ASSIGNMENT.md`
  - `CLAUDE.md`
  - `ARCHITECTURE.md`
  - `IMPLEMENTATION_PLAN.md`
  - `ENGINEERING_LOG.md`
  - `reports/IMPLEMENTATION_REPORT_TEMPLATE.md`
  - `reports/phase-00-architecture-review.md`
- **Files changed:** None (`.git` preserved; zero source files created or modified)
- **Decisions made (proposed — awaiting owner approval):**
  - Enforced **Verify-Before-Delivery** for unverified LLM outputs: stream upstream from provider to measure `provider_first_token_ms`, verify citation integrity deterministically, then emit to client (`first_token_ms` = user-visible TTFT).
  - Corrected Groq response header mapping (`x-ratelimit-*-requests` = RPD, `x-ratelimit-*-tokens` = TPM; RPM & TPD tracked locally and via 429 messages/console).
  - Separated Ingress Admission/Bounded FIFO Queue (`IngressGate`) from Upstream Provider Quota & Resilience (`ProviderGuard`) with single-owner rate-limit control.
  - Restricted `results.csv` `answer_grade` strictly to assignment values (`correct`, `wrong`, `refused_correctly`, `refused_wrongly`) or blank `""` when no answer was generated; preserved `results.csv` in repo root (not gitignored).
  - Anchored deployment explicitly to a single-process Uvicorn runtime (`workers=1`) and explained distributed scaling trade-offs.
  - Made L1 Exact Cache + SingleFlight the baseline (`CACHE_MODE=exact`) and L2 Guarded Semantic Cache an independently toggleable optimization (`CACHE_MODE=semantic`) whose thresholds must be empirically calibrated rather than hardcoded.
  - Added explicit non-commit rule to `CLAUDE.md` forbidding `git commit` or `git push` by Claude Code.
- **Commands executed:** Read-only directory inspection (`Get-ChildItem -Force`) and live web verification against official Groq documentation (`console.groq.com/docs/rate-limits`, `console.groq.com/docs/models`). No Groq inference API calls were made; no source code or test commands run.
- **Actual outputs:** Confirmed `c:\Users\ASUS\Desktop\The-Relay` initially contained only `.git`. Confirmed current Groq header semantics and active reasoning/non-reasoning model docs.
- **Tests performed:** None (documentation/architecture phase only; zero source code exists yet).
- **Test results:** N/A
- **Errors encountered:** None.
- **Root cause:** N/A
- **Fix applied:** N/A
- **Validation after fix:** Verified all 7 governance/architecture markdown files exist and cross-reference consistently with zero source files created.
- **Outstanding issues:** Unresolved owner architectural approvals listed in `ARCHITECTURE.md` and `reports/phase-00-architecture-review.md`.
- **Deviations:** Corrected earlier proposal defects identified during Part 1 review (streaming-before-verify, `x-ratelimit-*-requests` misinterpretation, `answer_grade="na"`, `.gitignore` path conflict, uncalibrated semantic thresholds).
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `ARCHITECTURE.md`, `reports/phase-00-architecture-review.md`, and approves unresolved decisions before authorizing Phase 1 in Claude Code.

---

### Execution Entry

- **Date and time:** 2026-11-11 13:14 IST
- **Phase identifier:** `phase-00b` (Architecture Reconciliation — documentation only)
- **Task requested:** Incorporate the owner's Phase 00B decisions (exact cache + SingleFlight only; buffered verification with safe refusal and no repair call; harness-owned `results.csv` with per-run storage and duplicate-safe aggregation; client abandonment as `timeout`; Day-1 provider integration and budget-capped probing with approval gates; `openai/gpt-oss-20b` as candidate only; owner-written eval questions; tracked evidence directories) and corrections (derived queue capacity; header semantics to be verified; usage-based token reconciliation; one worker with documented SingleFlight limits; retrieval-quality runs isolated from cache/load runs; no unnecessary tools/repair loops/infrastructure; 3-day plan with final-day buffer) into `ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md` and `CLAUDE.md`; preserve `ASSIGNMENT.md`; write `reports/phase-00b-architecture-reconciliation.md`.
- **Objective:** Produce a contradiction-free v2 documentation set ready for owner approval before P01.
- **Status:** completed (documentation only; awaiting owner review)
- **Files created:** `reports/phase-00b-architecture-reconciliation.md`
- **Files modified:**
  - `ARCHITECTURE.md` — rewritten as v2 (status `REVISED — OWNER DECISIONS INCORPORATED; FORMAL APPROVAL PENDING`; decision tags [OWNER-DECIDED]/[PROPOSED]/[OPEN]/[TO MEASURE]).
  - `IMPLEMENTATION_PLAN.md` — rewritten as v2 (re-ordered 3-day schedule P01–P11 + P02-LIVE + P04B + submission buffer; real-call budget; cut line).
  - `CLAUDE.md` — §2 item 2 (decision-tag semantics), §5 item 6 (updated examples), §5 new items 12–16 (rate-limit/daily-quota rule, single `results.csv` writer, evidence protection, owner-authored questions, no unverified header/tokenizer/model assumptions).
  - `ENGINEERING_LOG.md` — this entry appended; earlier entry untouched.
  - `ASSIGNMENT.md` — **not modified**.
- **Decisions recorded:** see `reports/phase-00b-architecture-reconciliation.md` §5 (owner decisions) and §6 (engineering decisions made within them, e.g. SQLite → in-memory cache, tiktoken removed, word-based chunk sizes, `server_total_ms` column, `evidence/` + `scratch/` layout).
- **Commands executed:** `date "+%Y-%m-%d %H:%M %Z"` → `2026-11-11 13:14 IST`; `git status --short` → clean (before edits). Earlier in this session (Phase 00 review, read-only): `python --version` → `Python 3.13.5`; `uv --version` → `uv 0.12.5`; `pip --version` → `pip 25.1 (python 3.13)`. Final consistency checks listed in the report §6.
- **Tests performed:** None (no source code exists; documentation-only phase).
- **Test results:** N/A
- **Errors encountered:** None blocking. IDE markdown-lint warnings (MD022/MD031/MD032/MD040: blank lines around headings/lists/fences, fence language) on the rewritten docs — cosmetic, not fixed.
- **Root cause / fix / validation:** N/A
- **Outstanding issues:** Open decisions listed in `ARCHITECTURE.md` §16. Groq header semantics, limits, latency, and pricing remain unverified (no live calls made). No network access used in this phase.
- **Deviations from previous ARCHITECTURE.md (v1):** semantic cache removed (excluded); SQLite cache removed; `fetch_section_context` tool removed; repair loop removed; fixed queue size 16 withdrawn; tiktoken removed; `runs/` replaced by tracked `evidence/` + ignored `scratch/`; server no longer writes `results.csv`; `status` for verifier-blocked answers changed from `error` to proposed `ok` (open decision); phases renumbered.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-00b-architecture-reconciliation.md`, resolves `ARCHITECTURE.md` §16 items (at minimum 1–3 before P01), marks `ARCHITECTURE.md` approved, then authorises P01.

---

### Execution Entry

- **Date and time:** 2026-11-11 13:59 IST
- **Phase identifier:** `p01` (Foundation, Contracts, Run Directories and Aggregation)
- **Task requested:** Implement P01 per `IMPLEMENTATION_PLAN.md` v2: package skeleton, typed config, frozen strict Pydantic contracts, error hierarchy, timing helpers, run-directory/manifest management, deterministic `results.csv` aggregation via `scripts/build_results.py`. Record owner's final decisions. Do not install dependencies; provide install/test/lint/validation commands for the owner to run in Conda env `the-relay` (Python 3.12).
- **Objective:** Establish the `results.csv` contract and evidence storage that every later phase writes into.
- **Status:** in progress — implementation written; **verification pending** (owner to run install, pytest, ruff and validation commands; results not yet observed).
- **Owner decisions recorded (2026-11-11):** (1) verification-failure refusal delivered as `status=ok` + `error_code=citation_verification_failed`; (2) Groq inference probes appear in `results.csv` as `probe_*` scenarios with `retrieval_setting=none` and blank grade, details in evidence files; (3) every factual sentence must carry an appropriate citation; (4) Architecture v2 is the approved baseline, with [PROPOSED]/[OPEN]/[TO MEASURE] items kept open.
- **Files created:** `.gitignore`, `pyproject.toml`, `.env.example`, `README.md`, `evidence/README.md`, `evidence/canonical_runs.json`, `src/relay/__init__.py`, `src/relay/config.py`, `src/relay/errors.py`, `src/relay/redaction.py`, `src/relay/schemas.py`, `src/relay/telemetry/__init__.py`, `src/relay/telemetry/spans.py`, `src/relay/telemetry/csv_codec.py`, `src/relay/telemetry/run_store.py`, `src/relay/telemetry/aggregation.py`, `scripts/build_results.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/factories.py`, `tests/test_schemas.py`, `tests/test_foundation.py`, `tests/test_run_store_and_aggregation.py`, `reports/phase-01-foundation-contracts.md`.
- **Files modified:** `ARCHITECTURE.md` (status → approved baseline; §7.3(a), §13.2, §13.4, §16 updated with owner decisions; error codes `provider_auth_error`, `payload_too_large`, `internal_error` added; client/server join note); `ENGINEERING_LOG.md` (this entry).
- **Decisions made:** see `reports/phase-01-foundation-contracts.md` §5 (strict Literal-typed contracts, explicit CSV codec, invariant set, run_id format and exclusive run dirs, aggregation ordering by `(run_id, request_id)`, overwrite refusal, network-blocking test fixture, `relay/redaction.py` added beyond the plan's file list, tests split into three files).
- **Security finding at start:** `git status` showed the owner's new `.env` as untracked and `git check-ignore .env` exited 1 (not ignored). `.gitignore` was written first; afterwards `.env` no longer appears in `git status --short`. `.env` contents were not read.
- **Commands executed (by Claude):** `git status --short`; `git log --oneline -3`; `ls -la`; `git diff --stat HEAD -- ARCHITECTURE.md IMPLEMENTATION_PLAN.md ASSIGNMENT.md` (no output: docs equal HEAD `0442f23`); `git check-ignore -v .env` → exit 1 (before `.gitignore`); `python -m py_compile src/relay/*.py src/relay/telemetry/*.py scripts/build_results.py tests/*.py` using Anaconda **base** Python 3.13.5 (not `the-relay`) → `py_compile OK` (syntax only); `awk` line-length scan → 3 lines >100 chars found (config.py:67, schemas.py:226, test_run_store_and_aggregation.py:254), all fixed; generated `__pycache__` removed; `date` → `2026-11-11 13:59 IST`.
- **Tests performed:** none executed yet. pytest and ruff have **not** been run in `the-relay`; no pass/fail claim is made.
- **Errors encountered:** over-long lines (above) — fixed by reformatting; placeholder code left in `load_canonical_runs` during drafting — replaced with real run_id format validation before handoff.
- **Remaining issues:** test and lint results pending; P01 cannot be marked complete until the owner's output is reviewed.
- **Deviations from ARCHITECTURE.md:** none to binding decisions. Implementation notes added to ARCH §13.2 (join done by harness, aggregation validates) and §13.4 (three additional error codes).
- **Owner approval status:** pending owner review
- **Next action:** Owner runs the commands in `reports/phase-01-foundation-contracts.md` §6 and shares the terminal output; Claude reviews it, fixes any failures, then updates this log and the report with observed results.

---

### Execution Entry

- **Date and time:** 2026-11-11 14:16 IST
- **Phase identifier:** `p01` (verification and fixes)
- **Task requested:** Owner reported failing test cases; asked Claude to investigate, fix, and run the checks itself.
- **Status:** completed (pending owner review)
- **Environment:** `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`, Python 3.12.16. The owner had already installed the project (`pip show`: the-relay 0.1.0 editable; pydantic 2.14.3, pydantic-settings 2.16.2, pytest 9.2.0, ruff 0.17.4). Claude installed nothing.
- **Files modified:** `src/relay/config.py` (snapshot flag renamed), `tests/test_foundation.py`, `tests/test_run_store_and_aggregation.py`, `tests/test_schemas.py`, `reports/phase-01-foundation-contracts.md` (results sections filled in), `ENGINEERING_LOG.md` (this entry).
- **Commands executed and observed output:**
  - `python -m pytest -q` → `1 failed, 72 passed in 0.65s` (failed: `test_key_never_appears_in_repr_snapshot_or_errors`, `assert '[REDACTED]' is True`).
  - After fix 1: `python -m pytest -v` → `73 passed in 0.49s`; `python -m ruff check .` → 2 errors: E501 `tests/test_run_store_and_aggregation.py:254`, SIM300 `tests/test_schemas.py:21`.
  - `python -m pip check` → `No broken requirements found.`
  - `git check-ignore -v -n ...` → `.env` ignored (`.gitignore:2`); `.env.example` matched only by negation `!.env.example` (not ignored); `results.csv`, `evidence/canonical_runs.json`, `evidence/experiments/exp-x/rows.csv` → `::` (not ignored); `scratch/x.csv` ignored (`.gitignore:20`).
  - `python scripts/build_results.py --check` → `{"outcome": "checked", "rows": 0, "runs": 0, "would_change": true}`, exit 0.
  - After fixes 2–3 (final): `python -m pytest -q` → `73 passed in 0.56s`; `python -m ruff check .` → `All checks passed!`.
  - Fresh determinism check (old scratch outputs deleted first): two `build_results.py --output scratch/...` runs → both `written`; byte comparison → `IDENTICAL 357 bytes` (header-only, 29 columns).
- **Errors, root causes, fixes:**
  1. Snapshot flag `groq_api_key_present` masked by the conservative redactor (any key containing `api_key`). Fix: renamed to `groq_credential_configured`; redaction rules unchanged. Validated: test passes.
  2. E501 at line 254: my earlier long-line fix had edited a different line in that file, and I did not re-scan. Fix: split the call. Validated: ruff clean.
  3. SIM300 Yoda comparison. Fix: `expected == MANDATORY_COLUMNS`. Validated: ruff clean.
- **Tests performed:** full suite, final run: 73 passed, 0 failed, 0 skipped; no warnings (`filterwarnings=error`).
- **Remaining issues:** the VS Code Pyright "Import relay... could not be resolved" diagnostics come from the editor using a different interpreter, not from code defects. The owner should select the `the-relay` interpreter.
- **Deviations from ARCHITECTURE.md:** none.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-01-foundation-contracts.md`; on approval, authorise P02 (offline provider client and probe tooling).

---

### Execution Entry

- **Date and time:** 2026-11-11 16:46 IST
- **Phase identifier:** `p02` (Offline Provider Client and Probe Tooling)
- **Task requested:** Implement P02 offline only: async `httpx` provider client (streaming + non-streaming, SSE, usage, rate-limit header capture, typed errors, no retries), model discovery, capped probe tooling with dry-run and offline paths, MockTransport tests. No P02-LIVE, no real Groq calls, no commits.
- **Objective:** Provider communication layer and probe tooling ready for the owner-approved P02-LIVE measurements that will later calibrate the Stand-In.
- **Status:** completed (offline scope), pending owner review
- **Files created:** `src/relay/providers/__init__.py`, `src/relay/providers/errors.py`, `src/relay/providers/headers.py`, `src/relay/providers/sse.py`, `src/relay/providers/base.py`, `src/relay/providers/http_provider.py`, `src/relay/providers/probing.py`, `scripts/probe_groq.py`, `tests/test_http_provider.py`, `tests/test_probe_groq.py`, `reports/phase-02-provider-client.md`.
- **Files modified:**
  - `tests/conftest.py`: the network guard now also patches httpx's real sync/async transports to refuse non-loopback hosts, because asyncio's Windows proactor sockets bypass `socket.connect`.
  - `README.md`: probe commands and safeguards.
  - `ENGINEERING_LOG.md`: this entry.
  - No P01 source module changed. No dependency installed or added.
- **Decisions made:** see report §5. Highlights:
  - Transport `retries=0`; exactly one request per call; the retryable flag is a hint for P06.
  - Stall timer resets only on SSE data events.
  - Error codes reuse `relay.schemas.ErrorCode`.
  - Header meanings labelled `documented_unverified`, with the raw headers kept.
  - Model metadata is tolerated field by field.
  - Live probes need `--live` + `--max-real-calls` + `MAX_REAL_CALLS` + key; `minute-limit` also needs `--owner-approved-rate-limit-probe`, even for a dry run.
  - Probe records go to `traces.jsonl`.
- **Commands executed and observed output** (Git Bash, `the-relay` Python 3.12.16; `.env` never read):
  - `git status --short` → clean at start; HEAD `d3c7bfa`.
  - Version check → `httpx 0.28.1`, `pytest-asyncio 1.4.1.post1`.
  - `python -m pytest -q`, run 1 → `2 failed, 161 passed`. Both were test bugs (see errors).
  - After the fixes → `163 passed`; `python -m ruff check .` → 3 errors.
  - After the ruff fixes (final): `python -m ruff check .` → `All checks passed!`; `python -m pytest -q` → `163 passed in 2.08s`; new files alone → `90 passed in 1.75s`.
  - CLI smoke with `--env-file scratch/none.env`: `discover` dry run → exit 0; `baseline --max-real-calls 5` dry run → exit 0 (blocked by `MAX_REAL_CALLS=0` and the missing key); `minute-limit --live` without approval → exit 2; `discover --live --max-real-calls 1` → exit 2.
  - `ls evidence` → unchanged (`README.md`, `canonical_runs.json`).
- **Tests performed:** full suite, 163 passed / 0 failed / 0 skipped, no warnings (`filterwarnings=error`). Per file: http_provider 75, probe_groq 15, schemas 29, foundation 21, run_store/aggregation 23.
- **Errors, root causes, fixes:**
  1. Dry-run test expected an env-cap problem with both caps at 0. The expectation was wrong; the test now uses CLI cap 5 with env cap 0.
  2. Aggregation test used `summary.rows`; the field is `row_count`.
  3. Ruff: one split-constant finding and two E501s; fixed.
  4. Drafting bug, caught before the first test run: `collect_stream` used `or` for first timestamps; changed to an `is None` check.
  - All validated by the final runs above.
- **Real Groq calls:** 0. No network access to any external host.
- **Remaining issues / deviations:** see report §11.
  - Evidence file name conflict in the docs: `probes.jsonl` (ARCH §13.1, plan P02-LIVE) vs `traces.jsonl` (ARCH §13.2, run_store).
  - Daily-scope 429 → `daily_budget_exhausted`: mapping needs owner confirmation.
  - Every header meaning and Groq wire detail is unverified until P02-LIVE.
  - New [PROPOSED] defaults: timeouts, chars-per-token estimate, baseline sizes.
  - No deviation from binding architecture decisions.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-02-provider-client.md` and decides §11 items 1–2. P02-LIVE starts only after explicit approval of the key, the `MAX_REAL_CALLS` cap and the `discover` run.

---

### Execution Entry

- **Date and time:** 2026-11-12 (before the P02-LIVE discovery run)
- **Phase identifier:** `p02-live` (owner approvals and decisions recorded)
- **Task requested:** Record P01/P02 approval and the owner's decisions in the docs; then run P02-LIVE Step 1 (discovery) only.
- **Status:** completed (documentation step)
- **Owner approvals:** P01 and P02 are approved. Both reports are marked `Owner Approved (2026-11-12)`. The owner committed P02 as `2b5d017`; Claude did not commit.
- **Owner decisions applied:**
  1. `traces.jsonl` is the per-call evidence file for probe runs. `probes.jsonl` was removed from ARCH §13.1 and from IMPLEMENTATION_PLAN P02-LIVE outputs and verification.
  2. Upstream daily-scope HTTP 429 → `daily_budget_exhausted`, `retryable=false`. Added as a row in ARCH §13.4.
  3. P02 defaults approved and recorded in ARCH §16:
     - timeouts: connect 5 s, read 30 s, write 10 s, pool 5 s, stall 30 s, total 120 s;
     - 3.0 characters/token fallback estimate;
     - baseline plan (3 short calls + 400/1600-word prompts, `max_completion_tokens=256`, ≤ 3 models; minute-limit `max_completion_tokens=8`).
  4. `daily_safety_fraction = 0.75`: ARCH §9.4 and §16 item 5 resolved; IMPLEMENTATION_PLAN budget text; comments in `.env.example` and `src/relay/config.py` (the default value was already 0.75, so there is no code change).
  5. Day-1 real-call ceiling = 40 calls (IMPLEMENTATION_PLAN schedule and budget, P02-LIVE preconditions). Day 2/3 caps remain proposals.
- **Files modified:** `reports/phase-01-foundation-contracts.md`, `reports/phase-02-provider-client.md`, `ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`, `.env.example` (comment only), `src/relay/config.py` (comment only), `ENGINEERING_LOG.md`.
- **Pre-run checks:**
  - `pytest` → `163 passed`; `ruff check .` → `All checks passed!`.
  - `git check-ignore -v .env` → `.gitignore:2:.env` (ignored); `.env` is not tracked.
  - Settings loaded from `.env`, with only non-secret values and booleans printed:
    - `provider_mode=scripted`, `groq_base_url=https://api.groq.com/openai/v1`, `groq_model=openai/gpt-oss-20b`;
    - `max_real_calls=40`, `daily_safety_fraction=0.75`;
    - key configured = True, `gsk_` prefix = True, whitespace/quotes = False.
  - The key value was not printed.
- **Owner approval status:** approvals recorded; the discovery run follows.

---

### Execution Entry

- **Date and time:** 2026-11-12 08:41 IST (run started 2026-11-12T03:11:08Z, ended 03:11:09.066Z)
- **Phase identifier:** `p02-live` Step 1 (model discovery only)
- **Task requested:** Execute `python scripts/probe_groq.py discover --live --max-real-calls 1` exactly once; inspect and security-scan the evidence; report. No baseline, no minute-limit, no inference, no model selection, no re-run.
- **Status:** completed
- **Command executed:** `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe scripts/probe_groq.py discover --live --max-real-calls 1`, run **once** from the project root. Exit 0. Output: `outcome=completed`, `planned_calls=1`, `attempted_calls=1`, `real_calls_sent=1`, `rows_written=0`, `stopped_reason=null`.
- **Real requests sent:** **1** (`GET /openai/v1/models`). The day's real-call total is 1 of the 40-call Day-1 ceiling.
- **Evidence run:** `probe-20261112T031108Z-2b5d017` → `evidence/groq_probes/probe-20261112T031108Z-2b5d017/`. It contains:
  - `manifest.json`: status complete, `real_call_count=1`, `git_dirty=true` because of the uncommitted doc edits;
  - `rows.csv`: header only, since discovery is not inference;
  - `traces.jsonl`: 1 record;
  - `summary.json`.
- **Result:** HTTP 200, discovery succeeded, 0 metadata issues, client total 559.269 ms (a single observation).
  - **11 models, all `active=true`:** `allam-2-7b`, `canopylabs/orpheus-arabic-saudi`, `canopylabs/orpheus-v1-english`, `meta-llama/llama-prompt-guard-2-22m`, `meta-llama/llama-prompt-guard-2-86m`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `openai/gpt-oss-safeguard-20b`, `qwen/qwen3.8-27b`, `whisper-large-v3`, `whisper-large-v3-turbo`. Context-window and max-completion values are in the report §8.1.
- **Rate-limit headers:** **none** on the `/models` response (no `x-ratelimit-*`, no `retry-after`), so no ARCH §9.1 hypothesis is verified yet.
- **Other headers:** `x-request-id`, `x-groq-region: dls` and Cloudflare/Google edge headers; `set-cookie` redacted.
- **Security scan** (Python; counts/booleans only, no values printed):
  - the exact key value is absent from all 4 files;
  - 0 hits for `gsk_`, `Bearer`, `authorization`, `org_`, `sk-`, `api_key`, `user_` / `proj_` / `acct_` ids and emails;
  - the single "cookie" hit is the `set-cookie` header name, whose value is `[REDACTED]`.

  Result: **PASS**.
- **Post-run checks:** `pytest` → 163 passed; `ruff check .` → All checks passed! These ran after the final code-comment edits.
- **Errors:** none.
- **Files created:** the evidence run directory above; `reports/phase-02-live-discovery.md`.
- **Remaining issues:**
  - header semantics, limits, latency and token ratios are not measured (they need the baseline step);
  - no model selected;
  - the shortlist decision belongs to the owner.
- **Owner approval status:** pending owner review
- **Next action:** The owner reviews the report, confirms a shortlist of up to 3 models and approves a `baseline` cap. Claude does not proceed until then.

---

### Execution Entry

- **Date and time:** 2026-11-12 08:48 IST (run 03:18:16.680Z → 03:18:24.324Z)
- **Phase identifier:** `p02-live` Step 2 (baseline)
- **Task requested:** The owner approved the shortlist `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b` and a baseline on the approved defaults (3 short + 400 w + 1600 w per model, `max_completion_tokens=256`), with a hard cap of 15 real calls. Dry-run first, execute once, report. No discover, no minute-limit, no extra or retry calls, no model selection, no P03.
- **Status:** completed
- **Pre-run:**
  - `probe_groq.py --help` checked.
  - Dry run with the real `.env` and explicit flags → `dry_run`, `planned_calls=15`, `live_blocked_by=[]`.
  - Programmatic check: 3 approved models × 5 calls (`short ×3`, `prompt_400w`, `prompt_1600w`), all `max_completion_tokens=256` → **True**.
- **Command executed once:** `python scripts/probe_groq.py baseline --model openai/gpt-oss-20b --model openai/gpt-oss-120b --model qwen/qwen3.8-27b --calls-per-model 3 --prompt-words 400,1600 --max-completion-tokens 256 --max-real-calls 15 --live` (`the-relay` interpreter; sandbox lifted for network). Exit 0.
- **Real requests sent:** **15**, all HTTP 200, 0 failed, 0 retries, `stopped_reason=null`. **Day-1 cumulative: 16 of 40.**
- **Evidence:** `probe-20261112T031816Z-2b5d017` → `evidence/groq_probes/probe-20261112T031816Z-2b5d017/` (manifest complete with `real_call_count=15`; 15 rows; 15 traces; summary).
- **Measured results** (client-side ms, n = 5 per model; descriptive only, not percentiles):
  - gpt-oss-20b first content 320.5 / 520.1 / 688.6 (min / median / max), total median 521.3;
  - gpt-oss-120b first content 601.6 / 631.1 / 735.2, total median 638.3;
  - qwen3.8-27b first content 261.0 / 280.2 / 391.3, total median 283.9.
  - Provider usage on all 15 calls (top-level `usage`; the `x_groq` fallback was not needed).
  - gpt-oss models: 40–54 reasoning tokens, streamed as reasoning deltas before the content. qwen: no reasoning, 2 completion tokens.
  - All `finish_reason=stop`; `max_completion_tokens=256` accepted and never reached.
  - Prompt tokens: gpt-oss 95 / 499 / 1699 vs qwen 38 / 443 / 1643 for identical messages (about 56–57 tokens of fixed overhead on gpt-oss).
- **Headers (measured):**
  - `x-ratelimit-limit-requests=1000` and `x-ratelimit-limit-tokens=8000` for each model.
  - `remaining-requests` decrements by 1 per call, with a separate counter per model. `reset-requests` grows by 86.4 s per request used: *consistent with* RPD (86,400 s / 1,000), **not formally verified**.
  - Token resets are seconds-scale: *consistent with* TPM. The `remaining-tokens` drop ≠ `usage.total_tokens`, which is **unreconciled**; Groq possibly reserves `max_completion_tokens` (unverified).
  - No `retry-after`. RPM and TPD are not in any header (**not measured**).
  - `x-groq-region=bom` on all 15.
- **Security scan:**
  - the exact key is absent from all 4 files;
  - 0 hits for `gsk_`, `Bearer`, `authorization`, `org_`, `sk-`, `api_key`, `user_` / `proj_` / `acct_`, emails;
  - 1 "cookie" hit = the `set-cookie` header name; unredacted cookie headers: 0.

  Result: **PASS**.
- **Errors:** none. **Code changes:** none, so the test suite was not re-run (last run 163 passed, ruff clean).
- **Files created:** the evidence run above; `reports/phase-02-live-baseline.md`; `scratch/baseline_dryrun.json` (gitignored).
- **Remaining issues:**
  - TPM header accounting unreconciled;
  - RPM / TPD not measured;
  - truncation at the limit not observed;
  - latency based on tiny outputs and synthetic prompts only;
  - `git_dirty=true` (uncommitted Step-1 docs).
  - No model selected; no quality claims.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-02-live-baseline.md`. Optional decision: a separately approved minute-limit probe. P03 does not start until the owner instructs it.
