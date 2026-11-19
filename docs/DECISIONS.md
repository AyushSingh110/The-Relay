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
