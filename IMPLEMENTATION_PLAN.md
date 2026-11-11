# IMPLEMENTATION_PLAN.md — Phased Execution Roadmap (v2, Phase 00B)

> **Governance:** Every phase follows `CLAUDE.md`: read governance docs and inspect the repo first; present a plan; implement only the current phase; never commit or push; run `pytest` and `ruff check .` after the final edit; append to `ENGINEERING_LOG.md`; write `reports/phase-XX-*.md`; report verified facts; **STOP** for owner approval.
>
> **Architecture reference:** `ARCHITECTURE.md` v2. Decisions marked [OPEN] there are not to be implemented as final until approved.
>
> **Real-quota rule:** No phase sends real Groq requests unless the owner explicitly authorises that run, with a hard `--max-real-calls` cap. Deliberately triggering a rate limit needs its own separate approval. The daily quota is never exhausted for testing.

---

## 1. Schedule (3 days available, final-day buffer)

| Day | Slot | Phase | Real Groq calls |
| :--- | :--- | :--- | :--- |
| **Day 1** | AM | **P01** Foundation, contracts, run directories, aggregation | none |
| | Midday | **P02** Provider client + probe tooling | none (offline tests) |
| | PM | **P02-LIVE** Model discovery + baseline probes *(owner-approved)* | ≤ 40 |
| | PM / Eve | **P03** PDF ingestion | none |
| **Day 2** | AM | **P04** Retrieval indexes + eval question schema & grading tooling *(owner writes questions in parallel)* | none |
| | AM | **P04B** Verify owner's questions against PDFs *(after owner delivers them)* | none |
| | Midday | **P05** Pipeline + evidence gate + verifier + safe refusal | none |
| | Midday | **P06** ProviderGuard + derived-capacity admission queue | none |
| | PM | **P07** Model comparison + Table 3 retrieval eval *(owner-approved, real, cache off)* | ≤ 80 |
| | Eve | **P08** FastAPI service + exact cache + SingleFlight | none |
| **Day 3** | Early AM | **P09** Stand-In service + calibration from P02-LIVE evidence | none |
| | AM | **P10** Load harness + scenarios + real steady run *(owner-approved)* | ≤ 30 |
| | Late AM | **P11** Table recomputation, slow-request forensics, submission audit | none |
| | **PM** | **Submission buffer** — owner write-up, Vaani visit, conversation export, final checks | none |

**Real-call budget (proposed, subject to measured limits):** Day 1 ≤ 40 (+ optional owner-approved minute-limit probe ≤ RPM + 5 tiny calls); Day 2 ≤ 80; Day 3 ≤ 30 plus reserve for one re-run. Every script also stops at the daily safety fraction (default 0.75) of known RPD/TPD. If measured TPD cannot fit the Day 2 eval, Table 3 is split across Days 2 and 3 and the plan is updated.

### Dependency graph
```
P01 ─┬─ P02 ── P02-LIVE ──────────────┬──────────── P09 ─┐
     └─ P03 ── P04 ── P04B ─┐         │                  │
                P05 ────────┼── P06 ──┼── P07            ├── P10 ── P11 ── buffer
                            └─────────┴── P08 ───────────┘
```
- P05 needs P01 and P04 (retrieval); it uses a scripted provider, so it does not need P02-LIVE.
- P06 needs P02 (provider errors/headers) and P05.
- P07 needs P02-LIVE (model list, headers verified), P04B (verified questions), P05, P06.
- P09 needs P02-LIVE evidence (otherwise it must label itself `uncalibrated_defaults`).
- P10 needs P08 and P09. P11 needs P10 and P07.

### Cut line if the schedule slips (in this order)
1. Circuit breaker reduced to "open after N consecutive failures, fixed cooldown".
2. Retry-amplification demo reduced to one stand-in comparison (budget on vs off).
3. Stand-in stalled-stream mode dropped (keep slow-success stragglers).
4. Model comparison reduced to 2 candidates.

**Never cut:** the `results.csv` contract, real probes, owner-graded Table 3, the five Table 2a scenarios, verification-before-delivery, the evidence directories.

---

## 2. Phase Specifications

Each phase lists objective, files, acceptance criteria, and verification. Report path: `reports/phase-XX-<short-description>.md`.

---

### P01 — Foundation, Contracts, Run Directories and Aggregation (Day 1 AM)

