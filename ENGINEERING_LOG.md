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
