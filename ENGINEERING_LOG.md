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

---

### Execution Entry

- **Date and time:** 2026-11-12 (start of P03)
- **Phase identifier:** `p02-live` approvals recorded
- **Owner approvals:**
  - P02-LIVE Step 1 (discovery, run `probe-20261112T031108Z-2b5d017`) and Step 2 (baseline, run `probe-20261112T031816Z-2b5d017`) are reviewed and accepted.
  - `reports/phase-02-live-discovery.md` and `reports/phase-02-live-baseline.md` are marked `Owner Approved (2026-11-12)`.
  - The owner committed the evidence as `0d7dfd9`.
- **Findings preserved for later phases (settings unchanged):**
  1. `max_completion_tokens=256` was accepted by all three models but never reached (peak 64 completion tokens), so truncation behaviour is unobserved.
  2. The 3.0 characters/token fallback may **underestimate** very short prompts, because a fixed message overhead is counted per request (gpt-oss: 95 prompt tokens for a 92-character prompt). For long prompts it overestimates.
- **Next action:** P03 (PDF ingestion) per the owner's instruction.

---

### Execution Entry

- **Date and time:** 2026-11-12 09:16 IST
- **Phase identifier:** `p03` (Research PDF Ingestion)
- **Task requested:** Fetch exactly `1706.03762v7`, `2410.00037v1` and `2507.07955v2` from their version-specific arXiv URLs; validate each and record ID, version, SHA-256, size and page count in the tracked manifest; extract text with PyMuPDF (page numbers, multi-column reading order); tag sections, references and metadata; chunk deterministically with stable IDs; test it all. No indexes, no generation, no Groq calls.
- **Status:** completed, pending owner review
- **Files created:** `src/relay/ingestion/{__init__,errors,arxiv_fetcher,pdf_parser,chunker,pipeline}.py`, `scripts/ingest_papers.py`, `tests/test_ingestion.py`, `data/papers/manifest.json` (tracked), `reports/phase-03-pdf-ingestion.md`. Gitignored and regenerable: the 3 PDFs in `data/papers/` and 12 files in `data/processed/`.
- **Files modified:** `.gitignore` (+`data/processed/`), `README.md` (ingestion commands), `reports/phase-02-live-{discovery,baseline}.md` (Owner Approved), `ENGINEERING_LOG.md`. No P01/P02 module was changed; nothing was installed.
- **Network:** one arXiv fetch session, 3 PDFs. **Groq calls: 0.**
- **Corpus (validated; each PDF carries the arXiv stamp `arXiv:<id>v<ver>` and its title on page 1):**

  | paper | SHA-256 | bytes | pages |
  | :--- | :--- | -: | -: |
  | `1706.03762v7` | `bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697` | 2,215,244 | 15 |
  | `2410.00037v1` | `a687144cc77bea86b0c60ae800ccb1d3e0816033fa5c7960388fe29a0dbfcfac` | 4,694,312 | 66 |
  | `2507.07955v2` | `04a2b32e6e82853cfc23c5db132750874a86cde99219ec6f664128f4aa83122f` | 1,375,282 | 39 |
- **Extraction:**
  - **Words:** Attention 5,716; Moshi 28,860; H-Net 20,341.
  - **Sections:** 27 / 48 / 38. Every PDF-outline entry was matched to a numbered heading (0 outline issues).
  - **References pages:** 10–12 / 42–52 / 25–30.
  - **Pages without text:** 0.
  - **Flagged pages** (text kept but less reliable): see report §8.4 — tables (row-wise cells, header/row merge), math (flattened equations), figure text removed (Moshi 34, 59; H-Net 3, 7, 15, 18, 38, 39), and low-text pages (Attention 13–15 figure pages, H-Net 24 acknowledgements).
- **Chunks** (presets [PROPOSED]):
  - `small_fast` 180/30 → 402 (49 / 207 / 146);
  - `balanced_rerank` 330/50 → 235 (32 / 119 / 84);
  - `large_wide` 600/90 → 164 (27 / 77 / 60).
  - Chunk-set SHA-256 values are recorded in the manifest.
- **Reproducibility:**
  - final `ingest_papers.py --offline` run twice → manifest and all 12 processed files byte-identical;
  - `--check` → `manifest_unchanged: true`, exit 0.
- **Tests:** `pytest -q` → **209 passed** (0 failed, 0 skipped); `tests/test_ingestion.py` → 46 passed, including the real-corpus test; `ruff check .` → All checks passed! These ran after the final code edit.
- **Errors found and fixed** (details in report §9–§10):
  - figure labels matched as headings → numbered match preferred;
  - figure pages detected as two-column → paragraph-sized blocks only;
  - the glyph filter dropped an equation and a table header → `=` guard;
  - several hyphenation rule errors → vocabulary of standalone words plus compound rules, reviewed per paper;
  - glued superscripts and interleaved author/affiliation blocks;
  - fixture sizing and MuPDF block-merging in tests;
  - 4 ruff findings.
- **Known limitations:**
  - flattened equations and merged table headers;
  - 4 questionable hyphen merges (`crossmatch`, `melspectrogram`, `bitsper`, `predecay`);
  - footnotes inline;
  - two-column ordering verified on synthetic PDFs only.