- **Objective:** Package skeleton; typed config; frozen Pydantic contracts; error hierarchy; client/server timing helpers; run-directory + manifest writer; `results.csv` aggregation with validation and duplicate rejection.
- **Files:** `pyproject.toml`, `.gitignore`, `.env.example`, `README.md`, `src/relay/__init__.py`, `src/relay/config.py`, `src/relay/schemas.py`, `src/relay/errors.py`, `src/relay/telemetry/__init__.py`, `src/relay/telemetry/spans.py`, `src/relay/telemetry/run_store.py`, `scripts/build_results.py`, `evidence/canonical_runs.json` (empty list), `tests/test_contracts_and_aggregation.py`.
- **Dependencies (proposed):** fastapi, uvicorn, httpx, pydantic>=2, pydantic-settings, pymupdf, fastembed, rank-bm25, numpy; dev: pytest, pytest-asyncio, ruff. **No tiktoken.**
- **Acceptance criteria:**
  - `.gitignore` ignores `.env`, `scratch/`, `data/papers/*.pdf`, `data/indexes/`, caches; does **not** ignore `results.csv` or `evidence/` (verified with `git check-ignore`).
  - `ResultRow`: columns 1–12 exactly as `ASSIGNMENT.md`, then the appended columns of `ARCHITECTURE.md` §13.3; `answer_grade` accepts only the four labels or blank; enums for status, cache_status, verdict, usage_source.
  - Blank (not `0`/`-1`) for undelivered `first_token_ms` and missing spans.
  - Span invariant `waiting + working + retrying == server_total` (±1 ms) enforced in validation.
  - `build_results.py` reads only canonical runs, rejects duplicate `request_id`s and duplicate run selection, validates every row, and is deterministic (same inputs → byte-identical output).
  - Manifest writer never serialises secrets (test with a fake key value).
- **Verification:** offline `pytest`, `ruff check .`, `git check-ignore -v` output in report.

---

### P02 — Provider Client and Probe Tooling (Day 1 Midday)

- **Objective:** `HttpOpenAIProvider` for Groq and Stand-In; header capture and parsing; error mapping; model listing; capped probe script. All offline-tested.
- **Files:** `src/relay/providers/__init__.py`, `src/relay/providers/base.py` (protocol, events, `ScriptedProvider`), `src/relay/providers/http_provider.py`, `src/relay/providers/headers.py`, `scripts/probe_groq.py`, `tests/test_http_provider.py`.
- **Requirements:**
  - `httpx.AsyncClient`, no retry wrapper, transport `retries=0`; explicit connect/read/total timeouts; per-chunk stall timeout.
  - `stream=True` + `include_usage`; handle the final `choices: []` usage chunk; separate reasoning vs content deltas.
  - Parse all `x-ratelimit-*` and `retry-after` headers and duration strings, **and keep the raw header dict** so meanings can be verified (`ARCHITECTURE.md` §9.1); parser semantics are labelled "documented, unverified" until P02-LIVE.
  - Map 429 (minute vs daily classified from body/headers), 413, 401/403, 5xx, timeouts to typed errors.
  - `probe_groq.py` modes: `discover` (`GET /models` only), `baseline` (short calls per candidate model + a few representative-size prompts), `minute-limit` (requires `--owner-approved-rate-limit-probe` flag; tiny requests only), all under `--max-real-calls` and the daily safety fraction; writes `evidence/groq_probes/<run_id>/` with sanitised records (no auth, org IDs redacted).
- **Acceptance criteria:** MockTransport tests for SSE parsing, usage chunk, header capture/parsing, duration strings, error mapping, zero hidden retries (exactly one transport request per call), cap enforcement, sanitisation, and that `minute-limit` refuses to run without the flag.
- **Verification:** offline `pytest`, `ruff check .`. **No live calls in this phase.**

---

### P02-LIVE — Model Discovery and Baseline Probes (Day 1 PM, owner-approved operational run)

- **Objective:** First real evidence: enabled models, header behaviour, latency spread, token usage per prompt size.
- **Preconditions:** owner has put `GROQ_API_KEY` in `.env` and explicitly approves the run and the cap (proposed ≤ 40 calls).
- **Steps:** `discover` → owner confirms shortlist (≤ 3) → `baseline`. The optional minute-limit probe runs **only** after a second explicit approval.
- **Outputs:** `evidence/groq_probes/<run_id>/{manifest.json, probes.jsonl, summary.json}`; draft Table 1 with every unmeasured cell marked "not measured"; header-meaning verification table (documented meaning vs observed behaviour); observed `usage.prompt_tokens / prompt_chars` ratios.
- **Verification:** owner reviews raw `probes.jsonl`; log entry and `reports/phase-02-live-groq-probes.md` record exact call count, and no secrets appear (`grep` for key prefix and `Authorization`).

---

### P03 — PDF Ingestion (Day 1 PM/Eve)

