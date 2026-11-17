# The Relay v2 — Implementation Plan

> **Status:** `PROPOSED` — awaiting owner approval. No phase has started.
> **Date:** 2026-11-17
> **Design:** `docs/ARCHITECTURE_V2.md` (section references such as §6 point there).
> **Baseline:** v1 at commit `fc3c6bc`; 467 tests passing; `results.csv` and `evidence/` frozen.

---

## 1. How to work through this plan

- **One phase at a time.** Each phase follows the existing `CLAUDE.md` procedure: read, inspect, plan, implement, test, log, report, stop.
- **Every phase ends with the v1 suite still green.** v2 adds to the system; it does not break the assignment evidence.
- **Real Groq calls are rare.** Most phases need none. Where one does, the cap is stated and needs owner approval before the run.
- **Each phase has a demo.** If the demo does not work, the phase is not done.

**Effort** is a rough estimate in focused working days for one person.

## 2. Phase map

| Phase | Name | Depends on | Effort (days) | Real calls |
| :-- | :-- | :-- | :-- | :-- |
| V2-00 | Governance and baseline | — | 0.5 | 0 |
| V2-01 | Runnable service | V2-00 | 2 | 0 |
| V2-02 | Metadata store | V2-01 | 2–3 | 0 |
| V2-03 | Collections and ingestion | V2-02 | 4–5 | 0 |
| V2-04 | Collection-aware scope, gate and verifier | V2-03 | 2–3 | ≤ 10 |
| V2-05 | Verified streaming | V2-04 | 4–6 | ≤ 15 |
| V2-06 | Web UI | V2-05 | 4–6 | 0 |
| V2-07 | Verifier v2 (refusal form and entailment) | V2-05 | 4–5 | ≤ 30 |
| V2-08 | Conversations and query rewriting | V2-05 | 3 | ≤ 15 |
| V2-09 | Provider router and cost tracking | V2-05 | 3–4 | ≤ 20 |
| V2-10 | Tenants, API keys and fair admission | V2-02 | 3 | 0 |
| V2-11 | Observability | V2-05 | 2–3 | 0 |
| V2-12 | Evaluation system and CI gate | V2-07 | 4 | ≤ 60 |
| V2-13 | Shared state and multi-worker (optional) | V2-10 | 4–5 | 0 |
| V2-14 | Load re-validation, demo and launch | all chosen | 3–4 | ≤ 20 |

**MVP cut:** V2-00 to V2-06, plus V2-14. That is roughly 20–27 working days and gives a product a stranger can run: upload papers, ask, and watch verified sentences stream with sources.

**Second release:** V2-07 to V2-12, for quality, reliability and credibility.

**Optional:** V2-13, only if multi-worker deployment is wanted.

---

## 3. Phases

### V2-00 — Governance and baseline

**Objective:** make v2 work legitimate under the project's own rules.

**Tasks**
1. Owner approves `docs/ARCHITECTURE_V2.md` or lists changes.
2. Update `CLAUDE.md`: point the source-of-truth hierarchy at the v2 documents for v2 work, and list the newly allowed dependencies.
3. Owner tags the submitted state in git (for example `v1-submission`), so v1 can always be recovered.
4. Add a `docs/DECISIONS.md` log for v2 decisions.

**Done when:** the approved documents are committed by the owner and the tag exists.

### V2-01 — Runnable service

**Objective:** one command starts the service.

**Files:** `src/relay/app/settings.py`, `src/relay/app/compose.py` (the composition root), `src/relay/cli.py`, `relay.example.toml`, `Dockerfile`, `compose.yaml`, tests.

**Tasks**
1. A typed settings file (`relay.toml` plus environment overrides): provider lanes, guard limits, queue inputs, paths, cache mode.
2. A composition root that builds retrievers, provider, guard, queue and the app from settings. This is the code the v1 load script does by hand.
3. `relay serve`, `relay ingest`, `relay doctor` (checks models, indexes, key presence without printing it).
4. A container image and a compose file that starts the service with the stand-in.
5. A README quickstart.

**Done when**
- `relay serve` answers `POST /ask` for the three v1 papers.
- `docker compose up` works on a clean machine with no key, using the stand-in.
- `relay doctor` reports a missing key or index clearly.
- The v1 suite and the load harness smoke run still pass.

**Demo:** clone, one command, one answered question.

### V2-02 — Metadata store

**Objective:** durable state behind a repository interface (§5).

**Files:** `src/relay/store/` (`schema.sql`, `migrations.py`, `repositories.py`), tests.

**Tasks**
1. SQLite in WAL mode, with a small versioned migration runner.
2. Repositories for tenants, API keys, collections, documents, chunks, ingest jobs, conversations and the request log.
3. All database work off the event loop.
4. A request-log writer fed by the existing `final` metadata.

**Done when**
- Migrations apply from empty and are idempotent.
- A restart keeps collections, documents and the request log.
- Repository tests run against a temporary database with no network.

**Demo:** restart the service; history is still there.

### V2-03 — Collections and ingestion

