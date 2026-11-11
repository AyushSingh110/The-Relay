# Phase 00 Report — Architecture Review & Finalization

> **Report Type:** Architecture Review & Governance Setup Report (NOT an implementation report)  
> **Phase ID:** `phase-00`  
> **Date:** `2026-11-11`  
> **Code Implementation Status:** Zero application source code implemented (as instructed).  
> **Owner Approval Status:** Pending Owner Review

---

## 1. Materials Reviewed
1. **Official Assignment Brief PDF** (*The Relay – assignment brief*, Pages 1–4) — treated as the sole primary source of truth.
2. **Proposed High-Level System Architecture & Request Sequence Diagrams** (from earlier architectural analysis).
3. **Previous Detailed Architecture & Claude Code Prompt Proposal** (Parts A–J).
4. **Live Official Groq Documentation** (`console.groq.com/docs/rate-limits`, `console.groq.com/docs/models`, `console.groq.com/docs/deprecations`, `groq.com/pricing`) checked via web search for Nov 2026 accuracy.
5. **Reference Ideation Repo (`HABIT`)** — read-only inspection of `ToolGateway`, `GroqChatModel` (`max_retries=0`), `SemanticDispatcher`, and pure-code `verify` gate patterns.

---

## 2. Strong Decisions Retained
- **Python 3.11+ + FastAPI/Uvicorn + `httpx.AsyncClient` + `pymupdf` + `fastembed` ONNX (`BAAI/bge-small-en-v1.5` & `Xenova/ms-marco-MiniLM-L-6-v2`) + Exact NumPy/BM25 RRF:** Optimal 3-day balance of delivery speed, zero external DB/daemon bloat, and sub-millisecond vector search on ~400–700 paper chunks.
- **Hybrid Deterministic + Single-Call LLM Orchestration:** Keeps query scoping, `ToolGateway`, bounded re-query loop (`max 1`), and citation checking deterministic, reserving Groq calls strictly for grounded synthesis (`1` nominal call per cache miss).
- **Standalone HTTP/SSE Stand-In Server (`127.0.0.1:8001`):** Exercises identical `HttpOpenAIProvider` networking, SSE framing, `Retry-After` headers, and cancellation behavior.
- **HABIT-Inspired Engineering Guardrails:** `max_retries=0` on HTTP/SDK clients so our middleware owns retries; Pydantic-validated `ToolGateway`; pure-code citation gate blocking unverified outputs from cache admission; exception secret sanitization.
- **Strict Experiment Isolation:** Cache explicitly disabled (`cache_mode="off"`) during all Table 3 retrieval experiments.

---

## 3. Issues Identified & Corrections Made

| # | Defect in Earlier Proposal | Engineering Correction Applied in `ARCHITECTURE.md` |
| :--- | :--- | :--- |
| **1** | **Streaming Before Verification:** Earlier diagrams streamed unverified LLM tokens to the user before `CitationVerifier` ran, risking sending wrong/hallucinated answers to stay fast. Also risked conflating `[Pi]` syntax checks with semantic entailment. | Switched to **Verify-Before-Delivery**: upstream still streams (`stream=True`) into a local buffer so `provider_first_token_ms` is measured accurately; `CitationVerifier` runs deterministically BEFORE delivering verified output to client (`first_token_ms` = user-visible TTFT). Clearly distinguished structural citation integrity from semantic entailment. |
| **2** | **Incorrect Groq Rate-Limit Header Mapping:** Earlier draft implied `x-ratelimit-*-requests` tracked RPM. Official Groq docs confirm `x-ratelimit-*-requests` tracks **RPD (Requests Per Day)** and `x-ratelimit-*-tokens` tracks **TPM (Tokens Per Minute)**, while RPM and TPD are omitted from standard headers. | Corrected `ProviderGuard` and Stand-In header contract (`x-ratelimit-*-requests` -> RPD; `x-ratelimit-*-tokens` -> TPM; RPM/TPD tracked locally and verified via Console + `429` JSON). Added explicit duration-string parsing and `reasoning_format` awareness. |
| **3** | **Unsupported `answer_grade="na"` Enum Value:** Earlier schema proposed `"na"` for un-answered requests, violating `ASSIGNMENT.md` p.4 allowed enum values. | Removed `"na"`. Un-answered/ungraded rows leave `answer_grade=""` (blank CSV value, parallel to blank `first_token_ms`). |
| **4** | **`.gitignore` vs. Root `results.csv` Risk:** Earlier plan mixed `results/` ignore patterns with `results.csv` pathing. | Anchored final `results.csv` strictly at project root (`results.csv`), NEVER gitignored; moved intermediate probes/traces/audits to `runs/`. |
| **5** | **Overlapping Admission & Rate Control:** Diagrams split daily budget and minute limits across overlapping modules. | Strictly separated **Application `IngressGate` + `BoundedRequestQueue`** (coalescing, cache, FIFO queue, deadlines, disconnects, predictive shedding) from **Single-Owner `ProviderGuard`** (RPM/TPM/RPD/TPD, semaphore, `RetryBudget`, `CircuitBreaker`). |
| **6** | **Hardcoded Semantic Cache Constants:** Earlier draft named `0.93` cosine / `0.03` margin upfront as fixed operational settings. | Designated **L1 Exact Cache + SingleFlight** as the reliable baseline (`CACHE_MODE=exact`) and made **L2 Guarded Semantic Cache** an independently toggleable module whose thresholds are empirically calibrated in Phase 9 (`scripts/calibrate_cache.py`). |
| **7** | **Missing Deployment & Stand-In Quality Boundaries:** Single-process assumptions and performance-vs-reasoning boundaries were implicit. | Declared **Single-Process Uvicorn (`workers=1`)** and documented distributed trade-offs; separated Stand-In performance simulation from genuine Groq answer-quality evaluation. |
| **8** | **Missing Non-Commit Rule in Governance:** User explicitly requested Claude Code never run git commits. | Added an absolute ban on `git commit` and `git push` in `CLAUDE.md`. |