- **Objective:** Fetch the exact versions, pin SHA-256, extract column-ordered text, tag references, chunk deterministically.
- **Files:** `src/relay/ingestion/__init__.py`, `arxiv_fetcher.py`, `pdf_parser.py`, `chunker.py`, `scripts/ingest_papers.py`, `tests/test_ingestion.py`.
- **Requirements:** URLs `https://arxiv.org/pdf/1706.03762v7`, `.../2410.00037v1`, `.../2507.07955v2`; `data/papers/manifest.json` (tracked) with sha256/page_count; 2-column ordering via block bounding boxes; hyphenation repair; section tracking; `is_reference`; word-based chunking with deterministic `chunk_id = {paper_id}_p{page}_c{idx}` and overlap.
- **Acceptance criteria:** manifest hashes stable across runs; synthetic 2-column test PDF reads left column then right; reference tagging test; chunk determinism test; **manual inspection excerpt** of Attention paper pages 1–4 and one table page included in the report.
- **Verification:** offline `pytest` (synthetic PDFs), `ruff check .`, one networked fetch (arXiv only).

---

### P04 — Retrieval Indexes, Scope Analyzer, Eval Schema and Grading Tooling (Day 2 AM)

- **Objective:** Three retrieval presets; deterministic scope analysis; the eval question template/validator and first-pass grading rubric. **Claude does not author the evaluation questions.**
- **Files:** `src/relay/retrieval/{__init__,embedder,hybrid_index,scope}.py`, `src/relay/eval/{__init__,questions,grader}.py`, `eval/questions.template.json`, `eval/README.md` (how to write questions, categories, grading rules), `scripts/build_indexes.py`, `scripts/validate_questions.py`, `tests/test_retrieval.py`, `tests/test_grader.py`.
- **Requirements:** embedder behind a Protocol (fake in tests); RRF fusion; reference down-weighting; paper filter; optional rerank; preset sizes finalised from inspected text; schema per `ARCHITECTURE.md` §11; grader proposes one of the four labels with reasons and supports owner overrides.
- **Acceptance criteria:** index save/load parity; RRF and down-weighting tests; grader tests for all four labels and the "correct fact, wrong citation → wrong" rule; validator rejects malformed questions and unknown labels.
- **Verification:** offline `pytest`, `ruff check .`; one networked model download for fastembed (reported).

### P04B — Verification of Owner-Written Questions (Day 2 AM, after delivery)

- **Objective:** Check each owner question's expected answer, required facts and gold citations against the extracted PDFs; confirm unanswerable questions are truly unanswerable; run retrieval to see whether gold pages are reachable per preset.
- **Output:** `reports/phase-04b-eval-verification.md` listing each question as confirmed / discrepancy found (with page quote). **No silent edits** to the owner's questions; proposed corrections are listed for the owner to accept.

---

### P05 — Pipeline, Evidence Gate, Verifier, Safe Refusal (Day 2 Midday)

- **Files:** `src/relay/orchestrator/{__init__,tools,evidence_gate,prompt,verifier,pipeline}.py`, `tests/test_pipeline_and_verifier.py`.
- **Requirements:** single tool `retrieve_passages` via a validated, timed gateway; evidence gate with **max one** paper-scoped re-query; prompt with `[P1]..[Pk]` metadata and cite-or-refuse instruction; buffered generation recording `provider_first_token_ms`; verifier checks (a)–(e) of `ARCHITECTURE.md` §7.3; on failure deliver a **safe refusal with no second LLM call**; server spans recorded.
- **Acceptance criteria (ScriptedProvider, offline):** verified answer delivered; out-of-range tag, untagged claim, unsupported number and mixed refusal each blocked → safe refusal, `error_code=citation_verification_failed`, exactly one provider call; re-query loop runs at most once; clear out-of-corpus question → early refusal with 0 tokens and `provider_called=false`; tool timeout handled without crash; span invariant holds.

---

### P06 — ProviderGuard and Admission Queue (Day 2 Midday)

- **Files:** `src/relay/control/{__init__,provider_guard,retry,admission}.py`, `tests/test_control_plane.py`.
- **Requirements:** ProviderGuard as sole quota owner (semaphore, local RPM/TPM windows, header sync per verified semantics, local TPD, daily safety stop); token **reservation + settlement from provider usage** with adaptive `chars_per_token` (`ARCHITECTURE.md` §9.3); retry policy with full jitter, `retry-after`, deadline share; retry budget with on/off switch; minimal circuit breaker; degraded top-k when TPM headroom is tight. Admission queue with **capacity derived from service rate × patience quantile** (§15), predictive shedding, deadline and disconnect checks.
- **Acceptance criteria:** settlement test (estimate replaced by usage; missing usage keeps estimate and sets `usage_source=estimated`); header sync test that never treats request headers as RPM; daily-exhausted fast fail with 0 provider calls; retry budget throttling; capacity derivation test with explicit inputs; queue-full and predicted-wait shedding; disconnect before provider call spends 0 tokens.

---

### P07 — Model Comparison and Retrieval-Quality Evaluation (Day 2 PM, owner-approved real run)