- **P02-LIVE findings preserved:** settings unchanged (256-token limit; 3.0 characters/token fallback).
- **Deviations from ARCHITECTURE.md:** none. Chunk presets remain [PROPOSED].
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-03-pdf-ingestion.md`. P04 starts only on explicit approval.

---

### Execution Entry

- **Date and time:** 2026-11-12 (start of P04)
- **Phase identifier:** `p03` approval recorded
- **Owner approval:** P03 reviewed and accepted. `reports/phase-03-pdf-ingestion.md` is marked `Owner Approved (2026-11-12)`. The owner committed P03 as `ccc933d`.
- **Next action:** P04 (retrieval indexes, scope analyzer, eval schema and grading tooling) per the owner's instruction.

---

### Execution Entry

- **Date and time:** 2026-11-12 10:13 IST
- **Phase identifier:** `p04` (Retrieval Indexes, Scope Analyzer, Evaluation Schema and Grading Tooling)
- **Task requested:**
  - BM25 + dense (FastEmbed ONNX, NumPy exact cosine) indexes over the P03 chunks; RRF hybrid retrieval with citations; at least 3 configurable settings;
  - finalise the chunk presets with a rationale;
  - reference handling; scope analyzer; eval schema, validator and manual grading tooling;
  - offline tests.

  Not to do: author questions, generate answers, call Groq, start P04B.
- **Status:** completed, pending owner review
- **Network:** one Hugging Face download session for the two planned models into gitignored `data/models/` (152 MB):
  - `Qdrant/bge-small-en-v1.5-onnx-Q` @ `aa8f8b060edb00e03bfdd08813a2949946c8ba55`, `model_optimized.onnx` SHA-256 `51f1bd0a…49f2431`;
  - `Xenova/ms-marco-MiniLM-L-6-v2` @ `a09144355adeed5f58c8ed011d209bf8ee5a1fec`, `onnx/model.onnx` SHA-256 `c623d0bc…2881870`.

  Every later load uses `local_files_only=True`. **Groq calls: 0.**
- **Files created:**
  - `src/relay/retrieval/{__init__,embedder,hybrid_index,scope}.py`, `src/relay/eval/{__init__,questions,grader}.py`;
  - `scripts/{build_indexes,validate_questions,grade_answers}.py`;
  - `eval/questions.template.json` (placeholders only), `eval/README.md`;
  - `tests/test_retrieval.py` (21), `tests/test_scope.py` (16), `tests/test_grader.py` (28);
  - `data/retrieval_manifest.json` (tracked);
  - `reports/phase-04-retrieval-and-evaluation-tooling.md`.

  **`eval/questions.json` was NOT created**; the owner authors it.
- **Files modified:**
  - `src/relay/ingestion/chunker.py`: `CHUNK_PRESETS` finalised; no logic change.
  - `data/papers/manifest.json`: regenerated; chunk sets only; paper checksums unchanged.
  - `ARCHITECTURE.md` §6: final presets and rationale, pending owner approval.
  - `.gitignore`: +`data/models/`.
  - `README.md`, `reports/phase-03-pdf-ingestion.md` (Owner Approved), `ENGINEERING_LOG.md`.
- **Final presets** (measured: 1.56 BGE tokens/word, p90 2.2; both models have a 512-token limit; the P03 proposals of 330/600 words exceeded 512 tokens for 34 % / 51 % of chunks):

  | preset | words / overlap | chunks |
  | :--- | :--- | -: |
  | `small_fast` | 160 / 25 | 442 |
  | `balanced_rerank` | 220 / 35 | 341 (16 over 512 tokens) |
  | `large_wide` | 400 / 60 | 204 (dense covered by 355 windows, 17 over 512) |

  - Dense windows: ≤ 220 words, step 180, best window scores the chunk.
  - Settings: top-k 4 / 4 (rerank 12) / 5.
  - RRF k = 60; references × 0.5 (fused) and −2.0 (rerank) unless reference intent is detected.
- **Determinism:**
  - `build_indexes.py --check` → `manifest_unchanged: true` with the real ONNX models (byte-identical vectors), including after the final edit;
  - `ingest_papers.py --check` → unchanged.
- **Commands and outputs:**
  - The first build was too slow (1 thread, batch 32) and was stopped.
  - Profiling on 64 chunks: 45.0 s (1 thread / batch 32) vs 2.8 s (default threads / batch 4). The fix is 4 fixed threads with batch size 4, and a full build takes 1 min 38 s.
  - New tests, run 1: `2 failed, 63 passed` (an empty-build guard ran too late; scope coverage was diluted by question words). After the fixes: 65 passed.
  - Ruff: 4 findings, all fixed.
  - **Final:** `pytest -q` → **274 passed**, 0 failed, 0 skipped; `ruff check .` → All checks passed!
  - Validator: the template is rejected (placeholders, exit 1); a missing `eval/questions.json` gives exit 1.
- **Smoke checks** (offline, not evaluation, not graded):
  - correct papers and sections for Mimi, routing-module and BLEU queries;
  - reference intent surfaces a References chunk;
  - **shared-vocabulary failure observed:** `large_wide` ranked H-Net first for a `d_model` query;
  - an off-topic query lands on Moshi App. F.1 (general-knowledge dialogue examples) with rerank scores ≈ −10;
  - latency ≈ 7–19 ms per query without rerank, ≈ 0.6–0.8 s with rerank.
- **P03 limitations preserved:** source text unchanged; listed as `garbled_extraction` trap candidates in `eval/README.md`.
- **Deviations from ARCHITECTURE.md:** none to binding decisions. The §6 preset values were finalised as the plan required, marked pending owner approval.
- **Owner approval status:** pending owner review
- **Next action:**
  - Owner reviews the report and approves the presets and setting parameters.
  - Owner writes `eval/questions.json`; P04B (question verification) starts only on instruction.

---

### Execution Entry

- **Date and time:** 2026-11-12 (evening)
- **Phase identifier:** `p04` approval recorded; `p04b` (Evaluation Question Verification) **blocked before start**
- **Owner approvals recorded:**
  - P04 reviewed and approved. `reports/phase-04-retrieval-and-evaluation-tooling.md` is marked `Owner Approved (2026-11-12)`. The owner committed P04 as `c6907c1`.
  - **Final retrieval settings approved:** chunk presets 160/25, 220/35, 400/60 words; RRF k = 60; reference down-weighting ×0.5 on the fused score and −2.0 rerank penalty; rerank top 12; top-k 4 / 4 / 5.
  - `ARCHITECTURE.md` §6 is now marked `[OWNER-DECIDED 2026-11-12]`, and §16 item 8 is resolved.
- **P04B precondition check:**
  - `eval/questions.json` exists (owner-authored, 20 questions, untracked).
  - `python scripts/validate_questions.py` → `"valid": false`, exit 1. The one error is `coverage: 2 distinct trap types (< 3)`. The trap types present are `garbled_extraction` (q02) and `shared_vocabulary` (q03, q13); neither `bibliography_match` nor `version_leak` is used.
  - `python scripts/validate_questions.py --allow-incomplete` → `"valid": true`, no errors. The schema, consistency and page checks all pass.
  - Coverage: 20 questions (15 answerable, 5 unanswerable); categories direct_factual 8, multi_hop 4, numeric_technical 2, cross_paper 1, unanswerable 5; gold citations Attention 11, Moshi 13, H-Net 13.
- **Status:** **blocked.** Per the owner's instruction ("if `eval/questions.json` is missing or invalid, stop and report"), P04B verification was not started.
  - No question was edited, added or rewritten.
  - No retrieval verification was run.
  - No P04B report was created, since no verification was performed.
- **Observation for the owner (not a validator error):** all 5 unanswerable questions (q16–q20: blockchain consensus, photosynthesis, the TCP handshake, Newton's laws, MySQL vs MongoDB) are far outside the corpus. ASSIGNMENT p.1 only promises "some that the papers do not answer at all", so this is compliant. However, these questions will most likely be stopped by the deterministic early refusal and won't exercise the LLM's cite-or-refuse path or the verifier on near-topic unanswerable questions (ARCH §6 known limitation).
- **Groq calls:** 0. **Files modified:** `reports/phase-04-retrieval-and-evaluation-tooling.md`, `ARCHITECTURE.md`, `ENGINEERING_LOG.md`.
- **Next action:** the owner either:
  - (a) tags or adds a question with a third trap type (`bibliography_match` or `version_leak`) so strict validation passes; or
  - (b) explicitly authorises P04B to proceed with the 2-trap-type coverage (lenient validation), recording that deviation.

---

### Execution Entry

- **Date and time:** 2026-11-12 19:57 IST
- **Phase identifier:** `p04b` (Evaluation Question Verification)
- **Task requested:** Verify the owner's `eval/questions.json` against the pinned PDFs:
  - answers, facts, citations, pages, sections and quotes;
  - unanswerable questions;
  - gold-page reachability under all three retrieval settings;
  - the known traps.

  Report confirmed / discrepancy and list proposed corrections without editing. No corpus change, no new features, no Groq, no P05.
- **Status:** completed (verification only), pending owner review
- **Precondition:** the owner added q21 (WavLM, near-topic unanswerable, `bibliography_match`). `python scripts/validate_questions.py` (strict) → `valid: true`, 0 errors, 0 warnings, exit 0, 21 questions.
- **Integrity:** `eval/questions.json` SHA-256 is `14616cd5271b08c911c418787d6e95c20f7ba043ae6bb345d2b91e4b22d085af` at start and end. **No question edited.**
- **Method:** read-only scratch harness (outside the repo) over `data/processed`, the raw PDFs (PyMuPDF), the P04 grader and the retrievers (offline, local models). Detail in report §2.
- **Results:**
  - **Quotes:** 45/45 found verbatim on the stated page, in both extracted and raw PDF text; 45/45 cited sections correct.
  - **Grader self-check** (expected answer + gold citations): 14/15 `correct`; **q09 → `wrong`**.
  - **Unanswerable:** 6/6 confirmed. q16–q19 have 0 term hits; q20's only hit is "bson" inside the author name "Webson". q21: WavLM appears only as a distillation target, ablation entry, speaker-verification model and reference, never with its pretraining objectives.
  - **Reachability** (≥ 1 gold page in top-k): `small_fast` 13/15, `balanced_rerank` 13/15, `large_wide` 13/15. Gold pages reached: 17 / 18 / 19 of 31. **q09 and q15 reach no gold page in any setting.**
  - **Verdicts:** 17 confirmed (8 with notes); discrepancies q09 (grading-blocking: required facts vs reference answer, facts' source p. 1 not cited), q15 (direct evidence on pp. 22 and 1 not cited), q14 ("most damaging ablation" not in text), q13 (EMA weight is P_t, not p_t).
  - **Traps:**
    - `shared_vocabulary` q03 did not fire; q13's Attention half is never retrieved, and the scope filter suggestion (H-Net only) would exclude it;
    - `garbled_extraction` q02 is present in the rank-1 gold chunk;
    - `bibliography_match` q21 is defused by reference down-weighting (0 reference chunks in the top 12; without it the WavLM reference ranks 2–7);
    - no `version_leak` question.
  - **Rerank scores:** off-topic q16–q20 −8.5 to −10.3; near-topic q21 +2.09.
- **Proposed corrections (not applied; for owner approval):** C-1 … C-9 (q09 facts and citations, q15 citations, q13/q14/q15 wording, notes for q21/q02, optional q01 citation), G-1 (generic or substring-prone fact alternatives), G-2 (full-sentence forbidden claims rarely match). See report §8.3.
- **Commands and outputs:**
  - validator → valid;
  - `pytest tests/test_grader.py tests/test_retrieval.py tests/test_scope.py` → **65 passed**;
  - `pytest -q` → **274 passed**;
  - `ruff check .` → All checks passed!
- **Errors:** the first results print exceeded the output limit (read from the saved file); a 12-hit search limit truncated the WavLM mention list (re-run unlimited). Neither affected the repo.
- **Files created:** `reports/phase-04b-eval-verification.md`. **Files modified:** `ENGINEERING_LOG.md`. Groq calls: 0.
- **Owner approval status:** pending owner review
- **Next action:** Owner decides on C-1 … C-9 / G-1 / G-2 and applies approved changes (or instructs Claude to apply exactly those), then re-runs the validator. P05 only on explicit approval.

---

### Execution Entry

- **Date and time:** 2026-11-12 (after P04B review)
- **Phase identifier:** `p04b`: owner approval + approved corrections applied
- **Owner approval:** P04B approved. `reports/phase-04b-eval-verification.md` is marked `Owner Approved (2026-11-12)`.
- **Correction to the previous P04B entry:** the gold-quote count is **37**, not 45 (a counting error). The finding is unchanged: every quote was verbatim on its page with the correct section. The report's headline table and text were corrected to 37/37. That earlier log entry is left as written, per the append-only rule.
- **Authorised changes applied to `eval/questions.json`** (data only; `src/relay/eval/grader.py` unchanged, confirmed by an empty `git diff`):
  - **C-1 / C-2 (q09):** facts widened; citations p. 1 (Abstract) and p. 7 (§3.1) added.
  - **C-3 (q15):** citations p. 22 (§4) and p. 1 (Abstract) added.
  - **C-4 / C-5 / C-6:** wording fixes in q13, q14 and q15.
  - **C-7 / C-8:** notes for q21 and q02.
  - **C-9 (q01):** citation p. 6 (§4) added.
  - **G-1:** specific fact alternatives in q03, q04, q06, q07, q10, q12, q13, q14, q15.
  - **G-2:** short forbidden phrases appended in q01, q04–q15; the owner's original sentences were kept.
- **Process note:** the first edit command failed to parse (shell quoting), and its `cp` backup never ran. I didn't notice before running the edit script. Integrity was established by inverting the edits programmatically: the reconstruction is **byte-identical** to the original (SHA-256 `14616cd5…22d085af`) and is kept as the backup in scratch. New SHA-256: `ce4dfe418b92a22b4b88ed6667a1f0d4e4470bc89cb1eb9ee7d18d07f05b3903`.
- **Re-verification (offline):**
  - 42/42 gold quotes verbatim (extracted and raw PDF), 42/42 sections correct;
  - grader self-check 15/15 `correct` (q09 fixed);
  - no forbidden phrase matches its expected answer;
  - restatement test: all `wrong`, with fewer facts satisfied by restatement (q07 2→0, q12 3→0, q13 1→0, q03 2→1);
  - reachability: `small_fast` 15/15 questions (20/36 gold pages), `balanced_rerank` 14/15 (19/36), `large_wide` 15/15 (22/36).
- **Commands and outputs (after all edits):**
  - `python scripts/validate_questions.py` → `valid: true`, errors [], warnings [], exit 0 (21 questions);
  - `pytest tests/test_grader.py tests/test_retrieval.py tests/test_scope.py` → 65 passed;
  - `pytest -q` → **274 passed**;
  - `ruff check .` → All checks passed!
- **Files modified:** `eval/questions.json`, `reports/phase-04b-eval-verification.md` (approval, 45→37, addendum), `ENGINEERING_LOG.md`. Groq calls: 0.
- **Owner approval status:** P04B approved; the corrections await owner review.
- **Next action:** wait for the owner's go-ahead before starting P05.

---

### Execution Entry

- **Date and time:** 2026-11-13 06:33 IST
- **Phase identifier:** `p05` (Pipeline, Evidence Gate, Verifier, Safe Refusal)
- **Task requested:** Execute P05 per CLAUDE.md: the single tool behind a validated, timed gateway; an evidence gate with at most one paper-scoped re-query; the prompt; buffered generation with `provider_first_token_ms`; verifier checks (a)–(e); safe refusal with no second LLM call; server spans. Real Groq calls: none.
- **Status:** completed, pending owner review
- **Preconditions read:** CLAUDE.md, ARCHITECTURE.md (§5–§7, §13), IMPLEMENTATION_PLAN.md P05, the P04B report §8.4, and the latest log entries. The repo was clean at HEAD `c51aaca` (the owner committed the P04 approvals, `eval/questions.json` and P04B). A brief plan was presented before any file was written.
- **Files created:** `src/relay/orchestrator/{__init__,tools,evidence_gate,prompt,verifier,pipeline}.py`, `tests/test_pipeline_and_verifier.py` (64 tests), `reports/phase-05-pipeline-and-verifier.md`.
- **Files modified:** `ENGINEERING_LOG.md` only. `git diff --stat` is empty: no P01–P04 source changed, and nothing was installed.
- **Decisions** (all [PROPOSED], for owner review; detail in report §5):
  - **Early refusal** only for: no passages; a non-pinned arXiv version; the scope out-of-corpus signal; or, in the reranked setting with no strong paper cue, a best rerank score below −5.0.
  - **Loop:** one re-query scoped to the missing expected paper(s), using the cue sentence. Up to `top_k // 2` passages are **appended**.
  - **Refusal** must be exactly `INSUFFICIENT_EVIDENCE`.
  - **Defaults:** `max_completion_tokens` 512; tool timeout 10 s.
  - **Failure mapping:** a first-retrieval tool failure → `error` / `internal_error`; a provider error → its own code, with no retry (retries are P06, via the `AnswerGenerator` interface).
