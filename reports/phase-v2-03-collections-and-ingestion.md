# Phase V2-03 Implementation Report — Collections and Ingestion

> **Phase ID:** `v2-03`
> **Date:** `2026-11-19 09:08 IST`
> **Status:** `Completed`.
> - Verified in Conda env `the-relay` (Python 3.12.16): **620 passed**, 0 failed, 0 skipped (546 before the phase, 74 new); `ruff check .` clean.
> - **Real Groq calls: 0.** `evidence/` and `results.csv` are unchanged.
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

From the owner's prompt (plan V2-03; `docs/ARCHITECTURE_V2.md` §§5, 9, 12):

**Collections and documents**
- Collections with `/v1` routes:
  - create and list collections;
  - upload a PDF, or submit an exact arXiv version;
  - document status.
- Upload limits: PDF only, 50 MB (413 before any row is created), 500 pages.
- SHA-256 de-duplication.

**Ingestion**
- A background ingest worker: queued → parsing → embedding → indexing → ready.
- On failure: `failed`, the class name only, and at most 3 attempts.
- Per-collection indexes with a version bump, plus an LRU index registry (8 collections loaded).
- An extraction-quality report per document (suspected scan: more than 40 % empty pages, flagged, not rejected).

**Pipeline and compatibility**
- The pipeline takes `collection_id`, with scope cues from the store.
- Migration 002.
- V1 compatibility:
  - `v1-papers` is auto-registered from the existing v1 index files;
  - `/ask` without a `collection_id` is unchanged.

**Done when:**
- the v1 chunk hashes are identical through the new path;
- a new PDF is queryable without a restart;
- collections are isolated;
- a crash mid-ingest resumes;
- all tests pass; ruff is clean; no Groq calls; `evidence/` and `results.csv` are unchanged.

## 2. Implementation Summary

**Store**
- **Migration 002:** `request_log.request_id`, `collection.rerank`, and two indexes.
- **New repository methods:**
  - `CollectionRepo.find_by_name`.
  - `DocumentRepo.find_by_sha256` / `update_status`.
  - Two single-transaction methods: `create_queued` (a document plus its job) and `requeue`.
  - `IngestJobRepo.list_runnable` / `latest_for_document`.
  - `complete`: document ready, job ready and version bump in one transaction.

**Ingestion**
- **DocumentRegistry** (`ingestion/registry.py`). It derives each collection's scope cues from its `ready` documents:
  - the title;
  - the short name before a colon;
  - the arXiv id, with its version pinned.

  It also derives the collection's retrieval setting (preset, plus rerank when the collection sets it). Generic titles such as "paper" are not cues.
- **IngestWorker** (`ingestion/worker.py`).
  - Polls every 2 s and processes one job at a time.
  - Uses the v1 parser, chunker and `HybridIndex` unchanged; all CPU work runs in threads.
  - The new document's chunks are merged into the collection index, ordered by `paper_id`. This reproduces the v1 chunk-set hashes.
  - The index swap is atomic: a new build directory, then `os.replace` on `index_ref.json`.
  - Failures record the class name and are retried at most 3 times, at the poll interval.
  - A job found half-done after a crash resumes, and counts as one failed attempt (`Interrupted`).

**Retrieval**
- **IndexRegistry** (`retrieval/registry.py`).
  - Loads lazily and reloads when the collection version changes.
  - Evicts the least recently used collection above 8.
  - `get` raises `CollectionNotReadyError`.
  - Lazy embedding and rerank models are loaded before the timed retrieval call.

**Pipeline and service**
- **Pipeline** (`orchestrator/pipeline.py`). `PipelineRequest.collection_id`: when it is set, the pipeline uses that collection's retriever, top-k, vocabulary, cues and valid paper filters. When it is `None`, the v1 path is unchanged.
- **`/ask`** (`api/app.py`). `collection_id` is optional; the collection's preset sets the retrieval setting.
  - The answer cache and SingleFlight key is `setting@collection:vVERSION`, so collections never share answers and a new document invalidates older ones.
  - 404 `collection_not_found`, 409 `collection_not_ready`, 422 `set_by_collection` / `collections_disabled`.

**`/v1` routes** (`app/routes/ingestion.py`)
- `POST` / `GET /v1/collections`.
- `POST /v1/collections/{id}/documents`, taking multipart `file` or JSON `{"arxiv_id": "2410.00037v1"}`.
- `GET /v1/documents/{id}`, which returns the document, its job and `extraction_quality`.
- The API key is **stubbed**: any Bearer key maps to tenant `dev`. A missing key is 401, a foreign tenant is 403, and another tenant's objects are 404.

