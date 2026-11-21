# Phase V2-10: Tenants, API keys and fair admission

- **Date:** 2026-11-21 IST
- **Status:** completed, pending owner review.
- **Real Groq calls:** 0 (cap 0). Every test runs offline with a scripted provider and an
  in-memory store.
- **Frozen evidence:** `results.csv` sha256 still starts with `8dfb10dfaadb`. `git status`
  shows no change under `evidence/`.

## 1. Objective

Several users can share one deployment safely (ARCHITECTURE_V2 §7 and §12):

- hashed API keys;
- tenant scoping on every `/v1` route, with cross-tenant access returning 404;
- key management commands;
- collection deletion that removes files, index, cache and logs;
- a token bucket per tenant and two priority classes;
- a capacity derived from a moving average of measured tokens per answer;
- a light tenant that stays responsive while a heavy tenant is throttled or shed.

## 2. Implementation Summary

**Keys.** Migration **7** adds `api_key.revoked`, `name` and `created_at`.

- `ApiKeyRepo.create(tenant_id, role="member", daily_limit=1000, name=…)` returns the raw
  key `rel_live_<32 hex>` once, together with the record. Only `SHA-256(key)` is stored.
- Also: `get_by_hash`, `get_by_id`, `revoke` and `list_for_tenant`.
- `TenantRepo.list_all` lists tenants.

**`api/auth.py` (new).** `Authenticator.authenticate` is used by every `/v1` route.

- It reads the Bearer key, hashes it and looks up the key; unknown and revoked keys both
  get 401. The dev-key comparison is constant-time (`hmac.compare_digest`).
- The development key `relay-dev-key` is the dev tenant (admin), only when enabled. By
  default (`[auth] dev_key_enabled` unset) compose enables it only when the server listens
  on a loopback address.
- `charge()` enforces a key's daily limit on `/v1/ask`. It counts in memory per UTC day and
  returns 429 `daily_limit_reached`.
- `get_current_tenant(request, auth)` and `require_admin` are helpers.
- `Rejected` moved to `app/routes/rejected.py` to avoid an import cycle; the ingestion
  module still exports it.

**Tenant scoping.** Every route authenticates and checks ownership:

- `/v1/collections` (list and create), `/v1/collections/{id}` (new GET), its documents,
  `/v1/documents/{id}`, `/v1/conversations` (create and get) and `/v1/ask`;
- another tenant's collection, document or conversation returns **404**, so existence never
  leaks;
- `/v1/whoami` (new) tells the UI its tenant, role, daily limit and a masked key;
- `POST /v1/collections` no longer needs `tenant_id`; the collection always belongs to the
  key's tenant;
- the request log now records `tenant_id`.

**`DELETE /v1/collections/{id}`** (admin only):

- **One transaction** (`CollectionRepo.delete_cascade`) removes messages, conversations,
  grades, evaluation questions and sets, chunks, ingest jobs, documents, the collection's
  `request_log` rows and the collection.
- **Refused with 409 `collection_busy`** while a document is being parsed, embedded or
  indexed. The check runs inside the same write lock as the delete.
- **Then the files:** the collection's own folder (index builds and extraction reports) and
  the upload files **no remaining document uses**. A PDF shared with another collection
  stays. The shared arXiv `papers_dir` is never touched.
- **Then the in-memory state:** the collection's cached answers (`ExactCache.purge_collection`),
  its loaded index and its safety rules (`StoreCollectionResolver.forget`).
- **`v1-papers` is protected (409).**

**Fair admission** (`control/admission.py`):

- **Per-tenant token bucket.** It refills at `tenant_rate_share × service rate`
  (default 0.5), with a burst of 4. A request that finds its tenant's bucket empty is
  **over rate**: it is demoted, not refused, so the queue never idles while work waits.
- **Rank of a waiting request:**
  - 0: interactive, within rate;
  - 1: batch, within rate;
  - 2: interactive, over rate;
  - 3: batch, over rate.
- **Fair order within a rank:** the tenant admitted least recently goes first (round robin),
  then arrival order.
- **When the queue is full,** an arrival sheds the **newest waiter of a worse rank**
  (`queue_full`, `shed_reason: "shed_for_priority"`); if none is worse, the arrival is shed.
  Batch is therefore shed first.