- **Files:** `src/relay/experiments/{__init__,eval_runner}.py`, `scripts/run_model_comparison.py`, `scripts/run_retrieval_eval.py`, `tests/test_eval_runner.py`.
- **Requirements:** **isolated from caching and load testing** — cache off, sequential, paced by ProviderGuard, no concurrency; `--source real|stand-in`, `--max-real-calls`, daily safety fraction; writes `evidence/experiments/<run_id>/` with rows, traces, `answers_audit.json`, proposed grades. Model comparison first (~6 owner questions × ≤3 models, `balanced_rerank`), then the owner selects the model, then Table 3 (all owner questions × 3 presets).
- **Acceptance criteria:** offline test against ScriptedProvider/stand-in; real run counts recorded; owner grade review completed and stored before rows become canonical.

---

### P08 — Service: FastAPI, Exact Cache, SingleFlight (Day 2 Eve)

- **Files:** `src/relay/control/cache.py`, `src/relay/api/{__init__,app}.py`, `tests/test_cache_and_api.py`.
- **Requirements:** `create_app` wiring pipeline + guard + admission + cache + SingleFlight (one worker); `POST /ask` SSE with a final event carrying server spans and request metadata; disconnect propagation; daily-exhausted behaviour (cache hits served; misses get honest unavailability + passage pointers, 0 provider calls); server traces to the run directory; exact in-memory cache admitting only verified, non-degraded answers; cache reset per run.
- **Acceptance criteria:** 10 concurrent identical misses → 1 provider call, 9 coalesced rows with 0 tokens; one follower disconnecting does not cancel the leader; blocked answers never cached; degraded answers never cached; daily-exhausted path.

---

### P09 — Stand-In Service and Calibration (Day 3 Early AM)

- **Files:** `src/relay/standin/{__init__,profile,server}.py`, `scripts/calibrate_standin.py`, `tests/test_standin.py`.
- **Requirements:** per `ARCHITECTURE.md` §12; calibration reads P02-LIVE evidence and writes `evidence/standin_calibration/<run_id>/`; label `uncalibrated_defaults` if evidence missing; quantile comparison report (stand-in vs measured); documented "least like the real thing" list.
- **Acceptance criteria:** driven through `HttpOpenAIProvider`: SSE + usage; RPM/TPM/RPD 429 with `retry-after` and verified header semantics, then recovery; slowdown above knee; injected 5xx; slow stragglers.

---

### P10 — Load Harness and Scenarios (Day 3 AM)

- **Files:** `src/relay/experiments/{workloads,patience,load_runner}.py`, `scripts/run_load_tests.py`, `eval/load_questions.json` (pool built from owner questions plus paraphrase/duplicate mix — content reviewed by owner), `tests/test_load_harness.py`.
- **Requirements:** client-side timing; patience sampling and disconnect → `timeout`/`client_gave_up`; seeded workloads for **steady, burst, sustained overload, per-minute limit halved, daily budget exhausted** (all five), plus retry-amplification (budget on vs off) and burst with cache on vs off as separately labelled runs; one small owner-approved **real steady** run; each run in its own `evidence/experiments/<run_id>/`.
- **Acceptance criteria:** 10-request stand-in smoke run into `scratch/`; abandonment produces correctly classified rows; aggregation of smoke run passes validation.

---

### P11 — Tables, Forensics and Submission Audit (Day 3 Late AM)

- **Files:** `src/relay/experiments/tables.py`, `scripts/compute_tables.py`, `tests/test_tables.py`.
- **Requirements:** rebuild `results.csv` via `build_results.py` from canonical runs; compute Tables 1, 2a, 2b, 3 (linear percentiles in ms, state N and percentiles, one-decimal %, "not measured" when N = 0); slow-request decomposition (waiting/working/retrying, before vs after first token) from rows + traces; output `scratch/computed_tables.md`.
- **Audit:** full `pytest -v`, `ruff check .`, secret scan (key prefix, `Authorization`, org IDs) over tracked files and evidence, `git check-ignore` confirms evidence and `results.csv` are tracked, recomputation is byte-identical on a second run.

---

### Submission Buffer (Day 3 PM — owner-led)

Owner writes the 2-page write-up with the required headings, visits Vaani, exports the real conversation history (Claude Code transcripts plus any earlier chats used to produce the Phase 00 documents), and makes final commits. Claude assists only when asked.

---

## 3. Per-Phase Prompt Template

The owner may start each phase with:

```text
Execute <PHASE-ID> of The Relay strictly following CLAUDE.md.
Read CLAUDE.md, ASSIGNMENT.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md (<PHASE-ID>) and the latest ENGINEERING_LOG.md entries; inspect the repository; present a brief plan before writing files.
Implement only <PHASE-ID>. Real Groq calls: <none | approved up to N calls for: ...>.
Run pytest and ruff check . after the final edit, append to ENGINEERING_LOG.md, write reports/<phase-id>-<short-description>.md, report verified facts, and STOP.
```