- **Commands and observed outputs:**
  - Design measurements on the real indexes (offline): top rerank scores are +0.22…+7.82 for answerable and near-topic questions against −8.48…−10.28 for off-topic ones. The q13 Attention-scoped re-query never returns gold page 4 with the whole question; with the cue sentence it is rank 3 in `small_fast` and rank 5 in `large_wide`.
  - `pytest tests/test_pipeline_and_verifier.py`, run 1 → `1 failed, 63 passed` (a test fixture error).
  - After the merge-policy change → `1 failed, 63 passed` (a fixture with a duplicated passage) → fixed → `64 passed`.
  - **Final:** `pytest -q -rs` → **338 passed**, 0 failed, 0 skipped (4.58 s); `ruff check .` → All checks passed!
- **Smoke run** (offline; a scripted model that always refuses; 21 owner questions × 3 settings = 63 runs; **0 Groq calls**):
  - **Early refusals:** `small_fast` {q18}; `balanced_rerank` {q16–q20}; `large_wide` {q18}. No answerable question was refused early.
  - **q21** passes the gate in every setting. **q13** loops once in every setting.
  - **Answerable questions with ≥ 1 gold page in the prompt:** 15/15, 14/15, 15/15. Gold pages: 20, 19, 22 of 36, the same as the P04B post-correction figures.
  - **`retrieval_ms`** median/max: 29/40, 1121/1502, 26/68.
  - **Prompt chars** median/max: 5,790/8,293; 7,086/9,165; 13,577/20,796.
  - **Verifier false-block check:** 64/64 verbatim gold-quote sentences verified.