- **`/v1/ask` takes `priority: "interactive"|"batch"`;** the default is interactive (the UI).
- **The capacity follows an EWMA** of the measured tokens per answer, reported by the
  provider (`observe_tokens` after each `/v1/ask` answer, re-derived with v1 §15). It is off
  when `queue.capacity` is set by hand.
- **Without a tenant, with the default priority (v1 `POST /ask`), the order is exactly the
  old FIFO,** including "full" being checked before the deadline forecast.

**CLI**

- `relay keys create --tenant T [--role admin|member] [--name N] [--daily-limit N|0]` prints
  the key once, with "Store this key now: it is shown only once and cannot be recovered."
- `relay keys list --tenant T` shows ids, names, roles, a 12-character hash prefix, state
  and creation date, never the key.
- `relay keys revoke --id K`.
- `relay tenants create --name N` and `relay tenants list`.

**UI**

- The Status page has an **Access** table: the tenant, the masked key ("Development key, for
  local use only" for the dev key), the role and the daily limit. A rejected key shows
  "not accepted: …".
- Collections are created without a tenant id.

**Tools:** the v2 load harness (`experiments/v2_load.py`, `scripts/run_streaming_load.py`) now
sends the dev key, because any other placeholder key is rejected.

## 3. Files Created

- `src/relay/api/auth.py`
- `src/relay/app/routes/rejected.py`
- `tests/test_v2_10_tenants.py`: 16 tests (13, plus 3 from the review fixes)
- `tests/test_v2_10_admission.py`: 18 tests
- `web/src/__tests__/access.test.tsx`: 3 tests
- `reports/phase-v2-10-tenants-keys-fair-admission.md` (this file)

## 4. Files Modified

- **Store:**
  - `src/relay/store/migrations.py`: migration 7;
  - `src/relay/store/repositories.py`: `ApiKeyRecord`, `ApiKeyRepo`, `DeletedCollection`,
    `CollectionBusyError`, `hash_api_key`, `new_api_key`, `TenantRepo.list_all`,
    `CollectionRepo.delete_cascade`;
  - `src/relay/store/__init__.py`: `Store.api_keys`.
- `src/relay/control/admission.py`: rewritten (fair queue), with the same public API plus
  `tenant`, `priority` and `observe_tokens`.
- `src/relay/control/cache.py`: `purge_collection`.
- `src/relay/app/collections.py`: `forget`.
- `src/relay/app/routes/ingestion.py`: authentication, GET and DELETE of one collection,
  whoami, the optional `tenant_id`.
- `src/relay/api/routes/ask_v1.py`: authentication, the daily limit, `priority`, tenant and
  priority into admission, `tenant_id` in records, `observe_tokens`, `shed_reason`.
- `src/relay/app/request_log.py`: `tenant_id` mapped.
- `src/relay/app/settings.py`: `AuthSettings` and the `QueueSettings` fairness fields.
- `src/relay/app/compose.py`: `dev_key_enabled`, `admission_queue`, the authenticator and the
  delete hook wiring, an auth start-up note.
- `src/relay/cli.py`: `relay keys` and `relay tenants`.
- `relay.example.toml`: the `[queue]` fairness keys and `[auth]`.
- **Web:**
  - `web/src/api.ts`: `WhoAmI` and `whoami()`; no `tenant_id` on create;
  - `web/src/pages/StatusPage.tsx`: the Access table;
  - `web/src/styles.css`;
  - `web/src/buildConfig.ts` and `web/.env.example`: comments;
  - `web/src/test/fakeApi.ts`.
- **Test helpers:** a few helpers that sent `"Bearer dev"` or other placeholder keys now send
  the dev key. This is the intended change: such keys are now rejected.
  - `tests/test_ask_v1.py`, `tests/test_compose_collections.py`, `tests/test_ingestion_routes.py`,
    `tests/test_streaming_load.py`, `tests/test_v2_05_silent_failure_fixes.py`,
    `tests/test_v2_08_followups.py`;
  - `tests/test_v2_09_router.py`: the migration assertion is now `>= 6`;
  - `web/src/__tests__/api.test.ts`: the create body, plus a whoami test.
- `src/relay/experiments/v2_load.py` and `scripts/run_streaming_load.py`: the dev key.
- `src/relay/app/static/*`: the rebuilt UI.

## 5. Technical Decisions

These are my rulings. Each says what it costs if wrong.

1. **The dev key is accepted only on a loopback host by default.**
   - The UI, the README, the demo and the load tools rely on `relay-dev-key` locally.
   - On a reachable host it would be a public admin key, so it is off unless
     `[auth] dev_key_enabled = true`.
   - Cost if wrong: a deployment bound to `0.0.0.0` needs real keys for the UI (set
     `VITE_API_KEY`).
2. **Over-rate requests are demoted, not refused.** A refusal would leave capacity idle when
   only one tenant is active. Fairness comes from ordering and from shedding the worst rank
   when the queue is full. Cost if wrong: a heavy tenant alone still uses the whole service.
   This is intended (work-conserving).
3. **No new error code for shedding.** `ErrorCode` lives in the frozen v1 schema module, so a
   shed request reuses `queue_full`. A new `shed_reason` field (`shed_for_priority` or
   `queue_full`) explains why.
4. **`DELETE` needs the admin role.** Members get 403 for their own collection; another
   tenant's collection is still 404. `v1-papers` returns 409 because it is the v1 corpus. A
   collection with a document being ingested returns 409. Cost if wrong: one more call by an
   admin key.
5. **Upload files are content-addressed and can be shared,** so only files no remaining
   document uses are deleted. arXiv PDFs in `papers_dir` are never deleted.
6. **v1 `POST /ask` stays unauthenticated,** as ARCHITECTURE_V2 §9 keeps it for the load
   harness. It is outside the `/v1` routes and serves only the v1 papers.
7. **v1-papers belongs to the dev tenant.** Another tenant's `/v1/ask` without a
   `collection_id` gets 404, because the default collection is not theirs. That tenant must
   name its own collection.
8. **The daily limit is counted in memory.** It is reset by a restart and is per process.
   A persistent count (in `request_log`) is left for later.
9. **Migration 7 also adds `api_key.created_at`.** The prompt's `relay keys list` shows
   creation dates, and the table had no such column.
10. **`get_current_tenant` is a plain async helper, not a FastAPI `Depends`.** The routes
    already turn `Rejected` into the project's `{error, message}` JSON shape. A dependency
    would answer in FastAPI's `{detail}` shape.
11. **The "agreed floor" from the plan is not set.** The plan's "done when" asks that the
    light tenant's delivery rate stay above an agreed floor. No floor has been agreed; the
    tests assert where B finishes in the order instead (§7).

## 6. Commands Actually Executed

The Python interpreter is `C:/Users/ASUS/anaconda3/envs/the-relay/python.exe`.

- `pytest -q` after the store, auth and route changes: 81 failed, 819 passed. All 81 were
  tests sending `Bearer dev`. After pointing those helpers at the dev key: **900 passed**.
- `pytest tests/test_v2_10_tenants.py -s`:
  - first run: 4 failed. Causes: a helper argument name clash, a missing
    `ingest_jobs.set_state` (the method is `update_state`), a test that assumed two
    generated PDFs share one hash, and shed requests counted as finished answers. All four
    were test-side.
  - after the fixes: 12 passed.
- `pytest tests/test_v2_10_admission.py tests/test_v2_10_tenants.py`:
  - 1 failed: the FIFO contrast, explained in §9.
  - after replacing it: **31 passed**.
- Checks that the tests can fail. Each run monkeypatches the code through a pytest plugin
  module kept in the scratchpad:

  | Plugin | What it switches off | Result |
  | :-- | :-- | :-- |
  | `mut_nofair` | ranks and tenant order | 8 failed |
  | `mut_noauth` | the key check: any key is the dev admin | 12 failed |
  | `mut_nopurge` | file and cache cleanup on delete | 1 failed |

- `npx jest` on the new UI tests before implementing them: 5 failed (RED). After: `npx jest`
  **54 passed**; `tsc` exit 0; `eslint` exit 0.
- `relay ui build`: copied 3 files.
- `python -m pytest -q`, before the review: **931 passed** in 167.74 s. `ruff check .`: All
  checks passed.
- A fresh-context code review (an agent), then 3 regression tests: **3 failed (RED)**, each
  for the reported reason. After the fixes: `tests/test_v2_10_*`: 34 passed.
- `python -m pytest -q`: 1 failed, 933 passed. The failure was
  `tests/test_standin.py::test_stragglers_are_much_slower`: 189.6 ms measured against a
  190 ms threshold, a wall-clock flake under full-suite load. Run alone 5 times: 5 passed.
  Neither the stand-in nor its test was changed in this phase (`git diff` is empty for both).
- Final `python -m pytest -q`: **934 passed** in 125.21 s. `ruff check .`: All checks
  passed. `npx jest`: 54 passed. tsc and eslint: exit 0.
- `sha256sum results.csv`: `8dfb10dfaadb…`. `git status --short results.csv evidence/`:
  empty.

## 7. Test Results

| Suite | Result |
| :-- | :-- |
| Backend `pytest -q` | **934 passed**, 0 failed, 0 skipped (900 before, plus 34 new) |
| `ruff check .` | clean |
| UI Jest | **54 passed** (50 before, plus 4 new) |
| UI `tsc` / `eslint` | exit 0 / exit 0 |

The tests the prompt asked for:

| Test | Observed |
| :-- | :-- |
| Key lifecycle | The key is `rel_live_` plus 32 hex; the `api_key` table holds its SHA-256 and never the key; whoami resolves the right tenant; revoked, unknown and malformed keys and a missing header all get 401 on REST and on `/v1/ask` |
| Isolation | Tenant B gets **404** on each of these for tenant A's resources: GET and DELETE of the collection, GET of the document, POST of a document, GET of the conversation, POST of a conversation, `/v1/ask`. B's collection list is empty. No provider call is made |
| Cache isolation | The same question in two tenants' collections is a miss, then a miss: B pays for its own answer (2 provider calls), and A's repeat is a hit |
| Deletion | **Rows:** collection, documents, chunks, conversation, messages and request-log rows are gone. **Files:** the index folder is gone; the shared upload stays until the second collection using it is deleted. **Cache:** 1 entry before, 0 after. `/v1/ask` on it then gets 404. A member gets 403; `v1-papers` gets 409; a collection being ingested gets 409 |
| Noisy neighbour, queue 40 | A sends **30 batch** questions at once; then B sends 1 interactive one. **B finished after 1 of A's 30 answers** (the one already running), after waiting 0.41–0.54 s across runs. All 30 of A's answers were still delivered |
| Noisy neighbour, queue 8 | **B finished after 1 of A's answers.** 22 of A's 30 were shed with `queue_full`, one of them with `shed_for_priority` to make room for B |
| Two interactive tenants | A sends 12 at once, over its burst of 2. B finished after 1 of A's |
| v1 baseline (unit) | Without tenants (v1 `/ask`), B waits behind A's whole burst (position 5 of 5). With tenants, B is at position 1 or 2 |
| Also | Interactive goes before batch; over-rate waits behind in-rate; the bucket refills at its rate; the EWMA capacity (1500 → 1000 → 550 tokens per answer gives the re-derived capacity and rate); a fixed capacity is never adapted; the dev-key policy for 7 host and setting cases; CLI create, list and revoke (the list never shows the key) and an unknown tenant giving exit 1; the daily limit (second request gets 429) |

## 8. Actual Output and Generated Artifacts

Printed by `pytest -s` (wall-clock times of the scripted provider, 10 ms per content piece):

```text
noisy neighbour (queue 40): B finished after 1 of A's 30 answers, waited 0.54 s
noisy neighbour (queue 8): B finished after 1 of A's answers; 22 of A's 30 shed; B waited 0.52 s
two interactive tenants: B finished after 1 of A's 12
```

There are no other artifacts, and no run directory: no load run was made in this phase.

## 9. Errors and Root-Cause Analysis

1. **81 existing tests failed once real keys were checked.** They sent `Bearer dev`, and
   `"Bearer any-dev-key"` or `"Bearer x"` in places. Those were valid only under the old stub.
   The helpers now send the dev key; no assertion changed.
2. **A ruff auto-fix stripped the quotes inside `Literal["interactive", "batch"]`** because
   `Literal` was not imported, so it read them as forward references. Fixed by importing
   `Literal` and restoring the quotes.
3. **A bash heredoc failed on quoting** before running anything. The files were written with
   the Write and Edit tools instead.
4. **Four test-side mistakes,** as listed in §6.
5. **The first FIFO contrast test was wrong.** With tenant ids but buckets off, B was still
   served after 1 of A's answers. The round robin between tenants already protects B, so
   "buckets off" is not v1's FIFO. v1's FIFO is "no tenant at all". The contrast is now a
   unit test without tenants, and the request-level test asserts only the fair behaviour.

**Findings from the code review.** The review found no cross-tenant leak. Each finding below
was confirmed against the code and given a regression test that failed first.

6. **A delete could remove a file that a new upload needs.**
   - Root cause: `DELETE` released the ingestion lock before removing files. An upload of the
     same PDF to another collection in that gap found the file still present, skipped writing
     it, and then lost it to the delete.
   - Fix: the cache purge, the index eviction and the file removal now run while the lock is
     still held.
   - Test: `test_a_pdf_uploaded_while_a_delete_removes_files_is_kept`. It holds the removal
     open and uploads meanwhile; without the fix the upload file was missing.
7. **An answer in flight when its collection was deleted** re-added a cache entry and wrote a
   request-log record for the deleted collection.
   - Fix: `AskV1Context.deleted` (filled by the delete hook) and `AskV1Context.record`. That
     answer is still delivered to its client, but it is not cached and not logged.
   - Test: `test_an_answer_finishing_after_its_collection_was_deleted_leaves_nothing`.
8. **Minor: the daily limit was charged before the collection was resolved,** so a 404 or 409
   spent a request. It is now charged after.
   Test: `test_a_refused_ask_does_not_spend_the_daily_limit`.

Not changed, and noted:

- A request shed from a full queue still uses its tenant's bucket token.
- The EWMA changes capacity at runtime (only through `/v1/ask`; v1 `/ask` never calls
  `observe_tokens`). A load run mixing both endpoints should read the capacity from
  `/healthz` at the time.

## 10. Fixes Applied

All of them are in §9. The final full runs are in §6.

## 11. Outstanding Issues

- **The main tests were written after the code.** The exceptions are the UI tests and the 3
  review regression tests, which failed first. To compensate, the mutation checks in §6
  confirm that each group fails without its feature, and a fresh-context code review was run
  (§9, items 6–8).
- **`tests/test_standin.py::test_stragglers_are_much_slower` is timing-sensitive.** It failed
  once under load at 189.6 ms against 190 ms. It belongs to an earlier phase and was left
  unchanged; it may need a wider margin.
- **No load run measured the two-tenant delivery rate.** The plan's "agreed floor" needs the
  owner to set it. A harness run at the stand-in's calibrated limits would measure it
  against that floor.
- **The daily limit is in memory, per process.**
- **The README and demo docs do not yet mention `relay keys`.** `relay.example.toml` and
  `web/.env.example` do.
- **Not deleted with a collection:** in-flight `/v1/ask` streams on it (they finish from the
  index already loaded), and ingest jobs that start after the busy check (none can start: the
  jobs are deleted in the same transaction).

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| `relay keys` manages hashed API keys | met (create, list, revoke; plus `relay tenants`) |
| Every `/v1` route enforces tenant scoping; cross-tenant access is 404 | met (7 routes tested; `/v1/ask` included) |
| `DELETE /v1/collections/{id}` purges database, index files and cache | met (also conversations, request-log rows and orphan uploads) |
| Fair admission protects interactive and light tenants during noisy bursts | met (B after 1 of A's 30, with queue 40 and queue 8) |
| 900+ backend and 50+ UI tests pass; ruff, tsc and eslint clean | met: 934 and 54 (one flaky timing test, §6) |
| Zero real Groq calls; `results.csv` and `evidence/` untouched | met |

## 13. Remaining Risks

- Under sustained interactive load, batch requests can wait indefinitely; there is no aging.
  This follows from the design: batch is lower priority.
- The tenant bucket counts requests, not tokens. A tenant sending very long questions is
  throttled by its request count only. The per-lane ProviderGuard still bounds tokens.
- Old keys from before migration 7 do not exist (the table had no rows), so `created_at` is
  `NULL` only for rows inserted outside `ApiKeyRepo`.

## 14. Recommended Next Phase

1. Owner review of this report.
2. The owner agrees the light-tenant delivery-rate floor, so a stand-in harness run can
   measure the two-tenant overload against it.
3. Then V2-11: observability (`/metrics`, traces, `GET /v1/requests/{id}` scoped by
   tenant).
