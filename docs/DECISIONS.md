# The Relay v2 — Decision Log

Owner decisions for v2 work, newest last. Each entry records what was decided, by whom and when. Design detail lives in `docs/ARCHITECTURE_V2.md`; this file records the decisions only.

---

- **V2-00:** Architecture approved by owner on 2026-11-18. V2 builds on V1 at tag v1-submission.
- **V2-02:** Store tests use `:memory:`; only the restart test and the WAL check use a pytest temporary file (owner, 2026-11-18).
- **V2-03:** A suspected scan (more than 40 % of pages without extracted text) is **flagged, not rejected**; it is surfaced in `extraction_quality`. This supersedes 'reject scans clearly' in `docs/ARCHITECTURE_V2.md` §15 and the V2-03 plan. A PDF with no extractable text at all still fails ingestion, because there is nothing to index (owner, 2026-11-18).
- **V2-03:** `POST /ask` without a `collection_id` keeps the exact v1 path (the three v1 papers, request-chosen retrieval setting). The `v1-papers` collection is registered in the store and is queried by passing its `collection_id` (owner, 2026-11-18).
- **V2-03:** Migration 002 adds `request_log.request_id` and `collection.rerank` (owner, 2026-11-18).
- **V2-04:** The cue set holds, per document:
  - `title`, `short_names` and the arXiv id/version;
  - optional **aliases** (extra strong names, for example "kyutai") and **topics** (weak cues, for example "speech").

  The v1 values move out of the scope, gate and verifier modules into one v1 corpus definition. That definition serves the `v1-papers` collection and the v1 `/ask` path, so v1 behaviour stays identical (owner, 2026-11-19).
- **V2-04:** Cues are stored per document in a new nullable JSON column, `document.cues` (**migration 003**). It is written at ingestion (derived) and at `v1-papers` registration (the curated v1 cues). Rows without cues fall back to derivation (owner, 2026-11-19).
- **V2-04:** Isolation at the verifier uses a **foreign-name check**. The verifier also receives the names of the tenant's documents that are not in the collection, and blocks a sentence naming one of them as `paper_mismatch` (owner, 2026-11-19).
- **V2-04:** The system prompt and the refusal messages are **rendered from the cue set**:
  - The v1 cue set renders byte-identical v1 text; prompt version `p05-1` is unchanged.
  - Other collections name their own documents, under a new collection prompt version (owner, 2026-11-19).
- **V2-04 [PROPOSED, pending owner approval]:** In `DocumentCues`, the approved aliases and topics are stored as pattern lists:
  - `strong` holds the title, short names, aliases and arXiv id, as regular expressions in v1's own order;
  - `weak` holds the topics.

  Plain `title`, `short_names` and arXiv id fields sit alongside them. The reason is exact v1 pattern order: v1 interleaves the arXiv id among H-Net's aliases, and the order appears in recorded scope reasons. Stored patterns are validated (they must compile) and refreshed for v1 papers at start-up. A foreign document is named only by its title and short names, never by its aliases or topics. (Proposed by Claude Code, 2026-11-19.)
- **V2-05:** The partial-answer rate on the v1 questions is measured by **replaying the 63 recorded Table 3 answers** through the streaming path (splitter, per-sentence verifier, final pass). There are no real calls; the stand-in's rate is reported separately (owner, 2026-11-19).
- **V2-05:** Property tests use a **seeded random generator, with no new dependency** (Hypothesis is not added) (owner, 2026-11-19).
- **V2-05:** `POST /v1/ask` without a `collection_id` answers from the caller's **`v1-papers` collection** (owner, 2026-11-19).
- **V2-06:** The web UI is **React + Vite + TypeScript in `web/`**. It is built to static files and served by the API on the same origin (D10). The phase prompt fixed these details (owner, 2026-11-19):
  - **Node is a build-time tool only.** `relay ui build` runs `npm run build` and copies `web/dist/` to `src/relay/app/static/`. No Node dependency is added to `pyproject.toml`.
  - **Frontend tests use Jest + React Testing Library.**
  - **The Bearer key is read at build time from `VITE_API_KEY`.** Until V2-10 the API accepts any key for the development tenant, so the default `relay-dev-key` is not a secret.
