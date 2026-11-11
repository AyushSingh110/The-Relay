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