**Start-up**
- **Compose** (sqlite mode):
  - dev tenant;
  - `v1-papers` (id `v1-papers`, preset `small_fast`; its three documents are `ready`, and its `index_ref.json` points at the existing `data/indexes/small_fast`);
  - IndexRegistry, IngestWorker start and stop, `/v1` routes.
- **Settings:** `paths.uploads_dir`, `paths.collections_dir`, and an `[ingestion]` section.

## 3. Files Created

**Source**
- `src/relay/ingestion/registry.py`
- `src/relay/ingestion/worker.py`
- `src/relay/retrieval/registry.py`
- `src/relay/app/collections.py` (resolver, lazy models)
- `src/relay/app/bootstrap.py` (dev tenant, `v1-papers`)
- `src/relay/app/routes/__init__.py`
- `src/relay/app/routes/ingestion.py`

**Tests**

| File | Tests |
| :-- | --: |
| `tests/test_store_migration_002.py` | 5 |
| `tests/test_scope_cues.py` | 6 |
| `tests/test_document_registry.py` | 4 |
| `tests/test_index_registry.py` | 3 |
| `tests/test_ingest_worker.py` | 11 |
| `tests/test_pipeline_collections.py` | 3 |
| `tests/test_v1_bootstrap.py` | 5 |
| `tests/test_ingestion_routes.py` | 13 |
| `tests/test_ask_collections.py` | 4 |
| `tests/test_compose_collections.py` | 3 |
| `tests/test_v2_03_review_fixes.py` | 17 |

**Report:** this report.

## 4. Files Modified

**Source and configuration**
- `src/relay/store/migrations.py` and `src/relay/store/repositories.py` (see §2).
- `src/relay/orchestrator/pipeline.py`: the collection path.
- `src/relay/orchestrator/tools.py`: `ToolGateway(paper_ids=…)`, `with_retrievers`.
- `src/relay/retrieval/scope.py`: `ScopeCues`, with `analyze_scope(..., cues=)` defaulting to the v1 cues.
- `src/relay/api/app.py`: `collection_id`, `AskTarget`, `CollectionDirectory`, error mapping.
- `src/relay/app/compose.py`, `src/relay/app/settings.py`, `src/relay/app/request_log.py` (`request_id` and `collection_id` columns).
- `relay.example.toml`, `pyproject.toml` (`python-multipart>=0.0.9`), `.gitignore` (`data/uploads/`, `data/collections/`).
- `docs/DECISIONS.md`: the three owner decisions from this phase.

**Tests**
- `tests/test_store.py`, `tests/test_store_migrations.py`: schema-version assertions made version-agnostic (`LATEST_VERSION`).
- `tests/test_store_integration.py`, `tests/test_v2_02_review_fixes.py`: sqlite-mode helpers now put the upload and collection directories in `tmp_path`, so tests never write to `data/`.

**Log:** `ENGINEERING_LOG.md`.

## 5. Technical Decisions

**Owner decisions (2026-11-18, recorded in `docs/DECISIONS.md`):**
- Suspected scans are flagged, not rejected. This supersedes "reject scans" in the plan; a PDF with no text at all still fails with `NoExtractableTextError`.
- `/ask` without a `collection_id` keeps the exact v1 path.
- Migration 002 as specified.

**My rulings ([PROPOSED]; each says what it costs if wrong):**

1. **Collection indexes live in `paths.collections_dir` (`data/collections/`), not in `data/indexes/`.** The v1 index directory is only ever read.
   - Cost if wrong: one more configured path.
2. **`v1-papers` has a fixed id `v1-papers` and one preset (`small_fast`).** It is registered only when the store has no collections. Its documents are `ready` with `ready` jobs, and a restart never points it back at the v1 index once uploads have been merged into it.
   - Cost if wrong: `balanced_rerank` / `large_wide` are not available as collections for the v1 papers. They still are through the v1 `/ask` path.
3. **The index swap is atomic.** Each rebuild writes `builds/<uuid>/`, then replaces `index_ref.json` with `os.replace`. The replaced build is kept (a reader may be loading it); older builds are deleted.
   - Cost if wrong: one extra index copy on disk per collection.
4. **`attempts` counts failed tries.** An interrupted job is counted as a failure when it is picked up again (`error_class="Interrupted"`), so a PDF that crashes the process cannot crash-loop the service.
   - Cost if wrong: a job interrupted once and then successful shows `attempts = 1`.
5. **Uploads go to `{uploads_dir}/{sha256}.pdf`, content-addressed.**
   - The title is the v1 manifest title for v1 arXiv papers, otherwise the PDF metadata title, otherwise the file name.
   - The extraction report is a JSON file per document (the schema has no column for it).
