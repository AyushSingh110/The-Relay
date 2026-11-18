# Phase V2-02 Implementation Report — Metadata Store

> **Phase ID:** `v2-02`
> **Date:** `2026-11-18 21:58 IST`
> **Status:** `Completed`.
> - Verified in Conda env `the-relay` (Python 3.12.16, SQLite 3.53.4, aiosqlite 0.22.1): 546 passed, 0 failed, 0 skipped; ruff clean.
> - **Real Groq calls: 0.**
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

From the owner's prompt (plan V2-02; `docs/ARCHITECTURE_V2.md` §5):
- SQLite in WAL mode behind repositories: the twelve specified tables, a versioned idempotent migration runner, six repository classes, and `Store` / `open_store`.
- Integration: in `sqlite` mode, `compose` opens the store at startup and passes it into the app; `memory` mode is unchanged; `relay doctor` reports store status.
- Tests on `:memory:`.

**Done when:**
- migrations apply from empty and are idempotent;
- a restart keeps the request log;
- `doctor` reports the store;
- the 508 tests pass;
- no test opens a real file;
- ruff is clean;
- no Groq calls;
- `results.csv` and `evidence/` are unchanged.

## 2. Implementation Summary

- **`store/schema.sql`** (migration 1): the 12 tables with the exact names and columns given.
  - Additions within the spec: `NOT NULL` on keys and foreign keys, `REFERENCES` for every foreign key listed, and indexes on the foreign-key columns.
  - `request_log` has no foreign keys, so a request is logged even for an unknown tenant or collection.
- **`store/migrations.py`**
  - `open_connection`: foreign keys on, 5 s busy timeout, and WAL with `synchronous=NORMAL` for file databases.
  - `migrate`: versioned and idempotent. The one-row `_schema_version` table is enforced by `CHECK (id = 1)`.
    - Each migration runs in one transaction together with its version bump.
    - A failing migration rolls back completely.
    - A database newer than the code is refused.
  - Introspection helpers (`schema_version`, `table_names`, `row_counts`, `journal_mode`), so tests need no SQL.
- **`store/repositories.py`**: `TenantRepo`, `CollectionRepo`, `DocumentRepo`, `IngestJobRepo`, `RequestLogRepo` and `ConversationRepo`, as specified, plus read-back helpers.
  - Every method is async and returns frozen dataclasses.
  - Integrity errors become `ForeignKeyViolationError` / `IntegrityViolationError`; a missing row on update is `NotFoundError`; values outside the allowed status, state and role sets are `InvalidValueError`.
  - **One write lock per connection** is held from a write's statement to its commit or rollback.
  - A failing rollback is logged and noted on the original error, never raised over it.
- **`store/__init__.py`**: `Store` (path, connection, migration result, journal mode, all six repositories) and `open_store(path)`.
  - The path is checked first, before anything is created.
  - Every SQLite failure while opening or migrating becomes `StoreOpenError` with SQLite's message. It is also a `ConfigurationError`, so the CLI prints it cleanly.
  - The connection is always closed; a failing close is logged.
- **`app/request_log.py`**: `RequestLogSink`, the request-log writer the plan asks for.
  - The app's `"request"` trace records become `request_log` rows, written by a background task through `RequestLogRepo`.
  - It never fails or delays an answer, and no loss is silent:
    - written, failed and dropped rows are counted, reported in `/healthz` (`trace_sink`) and summarised at shutdown;
    - the flush is bounded (10 s); rows left behind and rows interrupted mid-write count as dropped;
    - late records count as dropped;
    - a writer that dies is logged.
- **Integration**
  - **`compose.py`:** in `sqlite` mode the store opens **first**, so a bad path fails before any model loads or server starts. Then the sink starts and is passed to the app. Shutdown order: provider, stand-in, sink flush, store close. **`memory` mode is unchanged:** no store, no sink.
  - **`api/app.py`** (v1): typing and one additive health field only, detailed in §4.
  - **`cli.py`:**
    - `doctor` gains a store check: in sqlite mode it opens the store and applies migrations, with a **WARN** if that created a new store; otherwise "not used".
    - The cache-mode line now says where answers and the log live.
    - `serve` turns Ctrl+Break into a graceful stop and sends `relay.*` log records to the console.

## 3. Files Created

- `src/relay/store/__init__.py`, `migrations.py`, `repositories.py`, `schema.sql`
- `src/relay/app/request_log.py`
- Tests:
  - `tests/test_store_migrations.py` (5)
  - `tests/test_store_repositories.py` (11)
  - `tests/test_store.py` (5)
  - `tests/test_store_integration.py` (7)
  - `tests/test_v2_02_review_fixes.py` (11)