- **Errors, root causes, fixes:**
  1. The replace-half merge dropped q13's gold pages (2/3 → 0/3 in two settings). Changed to append; re-measured at 2/3, 2/3, 1/3.
  2. My smoke check tagged only the last sentence of multi-sentence quotes, producing 28 apparent blocks. The check was fixed; the verifier was correct.
  3. Two test fixture errors, fixed.
  4. Loosely typed draft pipeline code and an unknown-setting path, rewritten before the first test run.
  5. Ruff: 3 findings, fixed.
- **Remaining issues:**
  - thresholds and policies awaiting approval;
  - 4 of 5 off-topic questions still reach the model in the non-reranked settings;
  - q13's Attention gold page is still absent;
  - looped `large_wide` prompts approach the 8,000 TPM budget;
  - real-model compliance with the prompt is unmeasured until P07.
- **Deviations from ARCHITECTURE.md:** none to binding decisions. One LLM call per request, no repair loop, verify-before-delivery, and safe refusal as `ok` + `citation_verification_failed` are all preserved.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-05-pipeline-and-verifier.md` and decides the §11.1 items. P06 starts only on explicit approval.

---

### Execution Entry

- **Date and time:** 2026-11-13 07:09 IST
- **Phase identifier:** `p06` (ProviderGuard and Admission Queue)
- **Task requested:** Execute P06 per CLAUDE.md: ProviderGuard (semaphore, RPM/TPM windows, header sync, daily stop, reservation and settlement, adaptive chars-per-token, circuit breaker, degraded top-k), retry policy and budget, admission queue with derived capacity and shedding. Real Groq calls: none.
- **Status:** completed, pending owner review
- **Preconditions:** the repo was clean at HEAD `fc57d4c` (the owner committed P05). The P05 report is still marked `Pending Review`, and its §11.1 decisions are unanswered; P06 builds on those values as proposed. A brief plan was presented before any file was written.
- **Files created:** `src/relay/control/{__init__,retry,provider_guard,admission}.py`, `tests/test_control_plane.py` (34 tests), `reports/phase-06-provider-guard-and-admission.md`.
- **Files modified:**
  - `src/relay/orchestrator/pipeline.py`: additive change. An optional `deadline_ns`; `GenerationResult.attempts` and `.tokens`; an optional `budget` for degraded top-k. All 64 P05 tests pass unmodified.
  - `ENGINEERING_LOG.md`.
  - Nothing was installed.
- **Decisions** (all defaults [PROPOSED]; detail in report §5):
  - **Headers:** `*-requests` update daily state only, and `*-tokens` the per-minute token state. RPM and TPD come only from configuration.
  - **Reservation and settlement:** reserve `ceil(chars/cpt) + max_completion_tokens`. A rejected call is released; an opened stream without usage stays charged as `estimated`.
  - **Spans:** quota waits count as `waiting`.
  - **Error codes:** local refusals reuse existing codes (`daily_budget_exhausted`, `circuit_open`, `predicted_wait_exceeds_deadline`, `deadline_expired`, `payload_too_large`).
  - **Defaults:** retry 3 attempts / base 0.5 s / cap 8 s / deadline share 0.5; budget 0.2 × first attempts + 3 per 60 s; circuit 5 failures / 30 s; guard concurrency 2.
- **Commands and observed outputs:**
  - The P05 tests after the pipeline change → `64 passed`.
  - `pytest tests/test_control_plane.py`, run 1 → **hung**. A test held two permits against the default concurrency of 2. The run was stopped and re-run under `timeout`.
  - Then → `2 failed, 32 passed`.
  - After the fixes → `34 passed`.
  - **Final:** `pytest -q -rs` → **372 passed**, 0 failed, 0 skipped (4.68 s); `ruff check .` → All checks passed!
- **Errors, root causes, fixes:**
  1. **Real bug:** the admission queue kept cancelled waiters in its deque, which inflated depth and could shed requests against ghosts. A cancelled waiter is now removed. Verified by the disconnect test: depth 0, 0 provider requests, 0 reserved tokens.
  2. A test hang, from the test helper's concurrency default. Fixed in the helper.
  3. A wrong test expectation (the estimate after adaptation). Fixed.
  4. Self-review fixes before the first run: the circuit is checked before quota waits, and the half-open trial flag clears at settlement.
  5. Two inline edit scripts failed shell parsing. Nothing ran; they were re-done from script files.
  6. Ruff: one SIM300, fixed.
- **Remaining issues:**
  - real RPM/TPD values are not configured or measured;
  - the local daily counters start at process start;
  - Groq's TPM accounting is unreconciled;
  - the control plane is wired only in tests (P08 does the service wiring);
  - the P05 and P06 defaults await approval.
- **Deviations from ARCHITECTURE.md:** none to binding decisions. For daily exhaustion and circuit open, the outcome follows the §13.4 table (not delivered) and keeps the passages for the §5 "pointers".
- **Real Groq calls:** 0. **Network:** none.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-06-provider-guard-and-admission.md`, decides the P05 §11.1 items and the P06 defaults, and supplies RPM/TPD values with their source. The next phase starts only on explicit approval.

