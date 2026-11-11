# IMPLEMENTATION_PLAN.md — Phased Execution Roadmap & Claude Code Prompt Pack

> **Governance Contract:** Every phase below MUST follow `CLAUDE.md` strictly:
> 1. Inspect existing repository and read governance docs before editing.
> 2. Implement ONLY the current phase's scope; never silently rewrite working prior phases.
> 3. NEVER run `git commit` or `git push`.
> 4. Run tests/linters after final edits, update `ENGINEERING_LOG.md`, write `reports/phase-XX-*.md`, report observed facts, and **STOP** to await owner approval.

---

## Roadmap Overview (3-Day Critical Path)

| Phase | Target Window | Objective | Depends On | Required Report |
| :--- | :--- | :--- | :--- | :--- |
| **Phase 01** | Day 1 AM | Repo skeleton, config, schemas, `results.csv` & span tracker | Owner Architecture Approval | `reports/phase-01-foundation-telemetry.md` |
| **Phase 02** | Day 1 Mid | Exact arXiv PDFs (`1706v7`, `2410v1`, `2507v2`) & layout-aware chunking | Phase 01 | `reports/phase-02-pdf-ingestion.md` |
| **Phase 03** | Day 1 PM | Local hybrid index (`BGE` + `BM25` + `ONNX Rerank`) & verified 18-Q eval set | Phase 02 | `reports/phase-03-retrieval-evalset.md` |
| **Phase 04** | Day 1 Eve | `ModelProvider`, `ToolGateway`, `EvidenceGate`, `CitationVerifier` & `RelayPipeline` | Phase 01, 03 | `reports/phase-04-ai-pipeline.md` |
| **Phase 05** | Day 2 AM | `HttpOpenAIProvider` (`max_retries=0`, true Groq RPD/TPM headers) & budgeted probes | Phase 04 | `reports/phase-05-groq-integration.md` |
| **Phase 06** | Day 2 Mid | `IngressGate` bounded FIFO queue + single-owner `ProviderGuard` | Phase 04, 05 | `reports/phase-06-control-plane.md` |
| **Phase 07** | Day 2 PM | SingleFlight + L1 Exact Cache (+ optional L2 Semantic skeleton) + FastAPI API | Phase 06 | `reports/phase-07-cache-and-api.md` |
| **Phase 08** | Day 2 Eve | Calibrated standalone HTTP/SSE Stand-In service (`127.0.0.1:8001`) | Phase 05 | `reports/phase-08-standin-service.md` |
| **Phase 09** | Day 3 AM–Mid | Reproducible load-test runner, retrieval eval runner & L2 threshold calibration | Phase 07, 08 | `reports/phase-09-experiments.md` |
| **Phase 10** | Day 3 PM | Deterministic Table 1/2a/2b/3 recomputation, slow-req trace & final verification | Phase 09 | `reports/phase-10-verification-tables.md` |

---

## Phased Specifications & Copy-Ready Claude Code Prompts

---

### Phase 01 — Foundation, Typed Configuration, Frozen Schemas & Telemetry Contract

- **Objective:** Establish `pyproject.toml`, `.gitignore`, `.env.example`, `config.py`, `schemas.py`, `tracker.py`, and `csv_writer.py`.
- **Files:** `pyproject.toml`, `.gitignore`, `.env.example`, `README.md`, `src/relay/__init__.py`, `src/relay/config.py`, `src/relay/schemas.py`, `src/relay/telemetry/__init__.py`, `src/relay/telemetry/tracker.py`, `src/relay/telemetry/csv_writer.py`, `tests/test_schemas_and_telemetry.py`.
- **Acceptance Criteria:**
  - `results.csv` lives in repository root (NOT gitignored). `.gitignore` ignores `.env`, `*.db`, `data/papers/*.pdf`, `data/indexes/`, `runs/`, `__pycache__/`.
  - Columns 1–12 of `ResultRow` strictly match `ASSIGNMENT.md`; `answer_grade` accepts ONLY `"correct"`, `"wrong"`, `"refused_correctly"`, `"refused_wrongly"`, or `""` (never `"na"`).
  - Appended telemetry columns 13–24 include `provider_first_token_ms`, `queue_depth_at_entry`, `waiting_ms`, `working_ms`, `retrying_ms`, `retrieval_ms`, `cache_status`, `provider_called`, `model_id`, `cost_usd`, `verification_verdict`, `error_code`.
  - Missing `first_token_ms` or `provider_first_token_ms` serializes as `""` (never `0` or `-1`).
  - `waiting_ms + working_ms + retrying_ms == total_ms` within `1.0 ms`.
  - `pytest` and `ruff check .` pass cleanly.
- **Required Report:** `reports/phase-01-foundation-telemetry.md`