**Objective:** any PDF, in any collection, searchable within about a minute.

**Files:** `src/relay/ingestion/` (generalised), `src/relay/app/jobs.py`, `src/relay/retrieval/registry.py`, API routes, tests.

**Tasks**
1. Remove the hard-coded paper list. Document identity comes from the store.
2. Upload endpoint and arXiv-by-version endpoint, with size, page and type limits and SHA-256 de-duplication.
3. A background job loop with states (queued, parsing, embedding, indexing, ready, failed), retry, and a restart-safe jobs table.
4. Per-collection index files, incremental rebuild, and a collection version bump on every change.
5. An index registry that loads collections lazily and evicts the least recently used.
6. An extraction-quality report per document (pages with no text, suspected scans), surfaced in the status.

**Done when**
- The three v1 papers ingested through the new path produce the same chunk hashes as v1 (a regression check on the parser).
- A new PDF becomes queryable without a restart.
- Two collections never return each other's passages.
- A scanned PDF is rejected with a clear reason.
- A crash mid-ingestion resumes on restart.

**Demo:** upload a paper not in v1 and get a cited answer from it.

### V2-04 — Collection-aware scope, gate and verifier

**Objective:** the v1 safety logic works for any collection.

**Tasks**
1. Derive scope cues (titles, short names, identifiers) per document at ingestion and store them.
2. Make the scope analyser, evidence gate and verifier take the collection's cue set instead of the three-paper constants.
3. Re-run the v1 evaluation questions through the generalised path.

**Done when**
- All v1 pipeline and verifier tests pass against the generalised code.
- On the v1 Table 3 questions, the verdict of every row matches the v1 run, or each difference is explained. (Real calls ≤ 10 for spot checks; the rest uses recorded answers.)

**Demo:** the same unanswerable questions are refused in a new collection.

### V2-05 — Verified streaming

**Objective:** the user sees status at once and sentences as they verify (§6).

**Files:** `src/relay/orchestrator/streaming.py`, `verifier.py` (incremental mode), `api/` (new `/v1/ask`), tests, a harness scenario.

**Tasks**
1. An incremental sentence splitter that works on a token stream and knows when a sentence is complete.
2. An incremental verifier: check one sentence against its cited passages; keep the v1 whole-answer check as the final pass.
3. The event protocol: `accepted`, `status`, `sources`, `sentence`, `stopped`, `final`.
4. Cancel generation when a sentence fails; mark the answer partial; never cache a partial answer.
5. Queue position and expected wait in `accepted` and `status`.
6. Coalesced followers receive the same sentence stream as the leader.
7. A per-collection switch back to buffered mode.
8. Extend the load harness to record first-status and first-sentence times.

**Done when**
- No event ever carries a sentence that fails the verifier (a property test over scripted streams, including failures mid-answer).
- On the steady stand-in scenario, median first-sentence time is under half the v1 median total time.
- The partial-answer rate is measured and reported on the v1 questions (real calls ≤ 15).
- Disconnect and coalescing tests from v1 still pass on the new endpoint.

**Demo:** a question whose first sentence appears while the rest is still being written.

### V2-06 — Web UI

**Objective:** a person can use the product without reading API docs.

**Files:** `web/` (React + Vite + TypeScript), static serving from the API.

**Tasks**
1. Collections page: create, upload, ingestion progress.
2. Chat page: streamed sentences, each with citation chips.
3. Source panel: open the PDF at the cited page and highlight the passage.
4. A per-answer time bar: queue, retrieval, model, verification.
5. Honest states: queued with position, partial answer, refusal, unavailable with passage pointers.
6. A status page: quota headroom, queue depth, circuit state.

**Done when**
- The full flow works in a browser against the stand-in with no key.
- Every non-`ok` status from §6 has a distinct, readable state.
- It works at phone width and with keyboard only.

**Demo:** the 60-second video for the README.

### V2-07 — Verifier v2

**Objective:** close the known gap and add a semantic check (§6, L3 and L4).

**Tasks**
1. L3: detect refusal-shaped sentences ("the passages do not state…") and treat them as refusals.
2. Spike: pick a small ONNX entailment model; measure CPU latency per sentence and accuracy on the v1 graded answers.
3. L4 in advisory mode: score every sentence, log it, show a confidence mark.
4. Calibrate a blocking threshold from owner-reviewed grades; report false-block and false-pass rates.

**Done when**
- The v1 qwen q21 case (cited "does not state" sentence) is classified as a refusal.
- L4 adds less than an agreed latency per sentence (proposed: 150 ms at the median on the development machine).
- The calibration report exists, and blocking is enabled only if the owner accepts its numbers.

**Demo:** an answer with a plausible but unsupported sentence is stopped.

### V2-08 — Conversations and query rewriting

**Objective:** follow-up questions work.

**Tasks**
1. Conversation and message storage.
2. Standalone-question rewriting for follow-ups, with a small token budget, skipped for first questions.
3. Cache and coalescing keyed on the rewritten question.
4. An evaluation set of follow-up pairs written by the owner.