---

### Execution Entry

- **Date and time:** 2026-11-13 (start of P08)
- **Phase identifier:** `p05` and `p06` approvals recorded
- **Owner approvals:** P05 and P06 are approved **with all proposed defaults**. Both reports are marked `Owner Approved (2026-11-13)`. The owner committed P06 as `49afd77`.
- **Defaults now owner-approved:**
  - **P05:**
    - early-refusal rerank threshold −5.0;
    - early refusal for non-pinned arXiv versions;
    - exact-marker refusal rule;
    - re-query appends up to `top_k // 2` passages;
    - `max_completion_tokens` 512;
    - tool timeout 10 s;
    - at most 6 answer sentences.
  - **P06:**
    - retry: 3 attempts, base 0.5 s, cap 8 s, deadline share 0.5;
    - retry budget: 0.2 × first attempts + 3 per 60 s;
    - circuit: 5 failures, 30 s cooldown;
    - guard concurrency: 2;
    - chars-per-token adaptation: weight 0.2, margin 0.9, bounds 1.0–6.0.
- **Sequence note:** the owner instructed P08 next. P07 (real Groq evaluation) has not been run.
- **Next action:** P08 (FastAPI service, exact cache, SingleFlight); real Groq calls: none.

---

### Execution Entry

- **Date and time:** 2026-11-13 07:27 IST
- **Phase identifier:** `p08` — Service: FastAPI, Exact Cache, SingleFlight
- **Task requested and objective:** Mark P05/P06 approved (done in the previous entry), then build `create_app` (pipeline + guard + admission + cache + SingleFlight), `POST /ask` as SSE with a final metadata event, disconnect propagation, the daily-exhausted behaviour, server traces to the run directory, and an exact cache reset per run. Real Groq calls: none.
- **Status:** completed
- **Files created:**
  - `src/relay/control/cache.py`
  - `src/relay/api/__init__.py`
  - `src/relay/api/app.py`
  - `tests/test_cache_and_api.py`
  - `reports/phase-08-fastapi-service-and-cache.md`