- **V2-06 [PROPOSED, pending owner approval]:** Rulings made while building the UI (Claude Code, 2026-11-19):
  - **The "rerank" preset option sends `retrieval_preset: "balanced_rerank"`**, the backend's reranking preset.
  - **The per-answer time bar is estimated in the browser** from when each `status` event arrived, and it says so. `final` does not yet carry a span breakdown (ARCHITECTURE_V2 §11). The UI uses `final.spans` as soon as the server sends it.
  - **The `sources` event gains `short_name` and the full passage `text`.** `text_excerpt` is unchanged. Citation chips need the paper's short name, and the source panel highlights within the whole passage. Cached answers still carry no passage text; the panel says so.
  - **`GET /v1/collections` gains `document_count`.** It counts documents of any status, failed ones included.
  - **The Status page derives "metadata store" from `/healthz`** (`trace_sink` present means SQLite with the request log writing). `/healthz` has no separate store field.
  - **The source panel shows the cited passage text.** Opening the PDF at the cited page (V2-06 plan, task 3) is not built: there is no endpoint that serves PDFs.
  - **The built `src/relay/app/static/` is neither gitignored nor committed.** Whether to commit the build output is the owner's decision.
- **V2-07: L4 model.** The candidates are `cross-encoder/nli-MiniLM2-L6-H768` (Apache-2.0; full and int8 ONNX) and, second, `cross-encoder/nli-deberta-v3-xsmall` (Apache-2.0, ONNX). `typeform/distilbert-base-uncased-mnli` is replaced: it has no ONNX export and no licence, and converting it would need PyTorch. The models run on the onnxruntime and tokenizers already installed through fastembed: no new Python dependency and no sentence-transformers or PyTorch. They are downloaded into `data/models` (owner, 2026-11-20).
- **V2-07: L4 calibration labels.** The v1 grades are per answer, rubric-proposed and unreviewed. So the calibration table is computed against **per-sentence labels proposed by Claude Code** ("supported / not supported by its cited passage"), marked as unreviewed proposals. The owner reviews them before any blocking threshold is set (owner, 2026-11-20).
- **V2-07: L3 in streaming.** A refusal-shaped first sentence is **held, and decided at the end**, as the INSUFFICIENT_EVIDENCE marker is. If only refusal-shaped sentences (or nothing) follow, the answer is a verified refusal and is cached. If a real claim follows, the answer stops at once and is refused, not cached. The user sees the collection's refusal message, not the model's sentence. After verified sentences, a refusal-shaped sentence stops the answer as partial (owner, 2026-11-20).
- **V2-07: L3 in the whole-answer (buffered) verifier, including v1's `POST /ask`.** A refusal-shaped sentence among claims makes the answer **BLOCKED**, as v1 treats any failed sentence (buffered mode has no partials). An answer made only of refusal-shaped sentences is `REFUSAL_VERIFIED`. The Table 3 verdicts this changes are intended changes (owner, 2026-11-20).

- **V2-13: shared state and multi-worker, completed** (implementation commit 95fdf8e;
  owner, 2026-11-21).
  - A `StateStore` (`src/relay/control/state.py`) shares the answer cache, `/v1/ask`
    SingleFlight coalescing and each lane's quota counters across workers.
    - The default `[state] backend = "memory"` needs no Redis and keeps the in-process
      objects unchanged.
    - `backend = "redis"` uses Lua scripts for the sliding window and the flight lock.
    - Followers replay a per-flight event log; Pub/Sub only wakes them.
  - A dead leader's followers end with `internal_error` and `shed_reason: leader_lost`
    once its lock expires; there is no takeover.
  - The cache and coalescing fail open; the shared quota fails closed.
  - `redis` and `fakeredis` are approved dependencies (CLAUDE.md section 4.1), as is Docker
    for packaging.
- **v2 release (owner, 2026-11-21).** With V2-13 done, every phase of
  `docs/IMPLEMENTATION_PLAN_V2.md` is implemented. Work moves to release packaging:
  - the `Dockerfile` and `compose.yaml` (relay + Redis + stand-in);
  - `relay doctor` checking the state backend;
  - the README quickstarts;
  - the final verification before the `v2.0.0` tag.

  V2-14 remains as recorded: overload and halved-TPM deliver less than v1, with the cause
  explained in `reports/phase-v2-14-load-revalidation.md`. The owner-review items of V2-12
  (the 80 v1 answers; 100 owner-authored questions) remain the owner's.
- **F-01 (owner, 2026-11-22):** the stand-in splits passages with the Relay's own
  `split_sentences`, and the CI baseline `eval/baselines/standin-replay-small_fast.json` was
  regenerated (grounded 0.8, refusal precision 0.0; the old 0.1667 came from the bug).
- **F-02c (owner, 2026-11-22): L4 runs off the critical path.**
  - `[verifier] l4_mode = "async" | "sync" | "disabled"`, default `"async"`: a sentence is sent
    as soon as it passes L1–L3 and carries `confidence: null`.
  - Its score arrives in `final.confidences` (in sentence order) and the request log.
  - Scores are collected after the queue slot is released, one model call at a time per
    process.
  - This changes the V2-07 `sentence` event contract (`confidence` is now set only in
    `"sync"`). The web UI does not read it.
