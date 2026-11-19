# The Relay v2 — Decision Log

Owner decisions for v2 work, newest last. Each entry records what was decided, by whom and when. Design detail lives in `docs/ARCHITECTURE_V2.md`; this file records the decisions only.

---

- **V2-00:** Architecture approved by owner on 2026-11-18. V2 builds on V1 at tag v1-submission.
- **V2-02:** Store tests use `:memory:`; only the restart test and the WAL check use a pytest temporary file (owner, 2026-11-18).
- **V2-03:** A suspected scan (more than 40 % of pages without extracted text) is **flagged, not rejected**; it is surfaced in `extraction_quality`. This supersedes 'reject scans clearly' in `docs/ARCHITECTURE_V2.md` §15 and the V2-03 plan. A PDF with no extractable text at all still fails ingestion, because there is nothing to index (owner, 2026-11-18).
- **V2-03:** `POST /ask` without a `collection_id` keeps the exact v1 path (the three v1 papers, request-chosen retrieval setting). The `v1-papers` collection is registered in the store and is queried by passing its `collection_id` (owner, 2026-11-18).
- **V2-03:** Migration 002 adds `request_log.request_id` and `collection.rerank` (owner, 2026-11-18).