- **Files modified:**
  - `src/relay/telemetry/spans.py` (P01): additive read-only `ServerSpanTracker.snapshot()`; no existing line changed.
  - `reports/phase-05-pipeline-and-verifier.md`, `reports/phase-06-provider-guard-and-admission.md`: marked `Owner Approved (2026-11-13)`.
  - `ENGINEERING_LOG.md`: this entry and the approvals entry.
- **Engineering decisions (all [PROPOSED]):**
  - Cache key: SHA-256 of the question after NFKC + whitespace collapse, case preserved; scoped by corpus digest, retrieval setting, prompt version and model.
  - Cache admits `VERIFIED` and `REFUSAL_VERIFIED` only, never degraded; early refusals are not cached; 1024 entries, LRU.
  - `cache_mode=off` disables SingleFlight too (`cache_status=off`).
  - SingleFlight leader is a detached task, cancelled only when no waiter remains; the key is unregistered before cancellation.
  - The response is SSE written in one piece: `answer` or `unavailable`, then `final` with `results.csv` column names. HTTP status 200 / 429 / 504 / 502 / 500; 422 for invalid input; 499 after a disconnect.
  - `unavailable` carries passage pointers (no text) whenever retrieval had run.
  - Followers: 0 tokens, all server time as waiting, blank queue depth and retrieval time; they share the leader's result and deadline.
  - Traces: one `request` record per HTTP request, one `flight` record per leader work. The flight record is the authority for spend; cancelled flights report estimated tokens for calls already sent.
  - Trace write failures are counted and logged, not raised.
- **Commands executed and observed outputs:**
  - `python -c "import relay.api.app"` → `import ok`
  - `python -m ruff check src/relay/api src/relay/control/cache.py src/relay/telemetry/spans.py` → `All checks passed!`
  - `timeout 300 python -m pytest tests/test_cache_and_api.py -q -x` → `37 passed in 1.40s`
  - the same file five more times → `37 passed` each time (1.21–1.27 s)
  - `timeout 60 python scratchpad/p08_uvicorn_smoke.py` (loopback uvicorn, scripted provider) → client timed out at 0.3 s; `provider call cancelled: 1 | in flight: 0 | queue active: 0`; next request `status: 200 | text/event-stream; charset=utf-8`
  - `timeout 900 python -m pytest -q` (after the final edit) → `409 passed in 5.63s`
  - `python -m ruff check .` (after the final edit) → `All checks passed!`
- **Tests performed and results:** 409 passed, 0 failed, 0 skipped (372 earlier + 37 new). All five P08 acceptance criteria are covered and pass.
- **Errors, root causes, fixes, validation:**
  - A bash heredoc failed to parse ("unexpected EOF") and wrote nothing. Root cause: the known quoting pitfall. Fix: files written directly and edits through scratch scripts. Validated by the import check, ruff and tests.
  - Model id for cancelled-flight records was first taken from the cache scope (blank with the cache off). Found by reading before any test ran. Fix: explicit `model_id` on `RelayService`. Validated by `test_sole_client_disconnect_cancels_the_provider_call`.