6. **Upload responses:**
   - 202 for a new document.
   - 200 `{deduplicated: true}` for the same bytes in the same collection.
   - Re-adding a **failed** document re-queues it: 202 `{requeued: true}`, with a fresh job.
   - A multipart upload without `Content-Length` is 411. Otherwise the form parser would spool any size to disk.
7. **arXiv by id needs an exact version.**
   - The PDF is taken from the local papers directory when present; otherwise it is downloaded, capped at 60 MB.
   - The first page must carry `arXiv:<id>v<n>` (422 otherwise).
   - The download goes to arxiv.org: it is the feature's purpose, but it is network access the service did not do before.
8. **`/v1` routes and collections exist only in `cache.mode = "sqlite"`.** In memory mode, `/ask` with a `collection_id` is 422 `collections_disabled`.
9. **The API key is stubbed** (any Bearer key maps to tenant `dev`) until V2-05.
10. **Collection rerank can only switch reranking on** (a preset that already reranks keeps it).

## 6. Commands Actually Executed

```text
python -m ruff check .                         # after each file; last: All checks passed!
python -m pytest -q -p no:cacheprovider        # after each file (counts in §7)
python -m pytest tests/<new file> -q           # each new test file first, RED before code
python -m relay.cli serve   (RELAY_CACHE__MODE=sqlite, RELAY_SERVER__PORT=8765, store /
                             uploads / collections in the session scratch directory)
python -I scratch/e2e/run_e2e.py               # end-to-end client, loopback only
git status --short evidence results.csv ; sha256sum results.csv
```

## 7. Test Results

**Full suite after each file:**

| After | Passed |
| :-- | --: |
| Pipeline collection path | 578 |
| Settings | 578 |
| Bootstrap | 583 |
| Routes | 596 |
| `/ask` collections | 600 |
| Compose | 603 |
| Review fixes | 620 |

**Final:** `620 passed`; `ruff check .` → `All checks passed!`

**Prompt tests 1–10:**

| # | Test | Where | Result |
| :-- | :-- | :-- | :-- |
| 1 | Create a collection | `test_ingestion_routes::test_create_and_list_collections` | pass |
| 2 | Upload creates a queued document and a job | `…::test_an_upload_creates_a_queued_document_and_a_job` | pass |
| 3 | De-duplication creates no job | `…::test_a_duplicate_upload_returns_the_existing_document_without_a_new_job` | pass |
| 4 | More than 50 MB → 413, no rows | `…::test_an_upload_over_the_size_limit_is_413_and_creates_no_rows`, plus the 50 MB default | pass |
| 5 | Worker goes through every state | `test_ingest_worker::test_a_queued_job_goes_through_every_state_to_ready` | pass |
| 6 | Failure, `error_class`, 3-attempt cap | `…::test_parsing_failures_are_recorded_and_retried_at_most_three_times` | pass |
| 7 | LRU eviction on the 9th collection | `test_index_registry::test_the_ninth_collection_evicts_the_least_recently_used` | pass |
| 8 | A pipeline request with a `collection_id` retrieves from that collection | `test_pipeline_collections`, `test_ask_collections` | pass |
| 9 | `/ask` without a `collection_id` works | `test_ask_collections::test_ask_without_a_collection_is_the_v1_path`, `test_compose_collections` | pass |
| 10 | The v1 papers are queryable through the new path | `test_compose_collections::test_the_v1_papers_are_queryable_through_the_collection_path` (real v1 index, local model, offline) | pass |

**Flaky v1 test.** `tests/test_standin.py::test_stragglers_are_much_slower` failed in 2 full-suite runs while the machine was loaded: the "normal" request took 156 ms instead of about 40 ms, so the required 100 ms gap was missed.
- Neither the test nor the stand-in code was changed (`git status` clean for both).
- It passed 5/5 when run alone, and in every full run after that.
- It is a timing-sensitive v1 test. I did not edit it.

## 8. Actual Output and Generated Artifacts

**End-to-end (`relay serve`, embedded calibrated stand-in, scratch store, loopback only, 0 Groq calls).**

Start-up banner:
- `store: … (schema v2, journal wal)`
- `collections: v1-papers ready (version 1, reads the v1 small_fast index)`

Results:
- `GET /v1/collections` → `[v1-papers, version 1]`.
- Create `geology-demo` → 201.
- Upload a new 2-page PDF ("Columnar Basalt Jointing: A Field Note", not in v1) → 202.
- The same bytes again → 200 `deduplicated`.
- **The document was `ready` 2.56 s after upload** (job `ready`, attempts 0, `extraction_quality {total_pages: 2, empty_pages: 0, suspected_scan: false}`).