---

## 4. Final Recommended Architecture Summary
The finalized design (`ARCHITECTURE.md`) establishes a single-process Python 3.11+ FastAPI service organized around two cleanly separated control layers (`IngressGate` and `ProviderGuard`), a 6-step deterministic-plus-1-LLM RAG pipeline routed through `ToolGateway`, a **Verify-Before-Delivery** gate enforcing structural citation integrity, an L1 Exact + SingleFlight baseline with optional empirically calibrated L2 Semantic Cache, and a calibrated HTTP/SSE Stand-In server sharing the exact `HttpOpenAIProvider` transport used for Groq.

---

## 5. Technical Risks & Mitigations
1. **Groq Model Reasoning-Token Overhead / Deprecation:** Active models (`openai/gpt-oss-20b`, `qwen/qwen3.8-27b`) can spend TPM on reasoning tokens before visible text; mitigated by runtime `GET /models` discovery + configurable `reasoning_format`/`reasoning_effort`.
2. **Shared Daily Free-Tier Exhaustion:** Mitigated by keeping unit/integration/load tests 100% offline/Stand-In and enforcing `--max-real-calls` guards (~150 total real calls planned across 3 days).
3. **2-Column PDF Extraction Garbling (`1706.03762v7`):** Mitigated by `pymupdf` geometric column-block sorting and mandatory Phase 02 page inspection.
4. **Verify-Before-Delivery User TTFT Increase:** Buffering until completion adds upstream generation time (~400–1200 ms) to user-visible `first_token_ms` on uncached calls; explicitly justified by the zero-wrong-answer requirement and contrasted against `provider_first_token_ms`.
5. **2-Page Report Density:** Strict page cap mitigated by placing raw data/code/unedited conversation export in companion artifacts per `ASSIGNMENT.md` p.4 and automating Table 1–3 calculation.

---

## 6. Unresolved Decisions & Approval Checklist (Requires Owner Sign-Off)

1. **Verify-Before-Delivery Gate:** Approve buffering upstream LLM stream locally until `CitationVerifier` passes before sending output to client (trading user-visible TTFT on uncached misses for strict unverified-answer prevention)?
2. **`results.csv` Appended Columns (13–24) & Blank `answer_grade`:** Approve preserving mandatory columns 1–12 identically, appending telemetry columns 13–24 (`provider_first_token_ms`, `queue_depth_at_entry`, `waiting_ms`, `working_ms`, `retrying_ms`, `cost_usd`, `cache_status`, `provider_called`, etc.) so all tables recompute cleanly from `results.csv`, and using blank `""` for `answer_grade` when no answer is generated?
3. **Single-Process Assumption (`workers=1`) & Standalone HTTP Stand-In (`127.0.0.1:8001`):** Approve single-process design and separate HTTP/SSE Stand-In process?
4. **Cache Scope:** Approve building **L1 Exact + SingleFlight** (`CACHE_MODE=exact`) as baseline AND **L2 Guarded Semantic Cache** (`CACHE_MODE=semantic`, calibrated in Phase 9), OR restrict scope strictly to Exact + SingleFlight?
5. **Groq Model Candidate & ~150-Call Budget Allocation:** Approve Phase 05 `GET /models` live check starting with `openai/gpt-oss-20b` (or fastest active non-reasoning free-tier model) and capping real calls across probes + paced Table 3 eval + steady run at ~150 calls?
6. **FIFO Bounded Queue (`maxsize=16`):** Approve simple FIFO queue with deadline/disconnect checks and predictive shedding over multi-priority queuing?

*End of Phase 00 Architecture Review Report. Zero source files implemented. Ready for Owner Approval.*