- `reports/phase-v2-02-metadata-store.md` — this report.

## 4. Files Modified

- **`pyproject.toml`** — `aiosqlite>=0.20` under `[project] dependencies`. Installed: 0.22.1.
- **`.gitignore`** — `data/*.sqlite3`, `-wal` and `-shm`, so a running store is never committed. `results.csv` and `evidence/` are still not ignored (checked).
- **`src/relay/api/app.py`** (v1, additive):
  - a `TraceSink` protocol replaces the `RunHandle` annotation (typing only; the `RunHandle` import was removed);
  - `health()` adds `trace_sink` when the sink offers `stats()`.
- **`src/relay/app/compose.py`, `src/relay/cli.py`** — the integration in §2.
- **`tests/test_app_compose.py`, `tests/test_cli.py`** — **one test case retired and one test narrowed**, because they asserted that `sqlite` is refused, which V2-02 deliberately changes (§5.6).
- **`ENGINEERING_LOG.md`** — one appended entry.
- **Unchanged:** `results.csv` (SHA-256 `8dfb10df…44efd`) and `evidence/` (`git status`: 0 changes).

## 5. Technical Decisions

All items below are **[PROPOSED]** unless marked as owner decisions.

1. **Temporary files for the restart and WAL tests only** (owner decision 2026-11-18, asked because the prompt's two rules conflict). An in-memory database cannot survive a close, and WAL does not apply to it. Those two tests use pytest's `tmp_path`, never `data/relay.sqlite3`. All other store tests use `:memory:`.
2. **`cache.mode = "sqlite"` keeps the answer cache in memory and adds the store with its request log.** The specified schema has no cache table, so a persistent answer cache (`docs/ARCHITECTURE_V2.md` §8) is not part of V2-02. `doctor` and the start-up banner now say this plainly.
3. **The request log is fed by the app's existing trace records**, through a `TraceSink` protocol and a background writer, rather than by changing the request path.
   - `ttft_ms` and `total_ms` are server-side times (`provider_first_token_ms`, `server_total_ms`), because the server cannot see the client's.
4. **Row ids are generated UUIDs.** The specified schema has no `request_id` column, and a client-chosen request id can repeat. Recommendation: add `request_id` in migration 2 (owner decision).
5. **No automatic directory creation.** A store path whose directory does not exist is an error, which keeps typos visible.
6. **Two existing tests changed:** the `sqlite-cache` case of `test_open_runtime_refuses_unusable_settings_with_a_clear_reason` was removed, and `test_doctor_flags_an_unavailable_cache_mode_and_a_bad_profile` became `test_doctor_flags_a_bad_profile`. Both asserted the V2-01 rule "sqlite is refused", which this phase replaces. The narrowed CLI test would otherwise have made `doctor` create the real `data/relay.sqlite3`.
7. **Rulings during execution** (each with its cost if wrong):
   - The repository tests first used a migrated connection fixture, then the fresh-`Store` fixture once `Store` existed. Cost: none.
   - `PRAGMA` setup lives in `migrations.py` (`open_connection`), keeping raw SQL in the two permitted files. Cost: none.
   - The request-log mapping and sink live in `src/relay/app/` (the app layer), using only the repository. Cost: none.
   - **Deferred** (from review):
     - an abandoned leader's tokens are NULL in `request_log`, because its spend is in the flight record;
     - `start_run()` would replace the sink, but it is not used by `serve`;
     - per-row commits, not batched;
     - `_apply` interpolates internal constants into its script.

## 6. Commands Actually Executed

```bash
git status --short ; git tag --list (v1-submission present) ; sqlite version 3.53.4
python -m pip install "aiosqlite>=0.20" -q            # 0.22.1
(pyproject + .gitignore) ; git check-ignore ...        # store files ignored; evidence not
timeout 900 python -m pytest -q                        # 508 passed
(schema.sql) ; sqlite3 parse check (12 tables) ; pytest  # 508 passed
pytest tests/test_store_migrations.py   # RED: collection error (module missing)
(migrations.py) ; pytest that file      # 5 passed ; full suite 513 passed
pytest tests/test_store_repositories.py # RED ; (repositories.py) ; 16 passed with migrations
python scratchpad/fk_mutation.py        # without the pragma an orphan is accepted; with it: FK error
full suite                               # 524 passed
pytest tests/test_store.py              # RED ; (__init__.py) ; 5 passed ; full suite 529 passed
pytest tests/test_store_integration.py  # RED ; (request_log.py, app.py, compose.py, cli.py,
                                         #  2 old tests updated) ; 7 passed ; full suite 535 passed
code-review skill (medium) ; pr-review-toolkit:silent-failure-hunter agent
pytest tests/test_v2_02_review_fixes.py # RED: 10 failed (one shown failing for the right reason)
(fixes) ; 9 passed, 1 failed (real: in-flight row not counted) -> fixed ; 10 passed
full suite                               # 545 passed
python scratchpad/v2_02_e2e.py           # sessions exited 3, no shutdown summary (Ctrl+Break)
(Ctrl+Break test RED ; handler + logging) ; full suite 546 passed ; e2e again: exit 0 + summary
python scratchpad/v2_01_e2e.py           # memory mode unchanged
git status --short evidence results.csv ; sha256sum results.csv ; ls data/*.sqlite3*
timeout 900 python -m pytest -q ; python -m ruff check .   # final
```

## 7. Test Results

Full-suite results after each step:

| After | Result |
| :-- | :-- |
| Dependency install | 508 passed |
| `schema.sql` | 508 passed |
| `migrations.py` | 513 passed |
| `repositories.py` | 524 passed |
| `store/__init__.py` | 529 passed |
| Integration | 535 passed |
| Review fix pass | 545 passed |
| Ctrl+Break fix | 546 passed |
| **Final** | **546 passed, 0 failed, 0 skipped**; ruff: All checks passed! |

- **Count:** 508 − 1 retired case + 39 new = 546.
- **Every new test file was run alone and failed first:**
  - migrations, repositories, Store and integration: collection errors, because the module did not exist yet;
  - review fixes: 10 of 10 failed;
  - Ctrl+Break: 1 of 1 failed.
- **No store file was created in `data/`** by any test run (checked after each run).

The owner's seven required tests:

| # | Required | Test | Result |
| :-- | :-- | :-- | :-- |
| 1 | Migrations apply cleanly from empty | `test_migrations_apply_cleanly_from_empty` | pass |
| 2 | Idempotent (twice, same row counts) | `test_migrations_are_idempotent` | pass |
| 3 | `bump_version` increments and returns the new value | `test_collections_list_per_tenant_and_bump_their_version` | pass (0 → 1 → 2) |
| 4 | `update_status` persists across a repository re-instantiation | `test_document_status_persists_across_repository_instances` | pass |
| 5 | `RequestLogRepo.insert` writes every column without truncation | `test_request_log_insert_writes_every_column_without_truncation` | pass (2**40 tokens, 64-character hash, exact floats) |
| 6 | Foreign-key violation raises the application exception | `test_foreign_key_violations_raise_the_application_exception` (4 cases) | pass; mutation check proves the test catches a missing pragma |
| 7 | Invalid path raises a clear error before the app starts | `test_open_store_with_an_invalid_path_fails_clearly_before_anything_starts`, `test_an_invalid_store_path_fails_before_the_app_starts` | pass |
| — | Restart keeps the request log | `test_a_restart_keeps_the_request_log` (temp file, real reopen) | pass |

## 8. Actual Output and Generated Artifacts

**End-to-end run, sqlite mode** (stand-in, 0 real calls; store in the gitignored `scratch/v202_demo.sqlite3`):

```
[  OK] cache mode: sqlite (answers cached in memory; request log in the store)
[WARN] store: scratch\v202_demo.sqlite3 opened, migrations applied (schema v1, journal wal); created a new, empty store: ...
--- relay serve (first): q03, q10 -> HTTP 200, VERIFIED; healthz trace_sink {'written': 2, 'failed': 0, 'dropped': 0, 'pending': 0}
    stopped with exit 0 | INFO: relay.app.request_log: request log closed: 2 written, 0 failed, 0 dropped
[  OK] store: scratch\v202_demo.sqlite3 opened, migrations applied (schema v1, journal wal)
--- relay serve (second): q12 -> HTTP 200, VERIFIED; written 1; exit 0
--- request log after two sessions (restart in between): 3 rows; schema v1, journal wal, migrations applied on this open: ()
```

**Memory mode** (V2-01 check, re-run):
- `doctor`: all checks pass, `[INFO] store: not used (cache mode memory)`.
- q03, q10 and q12: HTTP 200, `VERIFIED`, 672–844 ms.

**Secret checks:** no key value and no `gsk_` pattern in any doctor output, serve log or response.

## 9. Errors and Root-Cause Analysis

1. **The prompt's two rules conflicted:** a "close and reopen" test against "`:memory:` only". The owner decided (§5.1).
2. **Code-review and silent-failure-hunter findings** (overlapping; grouped by effect):
   1. **CRITICAL:** a failed write's rollback on the shared connection could undo another coroutine's committed-looking write. Reproduced: `create` returned a tenant whose row no longer existed.
   2. The writer's counters were never reported.
   3. `aclose()` could hang (dead writer, locked database) and lose rows without counting them.
   4. `append_trace` could raise on the request path.
   5. A failing rollback replaced the original error.
   6. SQLite errors during migration escaped as raw `sqlite3` errors, crashing `doctor`.
   7. The writer's log hid programming errors.
   8. `doctor` silently created a store at a mistyped path.
   9. The name of the `sqlite` mode suggested a persistent answer cache.
3. **Found by a fix test:** a row the writer had already taken off the queue, interrupted by shutdown, was lost without being counted.
4. **Found by the end-to-end run:** Ctrl+Break killed `relay serve` on Windows (exit 3), so the request log's shutdown flush and summary never ran. Also, the `relay.*` loggers had no handler, so the INFO-level summary would never have been shown.
5. **Tooling slips:** a backslash continuation in a heredoc edit collapsed into one over-long line (caught by ruff); a stale Pyright view (`Protocol` undefined) while ruff and the tests passed.

## 10. Fixes Applied

1. **Owner decision applied** (§5.1).
2. Review fixes, each with a test in `tests/test_v2_02_review_fixes.py` that failed first:
   - **2.1:** a per-connection write lock held from statement to commit/rollback.
   - **2.2:** `stats()` reported in `/healthz` and summarised at shutdown.
   - **2.3:** bounded flush; cancel-and-count in `finally`; a closed flag; a done-callback.
   - **2.4:** `log_fields` guarded.
   - **2.5:** `safe_rollback` logs and adds a note.
   - **2.6:** `open_store` wraps migration and journal errors with SQLite's message.
   - **2.7:** store errors logged with their message, others with a traceback.
   - **2.8:** a WARN when `doctor` creates the store.
   - **2.9:** clear wording in `doctor` and in the start-up banner.
3. The cancelled insert counts as dropped and is re-raised.
4. `_install_break_handler` turns SIGBREAK into `server.should_exit`, and `_configure_logging` gives the `relay` logger a console handler. The end-to-end re-run shows exit 0 and the summary line.
5. The edit was redone with the Edit tool; ruff is clean.

## 11. Outstanding Issues

1. **`request_log` has no `request_id`** (the schema is as specified). Recommendation: add it in migration 2.
2. **Deferred minors:**
   - an abandoned leader's tokens are NULL in the log (the spend is in the flight record);
   - `start_run()` would detach the sink;
   - per-row commits;
   - `_apply` interpolates internal constants.
3. **Repositories for `api_key`, `chunk`, `eval_set`, `eval_question` and `grade` are not built.** The tables exist; the prompt asked for six repository groups. They arrive with V2-03, V2-10 and V2-12.
4. **Graceful Ctrl+C in an interactive console was not exercised by an automated test.** Ctrl+Break was, end to end.
5. **Carried over from V2-01:** the Dockerfile, `compose.yaml` and README quickstart.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| Migrations apply from empty and are idempotent | Met (tests 1, 2) |
| A restart with `cache_mode = "sqlite"` keeps the request log | Met (test, and a real two-session run) |
| `relay doctor` reports store status | Met (memory: INFO; sqlite: OK/WARN; failure: FAIL) |
| All 508 existing tests still pass | Met with one disclosed change: 507 unchanged and passing; 1 case retired and 1 narrowed because they asserted that sqlite is refused (§5.6) |
| No test opens a real file; store tests use `:memory:` | Met except the two owner-approved `tmp_path` tests (restart, WAL) |
| ruff clean | Met |
| No real Groq calls | Met (0) |
| `results.csv` and `evidence/` unchanged | Met (0 git changes; same SHA-256) |

## 13. Remaining Risks

- **One connection, one writer lock.** Fine for one process. Heavy concurrent writers in V2-03 ingestion will queue behind it.
- **SQLite on network filesystems** can break WAL. The store should live on a local disk.

## 14. Recommended Next Phase

Owner decisions first:
1. Whether migration 2 adds `request_id` to `request_log`.
2. Whether a persistent answer cache is wanted before V2-03.

Then **V2-03 — Collections and ingestion**. Nothing has been started.