`/ask` results:

| Request | Result | Tokens (in/out) | Notes |
| :-- | :-- | :-- | :-- |
| New collection: "Why do columnar basalt columns form?" | 200, **VERIFIED** | 565/52 | 2 sentences, both citing `[P1]` = chunk `0bb16ece…_p1_c0` of the uploaded document. This is the demo: a cited answer from a paper not in v1. |
| `v1-papers`: "What is d_model in the Transformer?" | 200, BLOCKED (`citation_verification_failed`) | 661/274 | Identical to the v1 path below: same tokens, same verdict. |
| v1 path, no `collection_id`, same question | 200, BLOCKED | 661/274 | The stand-in's generated answer did not pass the verifier, on both paths. |
| Isolation: the v1 question against `geology-demo` | `EARLY_REFUSAL`, no provider call | 0 | No v1 passage reached the geology collection. **But the refusal text still names the three v1 papers** (see §11). |
| Unknown collection | 404 `collection_not_found` | — | |

**Request log:** 4 rows; `collection_id` is set on the collection requests and `None` on the v1 request. `/healthz` `trace_sink`: written 4, failed 0, dropped 0.

**On disk:**
- `collections/<cid>/{builds/<uuid>/, docs/<doc>/extraction.json, index_ref.json}`.
- `collections/v1-papers/index_ref.json` only; nothing copied or re-embedded.

**Checks:**
- **Secret scan:** the only `gsk_…` match is v1's committed `FAKE_KEY` in `tests/factories.py` (unchanged).
- `results.csv` sha256 is `8dfb10df…44efd`; `git status evidence results.csv` shows nothing; there is no `data/uploads` or `data/collections` in the repository.

## 9. Errors and Root-Cause Analysis

- **Ruff F811 in `test_pipeline_collections.py`:** fixtures imported by name clashed with parameter names. Fixed with local fixtures.
- **Two `/ask` collection tests returned BLOCKED.** The scripted answer contained "line 0", a number that is not in the cited passage, and the verifier was right to block it. The test's answer was changed; the code was correct.
- **A long heredoc patch broke earlier,** so patches were moved to checked scratch scripts (every replacement must match exactly once).
- **Five worker tests needed updating after the fix pass,** because the contract changed:
  - "ready" is now written by the atomic `complete`;
  - the live index is the `index_ref.json` target;
  - an interruption counts as an attempt.
- **Stopping the end-to-end server:** Ctrl+Break could not be sent to the background process (not a process-group leader, WinError 87), so it was stopped with `Stop-Process`. The request log had already flushed (pending 0).

## 10. Fixes Applied

**Review pass.** I ran the `code-review` skill (high) and the `pr-review-toolkit:silent-failure-hunter` agent. Both first stopped on the usage limit and were rerun. I graded their findings by effect. Every Critical and Important finding I accepted got a test that failed first (17 tests, all RED for the expected reason, then green):

| Finding | Fix |
| :-- | :-- |
| **C1** Non-atomic index save (a crash breaks the collection for good) | Build directory plus atomic `index_ref.json` swap |
| **I1/I2** Version bumped before the document was ready; poisoned cue cache | `IngestJobRepo.complete`: one transaction |
| **I3** An interrupted job retried forever (crash loop) | Interruption counts as an attempt, capped |
| **I4** No backoff: 3 attempts used up in milliseconds | Wait a poll interval after a pass with no success |
| **I5** Document and job created separately (orphan document) | `create_queued`: one transaction |
| **I6** A failed document can never be re-added | Re-upload re-queues it (202 `requeued`) |
| **I7** Missing index files → 500 | `describe` checks the index → 409 |
| **I8** The worker loop logged full tracebacks with messages | Sanitized error only |
| **CR1** The collection gateway checked `paper_filter` against the v1 ids, so the gate's paper-scoped re-query never ran on collections | `ToolGateway(paper_ids=)` from the collection's index |
| **CR2** Generic titles became strong cues and skipped grounded refusals | Minimum title length; PDF title preferred |
| **CR5/M6** Chunked uploads spooled without limit; arXiv download had no cap | 411 without `Content-Length`; streamed download, capped |
| **CR8** Model loaded inside the 10 s tool timeout | Warmed in the registry load, outside the timed call |
| **CR10** `chunk_paper` ran on the event loop | `asyncio.to_thread` |

## 11. Outstanding Issues