- **F-02c (owner, 2026-11-22): admission is work-conserving within a tenant.**
  - Being over rate ranks a request lower only while another tenant is waiting. It then
    covers the tenant's backlog at that priority from its first over-rate request on.
  - A request never evicts or overtakes a waiting request of its own tenant and priority; a
    lone tenant gets a plain FIFO queue.
  - Priority and the cross-tenant protection of V2-10 are unchanged.
- **F-04 (owner, 2026-11-22): call spacing lives in ProviderGuard, not in the provider client.**
  - New `GuardConfig.min_interval_ms` (`[guard]` or per lane), default 0.
  - The Groq example lane suggests 250 ms, a cautious owner-chosen value; the real limit was not
    measured.
  - Spacing is per process; with the Redis state store, each worker spaces its own calls.
- **F-series closed and v2.0.0 release candidate (owner, 2026-11-22).**
  - **F-01:** the stand-in now splits passages like the verifier. 15 false uncited stops over 82
    replayed prompts became 0; the overload run's 7 verifier stops became 0.
  - **F-02 / F-02b:** 5-run overload comparisons. The remaining gap came from inline L4 (~130 ms
    per request) and single-tenant self-eviction (~1 answer per run), not from per-sentence
    checking.
  - **F-02c:** L4 async and work-conserving admission. Overload: 57.0 +/- 1.0 delivered against
    v1's 57.2 +/- 0.4 (within run-to-run noise); first verified sentence 489 against 620 ms (fast
    requests). Runs: `runs/f02c-overload-20261122T164234Z`.
  - **F-03:** real Groq (10 calls). No refund of an aborted stream within about 1.6 s; a partial
    hold (213-355 of 443 tokens); headers are not an exact per-request ledger; 1 accidental 429.
    ProviderGuard's header sync stays the authority.
  - **F-04:** call spacing (`min_interval_ms`) in ProviderGuard. Commenter edge cases (cross-tenant
    cache, version change and deletion mid-stream, key revoked mid-flight) are covered by tests;
    no product change was needed.
  - **Next:** the repository is the release candidate for `v2.0.0`. Tagging and pushing are the
    owner's.
- **F-07 (pending owner review, 2026-11-25): stress test with real verifier stops; judge tested on fixed sets.**
  - **Stand-in:** new `faults.uncited_rate` (default 0): the share of answers in which one
    sentence loses its citation tag. It is test tooling only and changes nothing at 0. The
    stand-in also answers a client that left before its body arrived with 499 instead of
    raising `ClientDisconnect` out of the app.
  - **Overload, 5 paired runs per rate** (`runs/f07-fault-r*-20261125T*`, summary
    `runs/f07-summary-*.md`). v2 − v1 delivered:
    - 0 %: −0.2 (95 % CI −1.8 to +1.4), replicating F-02c on other seeds;
    - ~1 % effective: +0.2 (−1.4 to +1.8);
    - ~6 % effective: −0.8 (−1.8 to +0.2);
    - ~27 % effective: −2.6 (−7.3 to +2.1); per run −3, 0, −9, 0, −1.
  - **Mechanism at the high rate:** cancelled streams (7.6 per run) hold their reservation, and
    the predicted-wait refusals rise (up to 22 against ~10). The stand-in holds the full
    reservation on a cancel; Groq held about 50–80 % in F-03, so this overstates Groq's cost.
  - **The judge's fixed sets** (thresholds saved first; sentences from the owner's question set):
    - A, supported quotes: 39/40 kept;
    - B, forbidden claims next to the right passage: 33/40 blocked (L1–L2 pass 34/40);
    - C, the quote next to a different passage: 39/40 blocked.
    The 7 B claims that pass L2 and L4 are role or direction swaps. L4 stays advisory.
  - **Correction to F-03:** the aborted calls' drops were 216.5–354.6 tokens (raw
    `analysis.json`), so "213–355" in the F-03 entry and report should read **217–355**.
- **F-07 addendum (pending owner review, 2026-11-25): the judge on audited labels.** On the
  owner's blind review (113 sentences), the L4 false-block rate at 0.5 is about 26 % (published
  as 21.7 % against the unreviewed AI labels), and it catches only about 3 of the ~11 truly
  unsupported sentences in 224. L4 stays advisory: no blocking threshold.
- **F-07 complete (pending owner review, 2026-11-25): the owner's grades of the 62 Table 3 answers.**
  36 correct, 8 partial, 0 incorrect of 44 answered; 1 wrong refusal; 17/17 correct refusals. The
  rubric's literal proposal (19/45) was a large undercount. The grades are kept in `scratch/`,
  because `evidence/` is frozen.