#### Copy-Ready Prompt — Phase 01
```text
Execute Phase 01 of The Relay strictly following CLAUDE.md.

First, read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 01), and ENGINEERING_LOG.md, then inspect the repository directory (`Get-ChildItem -Force`). Present a brief implementation plan before writing files.

OBJECTIVE: Create the Python 3.11+ package foundation, environment configuration, frozen Pydantic v2 contracts, monotonic telemetry span tracker, and root `results.csv` / `runs/traces.jsonl` writers.

FILES TO CREATE:
- `pyproject.toml` (hatchling or setuptools src layout `src/relay`; deps: fastapi, uvicorn, httpx, pydantic>=2, pydantic-settings, pymupdf, fastembed, rank-bm25, numpy, tiktoken; dev deps: pytest, pytest-asyncio, ruff)
- `.gitignore` (ignore `.env`, `*.db`, `data/papers/*.pdf`, `data/indexes/`, `runs/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`. CRITICAL: do NOT ignore `results.csv`!)
- `.env.example` (`GROQ_API_KEY=`, `GROQ_MODEL=openai/gpt-oss-20b`, `PROVIDER_MODE=standin`, `STANDIN_URL=http://127.0.0.1:8001/openai/v1`, `CACHE_MODE=exact`)
- `README.md`
- `src/relay/__init__.py`
- `src/relay/config.py`
- `src/relay/schemas.py`
- `src/relay/telemetry/__init__.py`
- `src/relay/telemetry/tracker.py`
- `src/relay/telemetry/csv_writer.py`
- `tests/test_schemas_and_telemetry.py`

REQUIREMENTS:
1. `config.py`: Use `pydantic-settings`. Do NOT fail tests when `GROQ_API_KEY` is absent unless `provider_mode == "groq"`. Define the 3 retrieval presets (`small_fast`: 250/40/k=4/rerank=False; `balanced_rerank`: 450/80/fetch=12/k=4/rerank=True; `large_wide`: 800/120/k=6/rerank=False), bounded queue/rate/retry defaults, and a pricing table mapping documented Groq models to counterfactual per-1M-token USD rates.
2. `schemas.py`: Frozen Pydantic v2 models (`PassageChunk`, `Citation`, `QueryPlan`, `VerificationResult`, `AskRequest`, `AskResponse`, `ResultRow`). `ResultRow` MUST preserve the 12 mandatory columns FIRST in exact order (`request_id`, `scenario`, `source`, `start_time`, `first_token_ms`, `total_ms`, `status`, `retries`, `tokens_in`, `tokens_out`, `retrieval_setting`, `answer_grade`), followed by cols 13–24 (`provider_first_token_ms`, `queue_depth_at_entry`, `waiting_ms`, `working_ms`, `retrying_ms`, `retrieval_ms`, `cache_status`, `provider_called`, `model_id`, `cost_usd`, `verification_verdict`, `error_code`). Enforce `answer_grade in ("correct", "wrong", "refused_correctly", "refused_wrongly", "")` ONLY. Include `sanitize_error(exc)` returning class name + optional HTTP status code only (never secrets).
3. `telemetry/tracker.py`: Monotonic `time.perf_counter_ns()` span tracker computing `waiting_ms`, `working_ms`, `retrying_ms`, `provider_first_token_ms`, `first_token_ms`, and `total_ms` with `abs(total_ms - (waiting_ms + working_ms + retrying_ms)) < 1.0`.
4. `telemetry/csv_writer.py`: Thread/async-safe CSV writer writing `results.csv` (blank `""` for `None` floats) and appending JSONL trace records to `runs/traces.jsonl`.

RESTRICTIONS:
- NEVER run `git commit` or `git push`.
- Inspect repo first, keep code modular and strictly typed.
- Run `pytest` and `ruff check .` after final edits.
- Update `ENGINEERING_LOG.md`, create `reports/phase-01-foundation-telemetry.md` using `reports/IMPLEMENTATION_REPORT_TEMPLATE.md`, summarize verified outputs, and STOP for owner review.
```

---

### Phase 02 — Exact arXiv PDF Downloading, 2-Column Extraction & Metadata Chunking

- **Objective:** Fetch exact versions (`1706.03762v7`, `2410.00037v1`, `2507.07955v2`), verify SHA-256 checksums, extract layout-ordered pages, track sections/references, and verify chunking.
- **Dependencies:** Phase 01 approved.
- **Files:** `src/relay/ingestion/__init__.py`, `src/relay/ingestion/arxiv_fetcher.py`, `src/relay/ingestion/pdf_parser.py`, `scripts/ingest_papers.py` (fetch + parse verification), `tests/test_ingestion.py`.
- **Acceptance Criteria:**
  - Exact 3 URLs downloaded into `data/papers/`, verified with `%PDF-` header & SHA-256 `data/papers/manifest.json`.
  - Geometric 2-column sorting reads left column before right column on `1706.03762v7`.
  - Reference/Bibliography sections tagged `is_reference=True`; hyphenation cleaned; page & section preserved on every `PassageChunk`.
  - Offline `pytest` synthesizing a 2-col test PDF + `ruff check .` pass.
- **Required Report:** `reports/phase-02-pdf-ingestion.md`

#### Copy-Ready Prompt — Phase 02
```text
Execute Phase 02 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 02), and ENGINEERING_LOG.md, and inspect existing Phase 01 files before making edits. Do NOT modify Phase 01 schemas/config unless fixing an explicitly documented bug.

OBJECTIVE: Implement exact-version arXiv PDF retrieval, SHA-256 manifest verification, multi-column-aware PyMuPDF extraction, section/reference detection, and token-aware chunking.

FILES TO CREATE:
- `src/relay/ingestion/__init__.py`
- `src/relay/ingestion/arxiv_fetcher.py`
- `src/relay/ingestion/pdf_parser.py`
- `scripts/ingest_papers.py` (Stage 1: download, manifest write, parse verification)
- `tests/test_ingestion.py`

REQUIREMENTS:
1. Pin exact URLs:
   - `attention_1706`: `https://arxiv.org/pdf/1706.03762v7.pdf` (`1706.03762`, v7)
   - `moshi_2410`: `https://arxiv.org/pdf/2410.00037v1.pdf` (`2410.00037`, v1)
   - `hnet_2507`: `https://arxiv.org/pdf/2507.07955v2.pdf` (`2507.07955`, v2)
2. Write `data/papers/manifest.json` with `paper_id`, `arxiv_id`, `version`, `url`, `sha256`, `page_count` and validate hashes on subsequent runs.
3. In `pdf_parser.py`: use `fitz` (`pymupdf`) block bounding boxes `(x0, y0, x1, y1)` to detect 2-column pages (critical for `1706.03762v7`) so full-width headers read first, then left column top-to-bottom, then right column top-to-bottom. Join line-wrap hyphens (`-\n`), preserve table readability, track `current_section`, and mark `References` / `Bibliography` chunks as `is_reference=True`.
4. Implement token-aware `chunk_document` using `tiktoken` (`cl100k_base`) generating deterministic `chunk_id` (`{paper_id}_p{page}_c{idx}`).
5. Run `scripts/ingest_papers.py --parse-only` (if network access is available in environment; if blocked, report honestly) and inspect extracted text from pages 1–4 of `1706.03762v7` to confirm zero column interleaving.
6. Write offline `tests/test_ingestion.py` using a `pymupdf`-generated synthetic 2-column PDF in `tmp_path` testing column order, `is_reference` tagging, and chunk determinism.

RESTRICTIONS: NEVER run `git commit` or `git push`. Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-02-pdf-ingestion.md`, report facts, and STOP.
```

---

### Phase 03 — Local Hybrid Index (`BGE` + `BM25` + `ONNX Rerank`) & Verified 18-Question Eval Set

- **Objective:** Build persistent per-preset indexes (`small_fast`, `balanced_rerank`, `large_wide`), query scope analyzer, and write/verify the 18-question evaluation dataset directly against the downloaded PDF pages.
- **Dependencies:** Phase 02 approved.
- **Files:** `src/relay/retrieval/__init__.py`, `src/relay/retrieval/embedder.py`, `src/relay/retrieval/hybrid_index.py`, `src/relay/retrieval/scope.py`, `scripts/ingest_papers.py` (extend to index), `eval/questions.json`, `tests/test_retrieval.py`.
- **Acceptance Criteria:**
  - All 3 index bundles persisted under `data/indexes/<setting>/` with `corpus_sha256` manifest.
  - Hybrid RRF fuses exact NumPy cosine + BM25, down-weights `is_reference=True` (`0.2x`), and toggles `Xenova/ms-marco-MiniLM-L-6-v2` cross-encoder reranking.
  - `eval/questions.json` contains $\ge 18$ questions across all 6 categories (direct factual, multi-hop, technical details, ambiguous, 3 strictly unanswerable, 3 retriever-trap questions) with every `gold_papers`/`gold_pages` verified against actual extracted PDF text.
  - `pytest` and `ruff check .` pass offline.
- **Required Report:** `reports/phase-03-retrieval-evalset.md`

#### Copy-Ready Prompt — Phase 03
```text
Execute Phase 03 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 03), and ENGINEERING_LOG.md, and inspect existing Phase 01–02 files first. Preserve working code.

OBJECTIVE: Build the local hybrid retrieval engine (exact NumPy cosine + BM25 + RRF + ONNX cross-encoder reranker), deterministic query scope analyzer, and create & verify `eval/questions.json` (>=18 questions grounded directly in the extracted PDF pages).

FILES TO CREATE / MODIFY:
- `src/relay/retrieval/__init__.py`
- `src/relay/retrieval/embedder.py`
- `src/relay/retrieval/hybrid_index.py`
- `src/relay/retrieval/scope.py`
- `scripts/ingest_papers.py` (extend to build all 3 index presets)
- `eval/questions.json`
- `tests/test_retrieval.py`

REQUIREMENTS:
1. `embedder.py`: Lazy `fastembed` wrappers for `TextEmbedding("BAAI/bge-small-en-v1.5")` and `TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")` behind `Protocol` interfaces so offline unit tests can inject deterministic fake embedders without loading ONNX models.
2. `hybrid_index.py`: Build & save `chunks.json`, `embeddings.npy`, `bm25_tokens.json`, and `index_meta.json` (`corpus_sha256` + preset). Implement `search(query, *, paper_filter, top_k, fetch_k, rerank)`: exact cosine + BM25 RRF fusion, optional paper filter, `0.2x` weight on `is_reference=True` chunks unless query explicitly asks about references, and optional `TextCrossEncoder` rerank over `fetch_k` candidates.
3. `scope.py`: Deterministic `analyze_query(query) -> QueryPlan` extracting paper hints (`attention_1706`, `moshi_2410`, `hnet_2507`), technical identifiers (`d_model`, `d_ff`, `12.5 hz`, `ratio loss`, `RoPE`, etc.), comparison intent, and `ambiguous_scope`.
4. `eval/questions.json`: Author 18 questions (3 direct factual, 3 multi-hop, 3 technical detail/equation, 3 ambiguous cross-paper, 3 strictly unanswerable, 3 retriever-trap questions covering the 3 concrete failure modes in ARCHITECTURE/eval design). Inspect actual extracted PDF text in `data/papers/` to record exact verified `gold_papers`, `gold_pages`, `required_facts`, `forbidden_confusions`, and confirm unanswerable questions truly do not appear in any of the 3 PDFs!
5. Run `scripts/ingest_papers.py` to build all 3 setting indexes and verify offline retrieval hits gold pages in `balanced_rerank` top-k.
6. Write offline `tests/test_retrieval.py` testing RRF, Reference down-weighting, paper filtering, and index save/load parity with mock embedder.

RESTRICTIONS: NEVER run `git commit` or `git push`. Zero Groq API calls. Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-03-retrieval-evalset.md`, report facts, and STOP.
```

---

### Phase 04 — Multi-Step AI Pipeline, `ToolGateway`, `EvidenceGate` & Verify-Before-Delivery `CitationVerifier`

- **Objective:** Build `ModelProvider` + `ScriptedProvider`, `ToolGateway`, `EvidenceGate`, deterministic `CitationVerifier`, and the 6-stage async `RelayPipeline` honoring Verify-Before-Delivery.
- **Dependencies:** Phase 01, Phase 03 approved.
- **Files:** `src/relay/providers/__init__.py`, `src/relay/providers/base.py`, `src/relay/orchestrator/__init__.py`, `src/relay/orchestrator/tools.py`, `src/relay/retrieval/evidence_gate.py`, `src/relay/orchestrator/verifier.py`, `src/relay/orchestrator/pipeline.py`, `tests/test_pipeline_and_verifier.py`.
- **Acceptance Criteria:**
  - Pipeline proves $\ge 4$ steps, formal `ToolGateway` calls (`retrieve_passages`, `fetch_section_context`), bounded branch/loop (`loop_count <= 1`), cited synthesis, and early grounded refusal (0 LLM tokens).
  - Verify-Before-Delivery buffers upstream stream (`provider_first_token_ms`), runs `CitationVerifier`, and delivers to client ONLY upon `VERIFIED` / `REFUSAL_VERIFIED` (`first_token_ms` = user-visible TTFT).
  - Unverified outputs blocked (`error_code="citation_verification_failed"`).
  - Offline `pytest` + `ruff check .` pass.
- **Required Report:** `reports/phase-04-ai-pipeline.md`

#### Copy-Ready Prompt — Phase 04
```text
Execute Phase 04 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 04), and ENGINEERING_LOG.md, and inspect Phases 01–03 first. Do NOT modify completed modules unnecessarily.

OBJECTIVE: Implement the provider abstraction (`ScriptedProvider`), HABIT-inspired `ToolGateway`, deterministic `EvidenceGate`, `CitationVerifier`, and the 6-stage `RelayPipeline` enforcing Verify-Before-Delivery.

FILES TO CREATE:
- `src/relay/providers/__init__.py`
- `src/relay/providers/base.py`
- `src/relay/orchestrator/__init__.py`
- `src/relay/orchestrator/tools.py`
- `src/relay/retrieval/evidence_gate.py`
- `src/relay/orchestrator/verifier.py`
- `src/relay/orchestrator/pipeline.py`
- `tests/test_pipeline_and_verifier.py`

REQUIREMENTS:
1. `providers/base.py`: `ModelProvider` Protocol (`stream_chat`) yielding `TokenDelta`, `HeaderMeta`, and `UsageEvent`. Normalized errors: `RateLimitError(retry_after_s, is_daily, headers)`, `ProviderTransientError(status_code)`, `PayloadTooLargeError`, `ProviderAuthError`, `ProviderTimeoutError`. Include `ScriptedProvider` for deterministic offline tests.
2. `orchestrator/tools.py` (`ToolGateway`): Register `retrieve_passages` and `fetch_section_context` with Pydantic input validation, tool allowlist, `asyncio.wait_for(..., timeout=tool_timeout_s)`, and per-call timing (`retrieval_ms`).
3. `retrieval/evidence_gate.py`: Evaluate retrieved chunks vs `QueryPlan`: detect missing cross-paper coverage, reference-only top chunks, or below-threshold relevance; return `SUFFICIENT`, `LOOP_EXPAND` (only if `loop_count == 0`), or `EARLY_REFUSAL`.
4. `orchestrator/verifier.py` (`CitationVerifier`): Pure-Python structural & attribution integrity check (explicitly NOT semantic entailment!). Reject answers with: (a) 0 `[Pi]` tags on non-refusal answers, (b) out-of-range `[Pi]` tags, (c) explicit arXiv ID/page mismatches against tagged chunk metadata, or (d) contradictory facts mixed into an `INSUFFICIENT_EVIDENCE` refusal.
5. `orchestrator/pipeline.py` (`RelayPipeline`): Implement 6 stages: (1) `analyze_query`; (2) `ToolGateway.retrieve_passages`; (3) `EvidenceGate` branch/loop (`loop_count <= 1`, deadline check); (4) Grounded prompt assembly `[P1]..[Pk]`; (5) Upstream streaming into in-memory buffer recording `provider_first_token_ms`; (6) `CitationVerifier` gate -> ONLY deliver verified response (`first_token_ms` = user-visible delivery timestamp!). Record full `RequestTracker` spans cleanly.

TESTING & ACCEPTANCE CRITERIA:
- `tests/test_pipeline_and_verifier.py` (100% offline via `ScriptedProvider`):
  - Verified cited answer delivers cleanly with `provider_first_token_ms <= first_token_ms` and valid resolved citations.
  - Hallucinated `[P9]` or uncited response is BLOCKED by Verify-Before-Delivery (`status="error"`, `error_code="citation_verification_failed"`).
  - Cross-paper missing-scope query loops exactly once (`loop_count == 1`), never >=2.
  - Out-of-corpus query triggers `EARLY_REFUSAL` (`tokens_in=0, tokens_out=0`, `provider_called=False`).
  - Simulated tool timeout inside `ToolGateway` fails cleanly without crashing.
- Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-04-ai-pipeline.md`, report facts, and STOP.
```

---

### Phase 05 — Real Groq HTTP/SSE Provider Integration & Budgeted Probing

- **Objective:** Implement unified `HttpOpenAIProvider` (`max_retries=0`, SSE + `include_usage`, accurate Groq `x-ratelimit-*-requests = RPD` & `x-ratelimit-*-tokens = TPM` header parsing) and `scripts/probe_groq.py` (strictly capped probe script).
- **Dependencies:** Phase 04 approved.
- **Files:** `src/relay/providers/http_provider.py`, `scripts/probe_groq.py`, `tests/test_http_provider.py`.
- **Acceptance Criteria:**
  - `HttpOpenAIProvider` works cleanly via configurable `base_url` (Groq or Stand-In) with zero SDK internal retries.
  - Header parser maps `x-ratelimit-*-requests` strictly to **RPD**, `x-ratelimit-*-tokens` to **TPM**, parses duration strings (`"1m30s"`, `"12h5m3s"`), and separates `reasoning` vs visible `content` deltas.
  - `MockTransport` unit tests + `ruff check .` pass offline. (Live `probe_groq.py` executes ONLY if user has set `GROQ_API_KEY` or awaits user instruction!)
- **Required Report:** `reports/phase-05-groq-integration.md`

#### Copy-Ready Prompt — Phase 05
```text
Execute Phase 05 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 05), and ENGINEERING_LOG.md, and inspect Phases 01–04 first.

OBJECTIVE: Implement the shared HTTP/SSE OpenAI-compatible provider (`HttpOpenAIProvider`) and the quota-guarded Groq probing script (`scripts/probe_groq.py`) for Table 1 discovery.

FILES TO CREATE:
- `src/relay/providers/http_provider.py`
- `scripts/probe_groq.py`
- `tests/test_http_provider.py`

REQUIREMENTS:
1. `http_provider.py` (`HttpOpenAIProvider`):
   - Built directly on `httpx.AsyncClient` with `max_retries=0` (NO automatic hidden retries) and explicit connect/read/total timeouts.
   - Sends `stream=True` and `stream_options={"include_usage": True}`; supports optional `reasoning_format="parsed"` / `reasoning_effort` config so thinking doesn't silently stall visible tokens.
   - Safely handles final usage chunk where `choices == []`.
   - Extracts case-insensitive Groq headers with CORRECT semantics:
     - `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-reset-requests` -> **RPD (Requests Per Day)**
     - `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens` -> **TPM (Tokens Per Minute)**
     - `retry-after` (on `429` only) -> exact seconds wait.
   - Parses duration strings (`"500ms"`, `"12.5s"`, `"1m30s"`, `"2h15m"`) into float seconds.
   - Maps `429` (inspecting JSON body/message & `retry-after` to flag daily RPD/TPD vs minute RPM/TPM), `413`, `401/403`, `500/502/503/504`, and timeouts to Phase 04 error types. Implements `list_models()` (`GET /models`, 0 quota).
2. `scripts/probe_groq.py`:
   - Enforces `--max-calls` hard safety cap (default `60`).
   - Phases: `discover` (`GET /models` + 5 spaced calls), `concurrency` (12 calls at c=1,2,4), `limit-test` (controlled burst/context calls to trigger 1 real 429 and poll every 2s until recovery).
   - Appends every raw probe attempt (sanitized—NO API key!) to `runs/groq_probes.jsonl` and generates `runs/table1_probe_summary.json` clearly marking observed vs `"not measured"`.

TESTING & SECURITY:
- Write offline `tests/test_http_provider.py` using `httpx.MockTransport` testing SSE + empty-choices usage chunk, RPD vs TPM header parsing, duration string parsing, 429 daily vs minute classification, 413/503/timeout mapping, and zero hidden transport retries.
- Do NOT call live Groq API unless `GROQ_API_KEY` is explicitly present AND user authorized live probing (if key absent, test completely offline and report exact ready-to-run probe commands!).
- Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-05-groq-integration.md`, report facts, and STOP.
```

---

### Phase 06 — Control Plane: `IngressGate` Bounded Queue & Single-Owner `ProviderGuard`

- **Objective:** Implement cleanly separated `IngressGate` (`BoundedRequestQueue` with FIFO deadlines, client-disconnect abort, predictive shedding) and `ProviderGuard` (single owner of RPM/TPM/RPD/TPD, `max_in_flight` semaphore, `RetryBudget`, `CircuitBreaker`, full-jitter backoff).
- **Dependencies:** Phase 04, Phase 05 approved.
- **Files:** `src/relay/control/__init__.py`, `src/relay/control/queue_manager.py`, `src/relay/control/provider_guard.py`, `src/relay/control/retry.py`, `tests/test_control_plane.py`.
- **Acceptance Criteria:**
  - Zero duplicate rate-limit state across modules (`ProviderGuard` is the sole quota owner).
  - Queue full -> immediate fast reject (`queue_depth_at_entry == max_queue_depth`, `status="rate_limited"`).
  - Expired deadline or disconnected client dropped BEFORE calling upstream (`0` tokens).
  - Daily budget exhausted -> instant fast fail (`<10ms`, `0` retries, `0` upstream calls).
  - `RetryBudget` (toggleable for retry-amplification experiment) and `CircuitBreaker` verified offline.
- **Required Report:** `reports/phase-06-control-plane.md`

#### Copy-Ready Prompt — Phase 06
```text
Execute Phase 06 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 06), and ENGINEERING_LOG.md, and inspect Phases 01–05 first.

OBJECTIVE: Implement cleanly decoupled application admission control (`BoundedRequestQueue`) and single-owner upstream quota & resilience (`ProviderGuard`, `RetryPolicy`, `RetryBudget`, `CircuitBreaker`).

FILES TO CREATE:
- `src/relay/control/__init__.py`
- `src/relay/control/queue_manager.py`
- `src/relay/control/retry.py`
- `src/relay/control/provider_guard.py`
- `tests/test_control_plane.py`

REQUIREMENTS:
1. `queue_manager.py` (`BoundedRequestQueue`):
   - FIFO `asyncio.Queue(maxsize=max_queue_depth)`.
   - Captures `queue_depth_at_entry` immediately upon arrival. Rejects immediately (`QueueFullError`) when full.
   - Predictive wait shedding: tracks EMA of service latency; if `estimated_wait_ms > remaining_deadline_ms * 0.85`, sheds immediately before enqueue/dispatch.
   - Checks deadline expiration and optional `is_disconnected()` async callback before starting work and before calling provider -> aborts cleanly (`status="timeout"`).
2. `retry.py`:
   - `RetryBudget`: rolling window capping retries to `<= max_retry_ratio * normal_requests` (default 0.20, min burst 3), PLUS `enabled: bool = True` flag so Phase 9 can compare retry-budget-on vs retry-amplification-off!
   - `CircuitBreaker`: `CLOSED -> OPEN -> HALF_OPEN` after consecutive transient 5xx/timeouts; configurable cooldown (10s default).
   - `compute_backoff`: full-jitter exponential backoff honoring `retry_after_s`; returns `None` (fail fast) if `retry_after_s > remaining_deadline_s * 0.65`, daily exhausted, or non-retryable (`400`, `401`, `413`).
3. `provider_guard.py` (`ProviderGuard`):
   - Sole owner of upstream quota & resilience: `asyncio.Semaphore(max_in_flight)`, sliding 60s RPM & TPM windows, daily RPD & TPD counters, `sync_from_headers(...)` updating **RPD** from `x-ratelimit-*-requests` and **TPM** from `x-ratelimit-*-tokens`, `reserve` + `settle` TPM delta accounting, `mark_daily_exhausted`, and `should_degrade_retrieval(est_tokens)` when TPM headroom drops below threshold.
   - Owns the retry execution loop around `ModelProvider.stream_chat`.

TESTING & ACCEPTANCE CRITERIA:
- Deterministic fast async unit tests in `tests/test_control_plane.py` covering queue-full fast fail, predictive wait shedding, disconnect cancellation before provider call, TPM reserve/settle accuracy, RPD header sync (not confused with RPM!), daily exhaustion fast-fail, `RetryBudget` storm throttling, and `CircuitBreaker` transitions.
- Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-06-control-plane.md`, report facts, and STOP.
```

---

### Phase 07 — SingleFlight, Baseline Exact Cache, Optional Calibratable Semantic Cache & FastAPI Service

- **Objective:** Build `SingleFlight`, SQLite `AnswerCache` (`off | exact | semantic`), wire complete pipeline + control plane into FastAPI `src/relay/api/app.py`, and prove hard-negative safety.
- **Dependencies:** Phase 06 approved.
- **Files:** `src/relay/control/cache.py`, `src/relay/orchestrator/pipeline.py` (cleanly wire cache/guard/queue hooks if not already decoupled), `src/relay/api/__init__.py`, `src/relay/api/app.py`, `tests/test_cache_and_api.py`.
- **Acceptance Criteria:**
  - SingleFlight collapses concurrent duplicate requests cleanly (`1` upstream call; coalesced rows log `tokens=0, provider_called=False`).
  - Cache admission rejects `UNVERIFIED`, failed, and `degraded_mode` answers.
  - L2 Semantic cache thresholds live in explicit `SemanticCacheConfig` (default uncalibrated/conservative until Phase 9 empirical calibration, NOT claimed as proven constants!) and enforces deterministic Scope & Identifier Lock + Margin.
  - FastAPI `POST /ask` (SSE & JSON) and `GET /status` work end-to-end offline.
- **Required Report:** `reports/phase-07-cache-and-api.md`

#### Copy-Ready Prompt — Phase 07
```text
Execute Phase 07 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 07), and ENGINEERING_LOG.md, and inspect Phases 01–06 first.

OBJECTIVE: Implement SingleFlight coalescing, SQLite-backed L1 Exact Cache + optional L2 Guarded Semantic Cache (calibrated via config, zero hardcoded "proven" thresholds), wire the complete service inside FastAPI `src/relay/api/app.py`, and verify Verify-Before-Delivery & hard-negative cache safety.

FILES TO CREATE / MODIFY:
- `src/relay/control/cache.py`
- `src/relay/orchestrator/pipeline.py` (clean integration with `IngressGate`/`ProviderGuard`/`AnswerCache`, minimal non-breaking edits)
- `src/relay/api/__init__.py`
- `src/relay/api/app.py`
- `tests/test_cache_and_api.py`

REQUIREMENTS:
1. `control/cache.py`:
   - `SingleFlightCoalescer`: coalesces concurrent misses keyed by `(canonical_query, retrieval_setting, scope_key)`. Leader executes once; followers await shared result and log `cache_status="coalesced"`, `provider_called=False`, `tokens_in=0`, `tokens_out=0`. Handle individual client disconnect without cancelling other waiting callers.
   - `AnswerCache` (SQLite `data/cache.db`):
     - Modes: `"off"`, `"exact"` (L1 normalized SHA-256), `"semantic"` (L1 + L2).
     - Composite key isolation: `(corpus_sha256, retrieval_setting, prompt_version, model_id)`.
     - L2 Guarded Semantic lookup (active only when `cache_mode=="semantic"`): requires strict match on `QueryPlan.papers` + extracted technical identifiers (`d_model`, `d_ff`, `RoPE`, etc.), embedding cosine `>= sim_threshold`, ONNX `TextCrossEncoder` pair score `>= ce_threshold`, and HABIT-style runner-up margin `>= ambiguity_margin` between distinct answer clusters. Keep thresholds configurable via `SemanticCacheConfig` (explicitly marked for empirical calibration in Phase 9).
     - Admission gate (`admit`): stores ONLY `VERIFIED`/`REFUSAL_VERIFIED` and `not degraded_mode` outputs.
2. `api/app.py`:
   - Single-process FastAPI `create_app(...)` wiring `IngressGate`, `BoundedRequestQueue`, `ProviderGuard`, `AnswerCache`, `SingleFlight`, `RelayPipeline`, and `TelemetryWriter`.
   - `POST /ask`: supports `stream=True` SSE and JSON, passes `request.is_disconnected`, implements 3 PM daily-exhausted behavior (L1/L2 cache hits served 200; misses fast-fail `<50ms` with honest reset message + top retrieved passage pointers, `status="rate_limited"`, 0 upstream calls).
   - `GET /status`: live queue depth, RPD/TPM estimates, circuit state, cache stats.

TESTING & ACCEPTANCE CRITERIA:
- `tests/test_cache_and_api.py` (offline via `ScriptedProvider` + `ASGITransport`):
  - SingleFlight collapses 10 concurrent identical misses to 1 `ScriptedProvider` call.
  - Hard-negative parameter swap (`d_model` vs `d_ff`) and paper swap (`Moshi` vs `Attention`) fail closed (cache MISS).
  - Unverified answer (`[P9]`) is NEVER admitted to `cache.db`.
  - Daily exhausted mode serves verified cache hits while fast-failing misses with honest pointers.
- Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-07-cache-and-api.md`, report facts, and STOP.
```

---

### Phase 08 — Calibrated Standalone HTTP/SSE Stand-In Service

- **Objective:** Implement `src/relay/standin/server.py`, `calibrator.py`, and `scripts/calibrate_standin.py` speaking OpenAI/Groq `/openai/v1/chat/completions` over HTTP/SSE.
- **Dependencies:** Phase 05 approved.
- **Files:** `src/relay/standin/__init__.py`, `src/relay/standin/calibrator.py`, `src/relay/standin/server.py`, `scripts/calibrate_standin.py`, `tests/test_standin.py`.
- **Acceptance Criteria:**
  - Simulates variable log-normal TTFT/TPS, slow-success outliers, `500`/`503` failures, load-dependent slowdown under concurrency, sliding-window RPM/TPM + daily RPD/TPD limits, exact `429` + `Retry-After` + `x-ratelimit-*` headers, recovery, and token-by-token SSE streaming with `include_usage`.
  - `calibrator.py` fits profile strictly from measured `runs/groq_probes.jsonl` (or cleanly marks synthetic default profile `"uncalibrated_defaults"`).
  - Integration tests using actual `HttpOpenAIProvider` + `ruff check .` pass offline.
- **Required Report:** `reports/phase-08-standin-service.md`

#### Copy-Ready Prompt — Phase 08
```text
Execute Phase 08 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 08), and ENGINEERING_LOG.md, and inspect Phases 01–07 first.

OBJECTIVE: Implement the standalone HTTP/SSE Stand-In model server and calibration fitter so `HttpOpenAIProvider` exercises real network streaming, `429`/`Retry-After` headers, load degradation, failures, and slow-success stragglers.

FILES TO CREATE:
- `src/relay/standin/__init__.py`
- `src/relay/standin/calibrator.py`
- `src/relay/standin/server.py`
- `scripts/calibrate_standin.py`
- `tests/test_standin.py`

REQUIREMENTS:
1. `standin/calibrator.py` & `scripts/calibrate_standin.py`:
   - Reads `runs/groq_probes.jsonl` to fit shifted log-normal TTFT `(shift_ms, mu, sigma)` and inter-token delay `(ms_per_token_mu, ms_per_token_sigma)` plus observed limits (`rpm`, `tpm`, `rpd`, `tpd`).
   - If real probe file doesn't exist yet, supports emitting explicit `"calibration_source": "uncalibrated_defaults"` (never label defaults as Groq-measured!).
2. `standin/server.py`:
   - FastAPI app exposing `/openai/v1/chat/completions`, `/openai/v1/models`, `/admin/config`, `/admin/reset`, `/admin/stats`.
   - Seeded RNG (`random.Random(seed)`).
   - Emits realistic `x-ratelimit-limit-requests` (RPD), `x-ratelimit-remaining-requests` (RPD), `x-ratelimit-reset-requests`, `x-ratelimit-limit-tokens` (TPM), `x-ratelimit-remaining-tokens` (TPM), `x-ratelimit-reset-tokens`.
   - Enforces RPM, TPM, RPD, TPD -> returns Groq-formatted `429` JSON + `retry-after` header when exceeded, and recovers cleanly as windows reset.
   - Simulates concurrency slowdown (`in_flight > load_knee`), probabilistic `503` errors, slow-success outliers (`slow_outlier_prob` * multiplier), and token-by-token SSE deltas + final empty-`choices` `usage` chunk.
   - Generates grounded extractive dummy answers citing `[P1]` from the prompt's `[P1]..[Pk]` blocks (purely for performance/plumbing tests; clearly distinct from real LLM quality evaluation!).
   - Stops streaming cleanly if client disconnects.

TESTING & ACCEPTANCE CRITERIA:
- `tests/test_standin.py` driving Stand-In via `HttpOpenAIProvider`:
  - Verifies SSE token streaming + exact `usage`.
  - Verifies RPM/TPM/RPD exhaustion returns `RateLimitError` with accurate `is_daily` & `retry_after_s`, and recovers after reset.
  - Verifies concurrency load slowdown, injected 503 failure, and slow-success outlier behavior.
- Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-08-standin-service.md`, report facts, and STOP.
```

---

### Phase 09 — Load-Testing Harness, Retrieval Experiments & Empirical Cache Calibration

- **Objective:** Implement reproducible workloads, hybrid rubric grader, `scripts/run_load_tests.py`, `scripts/run_retrieval_eval.py`, and L2 semantic threshold empirical calibration script.
- **Dependencies:** Phase 07, Phase 08 approved.
- **Files:** `src/relay/experiments/__init__.py`, `src/relay/experiments/workloads.py`, `src/relay/experiments/grader.py`, `scripts/run_load_tests.py`, `scripts/run_retrieval_eval.py`, `scripts/calibrate_cache.py`, `tests/test_experiments_and_grader.py`.
- **Acceptance Criteria:**
  - Covers all $\ge 5$ mandatory Table 2a scenarios (`steady`, `burst`, `sustained_overload`, `halved_limit`, `daily_exhausted`) + `retry_amplification_demo` + isolated cache ablations.
  - Retrieval evaluation runs cleanly with `cache_mode="off"` and paced real requests (`--max-real-calls` guard!), saving full answer audit JSON.
  - Calibration script determines empirical L2 cache thresholds (`sim_threshold`, `ce_threshold`, `margin`) from labeled paraphrase vs hard-negative data without guessing constants.
  - Offline smoke suite + `ruff check .` pass.
- **Required Report:** `reports/phase-09-experiments.md`

#### Copy-Ready Prompt — Phase 09
```text
Execute Phase 09 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 09), and ENGINEERING_LOG.md, and inspect Phases 01–08 first.

OBJECTIVE: Implement the reproducible load-test runner, retrieval-setting evaluator, hybrid objective grader, and empirical semantic-cache threshold calibrator, writing raw rows cleanly to root `results.csv`.

FILES TO CREATE:
- `src/relay/experiments/__init__.py`
- `src/relay/experiments/workloads.py`
- `src/relay/experiments/grader.py`
- `scripts/run_load_tests.py`
- `scripts/run_retrieval_eval.py`
- `scripts/calibrate_cache.py`
- `tests/test_experiments_and_grader.py`

REQUIREMENTS:
1. `experiments/workloads.py`: Seeded (`seed=42`) generators for: `steady`, `steady_standin`, `burst`, `sustained_overload`, `halved_limit`, `daily_exhausted`, `retry_amplification_demo` (`retry_budget` disabled vs enabled), and isolated cache ablation scenarios (`cache_disabled`, `cold_burst_sf`, `warm_equiv`, `warm_unique`, `hard_negatives`).
2. `experiments/grader.py`: Hybrid deterministic rule + rubric oracle evaluating `eval/questions.json` outputs into strictly the 4 allowed enum labels (`correct`, `wrong`, `refused_correctly`, `refused_wrongly`). Enforce: missing/wrong citation on an otherwise accurate fact -> `wrong`! Ungraded load-stress or failed rows leave `answer_grade=""`.
3. `scripts/run_retrieval_eval.py`: Runs the 18+ questions across `small_fast`, `balanced_rerank`, and `large_wide` with `cache_mode="off"`, configurable `--source real|stand-in`, `--pace-seconds`, and `--max-real-calls` hard guard. Writes `runs/retrieval_answers_audit.json` for manual inspection/override + appends rows to `results.csv`.
4. `scripts/run_load_tests.py`: Launches/configures local Stand-In (or attaches to real Groq for `steady`), runs selected scenarios, appends cleanly to `results.csv` + `runs/traces.jsonl`.
5. `scripts/calibrate_cache.py`: Sweeps `(sim_threshold, ce_threshold, margin)` over labeled equivalent-paraphrase pairs and hard-negative technical-swap pairs to select the lowest false-hit (`0.0%`) operating point empirically and save `runs/cache_calibration.json`.

TESTING & EXECUTION:
- Write offline `tests/test_experiments_and_grader.py` testing all 4 `answer_grade` branches and running a quick 10-request Stand-In end-to-end smoke run into `tmp_path/smoke_results.csv`.
- Do NOT fire bulk real Groq requests automatically unless explicitly confirmed/keyed; run offline + Stand-In smoke tests only and report exact CLI commands for Phase 9/10 live execution.
- Run `pytest` and `ruff check .`, update `ENGINEERING_LOG.md`, write `reports/phase-09-experiments.md`, report facts, and STOP.
```

---

### Phase 10 — Metric Recomputation, Slow-Request Forensics & Submission Audit

- **Objective:** Implement `compute_tables.py` recomputing Tables 1, 2a, 2b, 3, cache stats, and slow-request latency decomposition strictly from root `results.csv` & `runs/`, plus run final verification/hygiene checks.
- **Dependencies:** Phase 09 approved.
- **Files:** `src/relay/experiments/tables.py`, `scripts/compute_tables.py`, `tests/test_tables_recomputation.py`.
- **Acceptance Criteria:**
  - Every cell in Tables 2a, 2b, 3 recomputed strictly from `results.csv` (linear quantiles in ms, 1-decimal `%`, `"not measured"` when `N == 0`).
  - Slow-request forensic output proves `waiting_ms + working_ms + retrying_ms == total_ms` and pre/post-`first_token_ms` split against `runs/traces.jsonl`.
  - Pre-submission audit confirms zero committed secrets, `results.csv` tracked cleanly, all unit/smoke tests passing.
- **Required Report:** `reports/phase-10-verification-tables.md`

#### Copy-Ready Prompt — Phase 10
```text
Execute Phase 10 of The Relay strictly following CLAUDE.md.

Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (Phase 10), and ENGINEERING_LOG.md, and inspect Phases 01–09 first. Do NOT rewrite earlier working components unless fixing a verified bug.

OBJECTIVE: Implement deterministic table recomputation (`scripts/compute_tables.py`), slow-request forensic decomposition, and execute a full pre-submission hygiene & end-to-end validation audit.

FILES TO CREATE:
- `src/relay/experiments/tables.py`
- `scripts/compute_tables.py`
- `tests/test_tables_recomputation.py`

REQUIREMENTS:
1. `experiments/tables.py` & `scripts/compute_tables.py`:
   - Reads root `results.csv` (plus `runs/table1_probe_summary.json` if present) and validates every invariant: exact mandatory columns 1–12, valid enum values (`""` permitted for unanswered `answer_grade`), blank `first_token_ms` when no user byte delivered, `abs(total_ms - (waiting_ms + working_ms + retrying_ms)) <= 2.0`.
   - Computes Tables 2a, 2b, 3 using linear-interpolation `np.percentile(..., method="linear")` in milliseconds and 1-decimal percentages (`XX.X%`), outputting `"not measured"` where sample count is zero.
   - Outputs both `first_token_ms` (user-visible TTFT) and `provider_first_token_ms` diagnostics, plus cache hits / provider calls avoided.
   - Extracts one slow `status=="ok"` request (or `--request-id`), cross-references `runs/traces.jsonl`, and prints exact `waiting_ms`, `working_ms`, `retrying_ms`, `provider_first_token_ms`, `first_token_ms`, and pre- vs post-first-token delay analysis for Report Heading 4.
   - Writes Markdown tables to `runs/computed_report_tables.md`.
2. Final Audit Checks:
   - Run full `pytest -v` and `ruff check .`.
   - Check `git status` and search repository files (excluding gitignored `.env`) for any leaked `gsk_` key strings.
   - Run an end-to-end Stand-In rehearsal writing to `runs/smoke_results.csv` and verify `compute_tables.py --input runs/smoke_results.csv` exits `0` with exact matching math.

RESTRICTIONS: NEVER run `git commit` or `git push`. Never fabricate live Groq measurements. Update `ENGINEERING_LOG.md`, write `reports/phase-10-verification-tables.md`, report exact observed results and live-run readiness steps, and STOP.
```