**Deferred, as rulings:**
- **`/ask` with a `collection_id` has no API-key or tenant check.**
  - It is the unauthenticated v1 route; `/v1/ask` with keys is V2-05 (ARCH_V2 §9).
  - Today there is one dev tenant, but this must not ship beyond local use.
  - A 404/409 difference also reveals whether a collection exists.
- **v1-specific wording and cues on the collection path (V2-04 scope):**
  - Refusal messages name the three v1 papers. This was seen live in the isolation check above.
  - `requery_text` analyses without the collection's cues.
  - Verifier name cues are still v1.
- **Partial-failure leftovers:**
  - If the completion transaction itself fails after the index swap, the document's chunks stay in the index while it is marked `failed`, until a retry re-merges it.
  - `UnknownPresetError` or store errors raised while resolving a collection on `/ask` give a bare 500, with no trace row.

**Deferred minors** (from the silent-failure review):
- **M1** A missing chunk preset gives a `RuntimeError` class name.
- **M3** A corrupt stored upload is never rewritten.
- **M4** Other pymupdf exception types become 500.
- **M5** A non-UTF-8 extraction report gives 500.
- **M8** `aclose` swallows its own cancellation.
- The embedding model is loaded twice (once for v1 retrievers, once lazily for collections).
- The `relay serve` banner does not list the `/v1` routes.

**Not built in this phase:**
- `src/relay/app/jobs.py` (named in the plan): the worker lives in `ingestion/worker.py`, as the prompt specified.
- The `prompt.passage_metadata` label prints `arXiv:<paper_id>` even for uploads, where the id is a uuid.

## 12. Acceptance-Criteria Status

| Criterion | Status | Evidence |
| :-- | :-- | :-- |
| v1 chunk hashes identical through the new path | Met | Worker test for `small_fast` and `large_wide` (real PDFs); `v1-papers` index digest equals the manifest's `small_fast` sha256 |
| A new PDF is queryable without a restart | Met | Compose test; end-to-end: ready in 2.56 s, VERIFIED cited answer |
| Collections are isolated | Met | Pipeline, `/ask` and cache tests; end-to-end isolation check |
| A crash mid-ingest resumes | Met | `test_a_crash_mid_ingestion_resumes_on_restart`, plus the interruption tests |
| 546 tests plus the new ones pass | Met | 620 passed |
| ruff clean | Met | `All checks passed!` |
| No Groq calls | Met | Stand-in and scripted providers only; 0 real calls |
| `evidence/` and `results.csv` unchanged | Met | git status empty; sha256 unchanged |
| A scanned PDF is rejected (plan) | Superseded | Owner decision: flag, not reject |

## 13. Remaining Risks

- **Every request can reach any collection** through `/ask` until V2-05 adds keys and tenants. Local use only.
- **The v1 refusal wording on collections** is misleading to users until V2-04.
- **arXiv downloads are new outbound network access.** It is user-triggered and capped, but it is not mocked anywhere except in the tests.
- **Old index builds:** on Windows, deleting a build that a reader still has open may fail. That failure is logged; it only wastes disk space.

## 14. Recommended Next Phase

**V2-04** (collection-aware scope, gate and verifier):
- collection-specific refusal messages;
- cues for `requery_text`;
- verifier name cues from the store;
- re-run the v1 Table 3 questions through the generalised path.

Before that, the owner should decide on the [PROPOSED] rulings in §5, and on whether tenant checks for `/ask` should move ahead of V2-05.

### Plugins used, and whether they helped

| Plugin | Used for | Helped? |
| :-- | :-- | :-- |
| `superpowers:test-driven-development` | Every new test file was run RED first. | **Yes.** It caught 2 test-fixture bugs (a numeric answer the verifier correctly blocked; a list passed where the strict schema needs a tuple) before they could hide real behaviour. |
| `superpowers:executing-plans` | Step-by-step execution with a full-suite run after each file. | **Yes.** It isolated the flaky v1 timing test from my changes. |
| `code-review` skill (high) | Whole-diff bug review. | **Yes, substantially.** 10 findings. The `paper_filter` one (the gate's expansion silently never runs on collections) and the generic-title cue were real logic bugs that no test of mine covered. |
| `pr-review-toolkit:silent-failure-hunter` agent | Failure paths and consistency. | **Yes, substantially.** It found the critical non-atomic index swap, the crash loop, no backoff, the orphan document and the unrecoverable failed document. 8 of its findings were fixed. |
| `superpowers:verification-before-completion` | Fresh suite, ruff, end-to-end and evidence checks before any claim. | **Yes.** The end-to-end run surfaced the v1 refusal wording on collections, which the tests did not show. |

Both review plugins stopped once on the 5-hour usage limit and were rerun after it reset; their results above come from the completed reruns.