**Done when**
- "And what about its latency?" after a Moshi question retrieves Moshi passages.
- First questions cost no extra model call.
- Rewrite failures fall back to the original question.

### V2-09 — Provider router and cost tracking

**Objective:** one model's limit or weakness no longer decides the outcome (§7).

**Tasks**
1. A lane abstraction: provider, model, guard, token budget, reasoning settings.
2. Routing rules: primary, fallback on long quota wait or open circuit, escalation on truncation, honest unavailability last.
3. A pricing table per lane and `cost_usd` on every row.
4. Stand-in scenarios: primary lane exhausted, primary lane failing, truncating model.

**Done when**
- With the primary lane's daily budget exhausted, new questions are served by the second lane.
- A truncated answer triggers exactly one escalation, inside the retry budget.
- Token and cost sums across lanes reconcile with provider usage.

### V2-10 — Tenants, API keys and fair admission

**Objective:** several users can share one deployment safely (§7, §12).

**Tasks**
1. Hashed API keys, tenant scoping on every route, key management commands.
2. A token bucket per tenant and two priority classes in the admission queue.
3. Capacity derived from a moving average of measured tokens per answer.
4. Collection deletion that removes files, index, cache and logs.

**Done when**
- A tenant cannot read another tenant's collections, requests or cache entries.
- In a two-tenant overload scenario, the light tenant's delivery rate stays above an agreed floor while the heavy tenant is shed.

### V2-11 — Observability

**Objective:** the operator can see what the service is doing (§11).

**Tasks**
1. Prometheus metrics at `/metrics`.
2. OpenTelemetry spans aligned with waiting, working and retrying.
3. `GET /v1/requests/{id}` returning the time breakdown and trace.
4. Sanitised error categories for transport failures.
5. A starter dashboard definition.

**Done when**
- The overload scenario is readable from the dashboard alone: queue depth, shed rate, quota headroom, first-sentence latency.
- No metric or span contains a secret or question text beyond a hash.

### V2-12 — Evaluation system and CI gate

**Objective:** quality claims that hold up (§10).

**Tasks**
1. Eval sets, questions and grades in the store, with import from the v1 JSON.
2. A review screen in the UI for the owner's grading, replacing the command-line review.
3. Three graders reported side by side: rubric, entailment, owner.
4. A CI workflow: unit tests, lint, the stand-in pipeline run, and a comparison against the accepted baseline.
5. A budgeted real evaluation command for release checks.

**Done when**
- The 80 v1 proposed grades are reviewed in the new screen.
- A deliberately broken prompt fails the CI gate.
- The owner has at least 100 reviewed questions across at least three collections (owner-authored).

### V2-13 — Shared state and multi-worker (optional)

**Objective:** run more than one worker (§13).

**Tasks**
1. A `StateStore` interface; the in-memory implementation is today's behaviour.
2. A Redis implementation for the cache, coalescing (lock plus notification) and quota counters.
3. Harness runs with two workers and with a worker killed mid-request.

**Done when**
- Ten identical concurrent requests across two workers still make one provider call.
- Quota accounting stays within the provider's limit with two workers.
- Killing a worker loses no accepted request silently.

### V2-14 — Load re-validation, demo and launch

**Objective:** prove v2 is at least as robust as v1, and ship it.

**Tasks**
1. Re-run the v1 scenarios (steady, burst, overload, halved limit, daily exhaustion, retry amplification) against v2, and add first-sentence latency.
2. A before-and-after table against the v1 numbers.
3. A public demo deployment with the stand-in as the fallback lane.
4. Launch material: README with the architecture diagram and measured numbers, the demo video, and a short write-up of the hardest problem.

**Done when**
- Every v1 scenario's delivery rate is at least the v1 figure, or the regression is explained.
- The demo stays usable after the free daily quota is spent.
- A new user reaches a first answer in under ten minutes from the README.

---

## 4. Cross-cutting rules for every phase

1. **Tests offline and deterministic**, as in v1. New network behaviour is tested against the stand-in.
2. **No secret in logs, traces, metrics or evidence**; the v1 scan runs in every phase.
3. **v1 evidence is read-only.** New measurements go into new run directories.
4. **A phase that changes a number users will see** (latency, delivery rate, quality) ends with a harness run that measures it.
5. **Question content stays the owner's.** Tooling and review screens are built; questions and grades are not written on the owner's behalf.

## 5. What the owner needs to decide before V2-01

1. Approve the architecture and the new dependencies.
2. Choose the MVP cut or a different one.
3. Name the second model lane (needed by V2-09, but it shapes V2-01's settings).
4. Confirm the per-phase real-call caps in §2, or set others.

## 6. Recommended order if time is short

1. V2-00, V2-01 — runnable (2.5 days).
2. V2-02, V2-03, V2-04 — any documents (8–11 days).
3. V2-05 — verified streaming (4–6 days).
4. V2-06, V2-14 — interface and launch (7–10 days).

Stop there and ship. Everything after that improves a product that already exists.