- **Remaining issues and incomplete work:**
  - No production composition root: real indexes, provider from `Settings`, guard limits and queue capacity are not wired, because RPM/TPD, the final model and the patience quantile are still open.
  - The server writes traces only; P10 must build rows from the `final` event and read flight records for clients that gave up.
  - Cancelled-flight tokens are estimates at 3.0 chars/token.
  - Followers inherit the leader's deadline and failures.
  - Trace appends are synchronous writes on the event loop.
  - `start_run` must be called with no request in flight (documented, not enforced).
  - P07 has not been run.
- **Deviations from ARCHITECTURE.md:** none to binding decisions. `ARCHITECTURE.md` was not edited; its `[PROPOSED]` tags on the approved P05/P06 defaults are unchanged.
- **Real Groq calls:** 0. **Network:** one loopback-only (`127.0.0.1`) uvicorn smoke check; nothing external.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-08-fastapi-service-and-cache.md` and decides the §5 items. Recommended next phase: P09 (stand-in service). The next phase starts only on explicit approval.

---

### Execution Entry

- **Date and time:** 2026-11-13 07:54 IST
- **Phase identifier:** `p09` — Stand-In Service and Calibration
- **Task requested and objective:**
  - OpenAI-compatible stand-in service (SSE + usage, `/models`, admin) with calibrated latency, knee slowdown, 5xx, stragglers and stalls, and RPM/TPM/RPD/TPD 429s with `retry-after` and Groq headers.
  - Extractive cited answers.
  - `scripts/calibrate_standin.py`, writing `evidence/standin_calibration/<run_id>/`, with a quantile comparison, the "least like the real thing" list, and the `uncalibrated_defaults` fallback.
  - Tests through `HttpOpenAIProvider`.
  - Real Groq calls: none.
- **Status:** completed
- **Files created:**
  - `src/relay/standin/__init__.py`, `src/relay/standin/profile.py`, `src/relay/standin/server.py`
  - `scripts/calibrate_standin.py`
  - `tests/test_standin.py` (26 tests)
  - `evidence/standin_calibration/cal-20261113T022150Z-3f687c6/` (superseded; see errors)
  - `evidence/standin_calibration/cal-20261113T022403Z-3f687c6/` (current)
  - `reports/phase-09-standin-service-and-calibration.md`
- **Files modified:** `ENGINEERING_LOG.md` (this entry). No earlier-phase source, test or evidence file was changed.
- **Engineering decisions (all [PROPOSED]):**
  - Continuous-refill token buckets. The headers support this: reset-requests matched 15/15, reset-tokens 11/15 (qwen calls 2–5 do not fit).
  - `*-requests` = RPD bucket, `*-tokens` = TPM bucket (consistent, not formally verified).
  - TPM/TPD reservation of prompt + `max_completion_tokens`, refunded to usage at stream end.
  - Daily limits are checked before minute limits.
  - 429s and injected 5xx consume no quota.
  - RPM/TPD are not enforced unless set through `/admin/config` (recorded as `scenario`).
  - Limits are global, not per model.
  - Answers are at most 2 extractive sentences; output tokens counted at an assumed 4 characters per token.
  - The calibration replay runs with limits off.
  - Stream pacing uses cumulative deadlines on `time.perf_counter`.
  - Only `complete` probe runs are used.
  - Sigma floor 0.05.
- **Commands executed and observed outputs:**
  - Read-only fit on the real baseline: 15 calls; `tpm=8000`, `rpd=1000`; header checks requests 15/15, tokens 11/15.
  - `timeout 300 python -m pytest tests/test_standin.py -q -x` → 1 failed, 18 passed (wrong expectation 37 vs 38); after the fix, 25 passed; 5 repeats, 25 passed each.
  - `timeout 900 python -m pytest -q` → `1 failed, 433 passed` (`test_stragglers_are_much_slower`); 3 reruns, 434 passed each; after the assertion fix, 6 runs, 434 passed each.
  - `timeout 300 python scripts/calibrate_standin.py --evidence-dir evidence --seed 0 --replay-repeats 3` → `cal-20261113T022150Z-3f687c6`, exit 0. gpt-oss content started only 8–21 ms after the headers, where 44–87 ms was expected.
  - `time.get_clock_info("monotonic")` → `GetTickCount64()`, resolution 0.015625. 43 × `asyncio.sleep(2.03 ms)` took 656.1 ms in isolation.
  - After the `_Pacer` fix: `tests/test_standin.py` 3 runs, 26 passed each.
  - Scratch check: paced 107.1 / 94.2 / 92.4 ms vs naive 19.0 / 14.5 / 9.8 ms (expected 100).
  - Calibration run 2 → `cal-20261113T022403Z-3f687c6`, exit 0, 45 replayed calls, 0 errors. p50 total, measured / stand-in: gpt-oss-20b 521.3 / 543.4 ms, gpt-oss-120b 638.3 / 635.0 ms, qwen3.8-27b 283.9 / 296.5 ms. Largest differences are completion tokens (ratios 0.5, 0.82, 0.83).
  - Secret scan of `evidence/standin_calibration/` (`gsk_`, `authorization`, `Bearer`) → no matches.
  - Final: `timeout 900 python -m pytest -q` → `435 passed in 11.54s`; `python -m ruff check .` → `All checks passed!`
- **Tests performed and results:** 435 passed, 0 failed, 0 skipped (409 earlier + 26 new). All P09 acceptance criteria are covered and pass.
- **Errors, root causes, fixes, validation:**
  1. Stream pacing drift: the 15.625 ms event-loop clock on Windows made per-token sleeps inaccurate and the error accumulated. Fixed with cumulative-deadline pacing. Validated by the new regression test, the scratch discrimination check, and calibration run 2.
  2. Flaky ratio assertion in the straggler test: unscaled connection and scheduling jitter on the normal call. Fixed with floor + gap assertions (6 consecutive full passes).
  3. Wrong test expectation (intercept 37.5 → 38). Corrected.
  4. Lint findings (SIM905, I001, E501, SIM117). Fixed.
- **Remaining issues and incomplete work:**
  - The calibration rests on 15 sequential tiny-output calls.
  - Groq's extra completion tokens are not modelled.
  - Knee, slowdown, fault rates, RPM and TPD are unmeasured scenario inputs.
  - The 429 format was never observed.
  - The superseded run `cal-20261113T022150Z-3f687c6` is kept unmodified; the owner decides.
  - The stand-in is not yet used by the Relay end to end (P10).
- **Deviations from ARCHITECTURE.md:** none to binding decisions. `ARCHITECTURE.md` was not edited.
- **Real Groq calls:** 0. **Network:** loopback `127.0.0.1` only.
- **Owner approval status:** pending owner review
- **Next action:** Owner reviews `reports/phase-09-standin-service-and-calibration.md`, decides the §5 items and the scenario values, and whether to keep the superseded run. Recommended next phase: P10. The next phase starts only on explicit approval.

---

### Execution Entry

- **Date and time:** 2026-11-13 12:20 IST
- **Phase identifier:** `p07` — Model Comparison and Retrieval-Quality Evaluation (steps 1–4)
- **Task requested and objective:**
  - Evaluation runner and scripts (cache off, sequential, ProviderGuard pacing, `--source`, `--max-real-calls`, daily fraction 0.75).
  - Offline tests.
  - Live model comparison (6 questions × 3 models, `balanced_rerank`).
  - Then wait for the owner's model selection before Table 3.
- **Status:** in progress. Steps 1–4 completed; steps 5–6 await the owner's model selection.
- **Files created:**
  - `src/relay/experiments/{__init__,eval_runner,eval_cli}.py`
  - `scripts/run_model_comparison.py`, `scripts/run_retrieval_eval.py`
  - `tests/test_eval_runner.py` (16 tests)
  - `evidence/experiments/exp-20261113T063331Z-e17e9c1/`
  - `reports/phase-07-model-and-retrieval-eval.md`
- **Files modified:** `ENGINEERING_LOG.md` (this entry). No earlier module, evidence or `eval/questions.json` was changed.
- **Engineering decisions (all [PROPOSED]):**
  - Comparison subset by a stated rule: q03, q07, q10, q12, q13, q21.
  - One guard per model: TPM 8000 / RPD 1000 (headers), RPM pacing 10 (assumed), TPD unknown, concurrency 1.
  - No prompt budget, so no degraded answers.
  - Interleaved order across models.
  - TTFT reported without the guard's quota wait.
  - Rows keep `answer_grade` blank until the owner reviews.
- **Commands executed and observed outputs:**
  - Settings check (non-secret values only): key configured True, `MAX_REAL_CALLS` 40, fraction 0.75; `.env` is gitignored.
  - `pytest tests/test_eval_runner.py`: 1 failed / 2 passed → 2 failed / 14 passed → 1 failed / 15 passed → 16 passed (fixes in the report §9–§10).
  - Offline stand-in smoke (scratch/): 18 rows, 0 real calls, exit 0. It showed TTFT max of 45–52 s, which was quota wait; fixed with `provider_ttft_excl_wait_ms`.
  - Dry run: `run_model_comparison.py --source real --max-real-calls 24` → only "would refuse: dry run"; no run created.
  - **LIVE**, executed once: `run_model_comparison.py --source real --live --max-real-calls 24` → `exp-20261113T063331Z-e17e9c1`, exit 0, real calls 18, rows 18, all `ok`, 0 retries, no stops.
  - Secret scan of the run dir: 0 `gsk_`, no bearer/authorization, no `org_` IDs.
  - Final: `pytest -q` → `451 passed in 17.77s`; `ruff check .` → `All checks passed!`
- **Results (proposed grades, pending owner review):**
  - gpt-oss-20b and gpt-oss-120b: 3 of 6 calls each truncated at `max_completion_tokens=512`, all spent on reasoning, empty content, so safe refusals (q07, q12, q13). Citation pass 0.4; proposed correct 0/5; q21 refused correctly. TTFT excl. wait p50 2,350.6 / 2,567.4 ms; tokens out mean 397.0 / 395.5.
  - qwen3.8-27b: 0 truncated; 6/6 verified; citation pass 1.0; proposed correct 1/5; q21 answered with a cited "the passages do not state…" sentence (verifier gap), proposed `wrong`. TTFT excl. wait p50 2,217.4 ms; tokens out mean 97.0.
  - Retrieval took 607–2,790 ms per request, most of the TTFT.
  - Many "wrong" proposals come from the rubric's literal fact matching.
- **Errors, root causes, fixes:** see report §9–§10:
  - test `Path` bug;
  - dry-run exit code (real CLI bug);
  - over-strict round-trip and first-byte assertions;
  - TTFT including quota wait;
  - a failed edit-script assertion.
- **Remaining issues:**
  - Model choice is confounded by truncation: choose qwen, or re-run gpt-oss at 1024 tokens / `reasoning_effort` low (about 12 calls).
  - Verifier gap on refusal-like cited sentences (P05; owner decision).
  - Grades do not yet flow into rows (aggregation).
  - Table 3 cap: about 63+ calls against a proposed Day-2 envelope of 80 with 18 used; `MAX_REAL_CALLS` is 40.
  - TPD unknown.
- **Deviations from ARCHITECTURE.md:** none to binding decisions. `eval_cli.py` is an extra file not named in the plan.
- **Real Groq calls:** 18 (Day 2 total so far: 18). **Network:** Groq API for the live run only; otherwise loopback.
- **Owner approval status:** pending owner review
- **Next action:** Owner decides the model, the Table 3 cap and token settings, the verifier gap, and reviews the 18 proposed grades. Step 5 starts only on explicit approval.
