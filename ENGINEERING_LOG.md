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
