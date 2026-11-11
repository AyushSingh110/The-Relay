# CLAUDE.md — Project Engineering Governance & Operating Instructions

> **Status:** PERMANENT OPERATING CONTRACT FOR CLAUDE CODE  
> **Project:** The Relay  
> **Rule Scope:** Applies to EVERY prompt, phase, edit, test, and report executed in this repository.

---

## 1. Project Identity and Objective

**The Relay** is a production-oriented, rate-limit-aware RAG Q&A system built in Python 3.11+ that answers engineering questions exclusively from three exact-version arXiv papers (`1706.03762v7`, `2410.00037v1`, `2507.07955v2`), enforces strict citation integrity and grounded refusal, manages real Groq free-tier rate limits (`RPM`, `TPM`, `RPD`, `TPD`), runs load experiments against a calibrated HTTP/SSE stand-in model service, and emits strictly recomputable raw telemetry in `results.csv`.

Deliverables must satisfy every requirement in `ASSIGNMENT.md` cleanly, safely, reproducibly, and within a tight three-day execution schedule without over-engineering.

---

## 2. Source-of-Truth Hierarchy

Resolve any ambiguity strictly in this descending priority order:

1. **Official Assignment Brief:** `ASSIGNMENT.md` (primary and non-negotiable truth).
2. **Owner-Approved System Architecture:** `ARCHITECTURE.md` (once marked approved by the project owner). Until its status is `APPROVED`, items tagged **[OWNER-DECIDED]** are binding owner decisions, while items tagged **[PROPOSED]** or **[OPEN]** must not be treated as final.
3. **Approved Phase Roadmap & Task Specs:** `IMPLEMENTATION_PLAN.md`.
4. **Project Execution History:** `ENGINEERING_LOG.md`.
5. **The Specific Phase Prompt:** Provided explicitly by the project owner for the current phase.

**You must NEVER silently override a higher-priority source.** If a phase prompt conflicts with `ASSIGNMENT.md` or `ARCHITECTURE.md`, stop, report the conflict clearly, and wait for the owner's decision.

---

## 3. Mandatory Operating Procedure (Before Every Implementation)

Before editing or creating any source or test file in any phase, you MUST execute these steps in order:

1. Read this `CLAUDE.md` completely.
2. Read the relevant sections of `ASSIGNMENT.md`.
3. Read `ARCHITECTURE.md`.
4. Read the target phase section in `IMPLEMENTATION_PLAN.md`.
5. Read the latest entries in `ENGINEERING_LOG.md`.
6. Inspect the actual current repository files (`git status`, directory contents, existing modules, and tests).
7. Understand existing interfaces, contracts, and working behavior before changing anything.
8. Present a brief, concrete implementation plan (files to touch, responsibilities, invariants to preserve) before making significant changes.

---

## 4. Coding Standards

All code written in this repository MUST follow these standards:

- **Modular Single-Responsibility Design:** Clear boundaries between ingestion, retrieval, provider HTTP transport, admission/rate control, caching/coalescing, orchestration/verification, stand-in simulation, telemetry, and experiment runners.
- **Strict Type Safety:** Explicit Python 3.11+ type annotations (`from __future__ import annotations` optional, standard `| None` unions, `Protocol` interfaces, `Pydantic v2` frozen models at module boundaries).
- **Input & Contract Validation:** Validate configuration, tool inputs, HTTP payloads, and `results.csv` schema rows strictly using Pydantic v2; fail loudly on malformed data rather than coercing silently.
- **Meaningful Exception Handling:** Use a structured domain error hierarchy; never swallow exceptions silently; never use bare `except:`.
- **Async Safety:** Use `asyncio` primitives correctly; offload CPU-heavy local embeddings/reranking via `asyncio.to_thread`; avoid holding locks across `await` boundaries; propagate `asyncio.CancelledError` cleanly on client disconnects.
- **Secure Secret Handling:** Load secrets only from environment/`.env`. NEVER print, log, store in `results.csv` / `ENGINEERING_LOG.md` / error tracebacks, or commit `GROQ_API_KEY` or credentials. Use exception sanitization (`type(exc).__name__` + HTTP `status_code` only).
- **Minimal Dependencies:** Do NOT introduce LangChain, LangGraph, LlamaIndex, Redis, Kafka, Celery, Docker/Kubernetes, FAISS, ChromaDB, Qdrant, or PyTorch unless explicitly approved in `ARCHITECTURE.md`.
- **Deterministic Offline Testing:** All unit/integration tests in `tests/` must run 100% offline with zero network calls and zero Groq API keys via `ScriptedProvider` or `httpx.MockTransport` / local ASGI transport.
- **Clean, Readable Python:** Prefer straightforward helper functions, shallow nesting, early returns, and zero speculative abstractions; verify lint cleanliness with `ruff check .`.

---

## 5. Absolute Engineering Restrictions

Claude Code MUST **NEVER**:

1. **Commit or push anything to Git.** NEVER run `git commit`, `git commit --amend`, `git push`, or rewrite Git history/staging for commits. The project owner retains 100% control over all Git commits.
2. **Fabricate test results, benchmarks, probe data, or experimental metrics.**
3. **Invent Groq limits, response headers, pricing, or latency numbers** as observed measurements when they were not actually measured against a live run.
4. **Pretend an implementation is complete** when features, edge cases, or tests remain missing or mocked.
5. **Claim tests or linters passed** without actually executing them after the final edit and observing every test pass.
6. **Silently alter approved architectural decisions** (e.g., buffered verify-before-delivery with safe refusal on failure, no hidden transport/SDK retries, `results.csv` mandatory schema, single-worker design, exact-cache-only scope).
7. **Rewrite or refactor unrelated working modules** from earlier completed phases unless fixing a verified bug (and document that fix explicitly).
8. **Ignore failing tests, warnings, or invariant violations.**
9. **Commit or hardcode API keys, `.env` secrets, or credentials** anywhere in code, logs, fixtures, or reports.
10. **Spend real Groq API quota unexpectedly** during automated tests or without explicit phase/user instruction and `--max-real-calls` guards.
11. **Silently omit mandatory requirements** from `ASSIGNMENT.md`.
12. **Deliberately trigger a real Groq rate limit** without a separate, explicit owner approval for that specific probe, or **exhaust the real daily quota** for testing under any circumstances.
13. **Write the canonical `results.csv` by any path other than the experiment harness aggregation step** (`scripts/build_results.py`), or let aggregation accept duplicate `request_id`s.
14. **Gitignore, delete, or overwrite submission evidence** under `evidence/` (raw Groq probes, stand-in calibration, experiment runs) or `results.csv`. New runs go into new run directories.
15. **Author the evaluation questions on the owner's behalf, or silently edit them.** Claude prepares structure and tooling and verifies owner questions against the PDFs, reporting proposed corrections for the owner to accept.
16. **Treat a Groq header's meaning, a tokenizer's token counts, or a model choice as verified** without real evidence; provider-reported `usage` is the authoritative token count.

When blocked by missing credentials, missing files, environment errors, or specification ambiguity, STOP, document the exact blocker and root cause honestly, and report it to the owner.

---

## 6. Mandatory Documentation Procedure After Every Implementation

**THIS PROCEDURE IS NON-NEGOTIABLE.** No implementation phase is complete until all three parts below are finished:

### A. Update `ENGINEERING_LOG.md`
Append a new chronological `### Execution Entry` (never overwrite or sanitize past entries) recording:
- Date and time
- Phase identifier
- Task requested & objective
- Status (`planned` / `in progress` / `completed` / `blocked`)
- Files created and files modified
- Engineering decisions made
- Exact commands executed and actual observed outputs
- Tests performed and exact test results (pass/fail/skip counts)
- Errors encountered, root cause of each, fix applied, and post-fix validation
- Remaining issues, incomplete work, and any deviations from `ARCHITECTURE.md`
- Owner approval status (`pending owner review`) & recommended next action

### B. Create a Separate Implementation Report
Create a new file:
`reports/phase-XX-short-description.md` (following `reports/IMPLEMENTATION_REPORT_TEMPLATE.md` strictly) containing all 14 mandatory sections:
1. Objective
2. Implementation Summary
3. Files Created
4. Files Modified
5. Technical Decisions
6. Commands Actually Executed
7. Test Results
8. Actual Output and Generated Artifacts
9. Errors and Root-Cause Analysis
10. Fixes Applied
11. Outstanding Issues
12. Acceptance-Criteria Status
13. Remaining Risks
14. Recommended Next Phase

### C. Final Response to the Owner — Then STOP
In your final response to the user, provide ONLY verified, directly observed facts covering:
- What was completed vs. what was not completed
- Tests actually executed and exact observed results
- Important outputs and metric/artifact summaries
- Errors encountered and fixes applied
- Exact path to `reports/phase-XX-short-description.md`
- Exact path to updated `ENGINEERING_LOG.md`
- Any decisions or blockers requiring owner approval

**STOP IMMEDIATELY after reporting.** NEVER automatically begin the next phase. Wait until the owner reviews the phase report and explicitly instructs you to proceed.

---

## 7. Architecture Protection Protocol

If during implementation you discover a flaw in `ARCHITECTURE.md` or believe a design decision must change:
1. **Do not silently code around it.**
2. Explain the concrete technical problem observed.
3. Present the proposed alternative and compare trade-offs.
4. Explain consequences for earlier phases, later phases, telemetry, and `ASSIGNMENT.md` compliance.
5. Wait for explicit owner approval before updating `ARCHITECTURE.md` or implementing the change.

---

## 8. Final Engineering Philosophy

**Correctness, grounded citation integrity, reproducibility, security, and brutally honest reporting take priority over implementation speed or apparent completion.** Never trade truth for convenience.
