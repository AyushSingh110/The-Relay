# Phase V2-00 + V2-01 Implementation Report — Governance and Runnable Service

> **Phase ID:** `v2-00`, `v2-01`
> **Date:** `2026-11-18 20:50 IST`
> **Status:** `Completed` for every task in the owner's prompt. Not in this prompt and not done: the plan's Dockerfile, `compose.yaml` and README quickstart (§11).
> - Verified in Conda env `the-relay` (Python 3.12.16): 508 passed, 0 failed, 0 skipped (the 467 v1 tests plus 41 new); ruff clean.
> - **Real Groq calls: 0.**
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

From the owner's prompt, following `docs/IMPLEMENTATION_PLAN_V2.md` V2-00 and V2-01.

**V2-00**
1. List `aiosqlite`, `prometheus-client`, `opentelemetry-sdk` and `python-multipart` as allowed dependencies in `CLAUDE.md`.
2. Create `docs/DECISIONS.md` with the approval entry.
3. Record in `CLAUDE.md` that `docs/ARCHITECTURE_V2.md` is the design source of truth for v2, and that v1 evidence is frozen and read-only.

**V2-01**
4. Typed settings from `relay.toml` with environment overrides.
5. `relay.example.toml` that works with the stand-in and needs no key.
6. A `relay` CLI with `serve`, `doctor` and `ingest` (stub).
7. `POST /ask` keeps working, and all 467 tests still pass.

**Done when:**
- `relay serve` answers `POST /ask` for the three v1 papers;
- `relay doctor` reports clearly on the key and the index;
- the 467 tests pass;
- no secret appears in any output.

## 2. Implementation Summary

- **Governance.**
  - `CLAUDE.md` gains §2.1, the v2 source-of-truth order with v1 evidence frozen, and §4.1, the four approved dependencies (none installed; each is added to `pyproject.toml` in the phase that uses it). Nothing was removed.
  - `docs/DECISIONS.md` holds the approval entry, worded exactly as given.
  - The status lines of the two v2 documents now say `APPROVED` (2026-11-18).
- **Settings** (`src/relay/app/settings.py`).
  - Frozen Pydantic models: `server`, `lanes`, `guard` (RPM, TPM, RPD, TPD, daily safety fraction, concurrency), `queue` (patience median, tokens per answer, optional capacity, max active), `paths` (papers, index, models, store), `cache` (`off`, `memory`, `sqlite`) and `retrieval`.
  - Layers: defaults, then the TOML file, then `RELAY_<SECTION>__<KEY>` environment variables.
  - Unknown fields are errors, so a key-like field in the TOML file is rejected. Validation errors report field locations, never the rejected values.
- **Composition root** (`src/relay/app/compose.py`). The plan's composition-root file; `serve` needs it.
  - `open_runtime(settings, env)` builds the corpus digest, the retrievers, the provider (embedded stand-in, external stand-in or Groq), a ProviderGuard, an admission queue with Little's-law capacity, and the v1 P08 app.
  - Blocking loads run in a worker thread before the embedded stand-in starts.
  - A Groq lane needs `GROQ_API_KEY` and `MAX_REAL_CALLS ≥ 1`, which becomes a hard `RealCallBudget` for the process.
- **CLI** (`src/relay/cli.py`; `relay` console script in `pyproject.toml`).
  - `serve` runs the composed app under uvicorn.
  - `doctor` checks the corpus manifest, indexes, models, key presence (without printing it), the call cap, the lane profile, the cache mode and admission capacity, using the **same helpers as `serve`**.
  - `ingest` prints that it arrives in V2-03 and exits 2.
- **No v1 module changed.** `POST /ask` is the unchanged P08 endpoint.

## 3. Files Created

- `docs/DECISIONS.md` — v2 decision log with the V2-00 entry.
- `src/relay/app/__init__.py` — package marker.
- `src/relay/app/settings.py` — typed settings, TOML and environment loading.
- `src/relay/app/compose.py` — composition root.
- `src/relay/cli.py` — `relay serve | doctor | ingest`.
- `relay.example.toml` — defaults for the embedded, calibrated stand-in; no key needed.
- `tests/test_app_settings.py` (15), `tests/test_app_compose.py` (9), `tests/test_cli.py` (8), `tests/test_v2_01_review_fixes.py` (9).
- `reports/phase-v2-01-runnable-service.md` — this report.

## 4. Files Modified

- `CLAUDE.md` — added §2.1 and §4.1 only.
- `docs/ARCHITECTURE_V2.md`, `docs/IMPLEMENTATION_PLAN_V2.md` — status line only.
- `pyproject.toml` — added `[project.scripts] relay = "relay.cli:main"`.
- `tests/test_eval_runner.py` (v1, P07) — **one assertion fixed** in `test_rows_traces_grades_audit_and_summary`, a verified flaky-test bug (§9.4).
- `ENGINEERING_LOG.md` — one appended entry.
- **Unchanged:** `results.csv` (SHA-256 `8dfb10df…44efd`, as at P11) and everything under `evidence/` (`git status`: 0 changes).

## 5. Technical Decisions

All items below are **[PROPOSED]** until the owner accepts them.

1. **Default lane: the embedded, calibrated stand-in** (`cal-20261113T022403Z-3f687c6`, model `qwen/qwen3.8-27b`), so a fresh setup answers without a key.
2. **The default retrieval settings are all three v1 presets**, so any v1 `POST /ask` works. The v2 architecture's per-collection `small_fast` default arrives with collections in V2-03.
3. **V2-01 serves from the first lane only.** Routing is V2-09.
4. **`cache.mode = "sqlite"` is accepted by the settings but refused by `serve` and `doctor`** with "arrives in V2-02".
5. **Capacity is derived from TPM ÷ tokens per answer × patience median** (v1 §15), giving 1 with the defaults. Raising it is a settings choice.
6. **The Groq lane's real-call cap is `MAX_REAL_CALLS` from `.env`**, per process. `serve` refuses to start a Groq lane without it.
7. **Rulings made during execution** (each with its cost if wrong):
   - No worktree, branch or commits, because `CLAUDE.md` §5.1 reserves git for the owner. Cost: none.
   - `relay.example.toml` was created before the first settings test run, because a settings test parses it. Cost: none.
   - `compose.py` was added although the prompt listed only `settings.py`, the example and `cli.py`; the plan names it and `serve` needs it. Cost: one extra module.
   - **Relative paths resolve against the working directory**, as documented. Cost: running from another directory needs absolute paths.
   - **`RELAY_*` overrides come from the process environment only**, not `.env`, as documented. Cost: an override placed in `.env` is ignored.
   - No red-first runs while creating files, to keep the "green after every file" rule. The review fixes were done red-first.
   - The `doctor` test assertions were updated to the composition root's messages, because doctor now uses those rules.

## 6. Commands Actually Executed

```bash
git status --short ; git log --oneline -3 ; git tag --list          # no tags exist
python -c "pydantic_settings version; TomlConfigSettingsSource"     # 2.16.2, available
python -m pip show the-relay                                         # editable install
python (status-line edits of the two v2 docs)
timeout 900 python -m pytest -q        # after V2-00 docs: 467 passed
python -m ruff check ... ; pytest tests/test_app_settings.py ; full suite   # 15 passed; 482 passed
pytest tests/test_app_compose.py       # 1 failed (test bug, §9.1) -> 9 passed; full suite 491 passed
pytest tests/test_cli.py               # 8 passed
python -m pip install -e . --no-deps -q    # registers relay.exe; no packages installed
relay --help ; timeout 900 python -m pytest -q    # 499 passed
python scratchpad/v2_01_e2e.py         # doctor + serve + 3 questions (pre-review)
code-review skill (medium) on settings.py, compose.py, cli.py, relay.example.toml   # 10 findings
pytest tests/test_v2_01_review_fixes.py   # 9 failed (RED, before fixes)
(fixes) ; ruff check . ; pytest V2-01 test files   # 1 failed (old doctor assertion) -> updated
timeout 900 python -m pytest -q        # 508 passed
python scratchpad/v2_01_e2e.py         # fresh end-to-end after fixes
relay serve --port 0 ; relay ingest ; RELAY_PATHS__INDEX_DIR=data/no_such_indexes relay doctor
git status --short evidence results.csv ; sha256sum results.csv
timeout 900 python -m pytest -q ; python -m ruff check .             # final
```

`python` is `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe`; `relay` is that environment's `Scripts\relay.exe`.

## 7. Test Results

Full-suite results after each step:

| After | Result |
| :-- | :-- |
| V2-00 documents | 467 passed |
| `settings.py` and the example file | 482 passed |
| `compose.py` | 491 passed |
| `cli.py` and the entry point | 499 passed |
| Review fix pass | 508 passed |
| First final run, after the docs | **1 failed, 507 passed**: flaky v1 test (§9.4) |
| **Final**, after the flaky-test fix | **508 passed, 0 failed, 0 skipped**, twice in a row; ruff: All checks passed! The fixed test passed 30 of 30 isolated repeats. |

The 467 v1 tests are all inside the 508. The 41 new tests are 15 settings, 9 compose, 8 CLI and 9 review-fix tests. The 9 review-fix tests were run before the fixes and **all 9 failed**; after the fixes all pass.

## 8. Actual Output and Generated Artifacts

**`relay doctor`** (built-in defaults, real `.env`):

```
[  OK] corpus manifest: data\papers\manifest.json (3 papers)
[  OK] index directory: data\indexes (3/3 presets)
[  OK] embedding models: data\models
[INFO] GROQ_API_KEY: set (not needed: no groq lane is configured)
[  OK] lane standin: embedded stand-in, model qwen/qwen3.8-27b, profile calibrated (cal-20261113T022403Z-3f687c6)
[  OK] cache mode: memory (-> exact)
[  OK] admission capacity: 1 (bound tpm, 5.3/min)
doctor: all required checks passed
```

With `RELAY_PATHS__INDEX_DIR=data/no_such_indexes`: `[FAIL] index directory: data\no_such_indexes not found; run scripts/build_indexes.py`, exit 1.

**`relay serve --port 8765`** as a separate process (final run) was ready in 3.1 s. Stand-in answers, 0 real calls:

| Question | Paper | HTTP | Time | Verdict | Cited |
| :-- | :-- | :-- | -: | :-- | :-- |
| q03 | Attention | 200 | 875 ms | `VERIFIED` | `1706.03762v7` |
| q10 | Moshi | 200 | 625 ms | `VERIFIED` | `2410.00037v1` |
| q12 | H-Net | 200 | 719 ms | `VERIFIED` | `2507.07955v2` |

**Other checks**
- `relay serve --port 0` → `relay serve: invalid relay settings: server.port: Input should be greater than or equal to 1`, exit 1.
- `relay ingest` → the V2-03 message, exit 2.
- **Secret check:** the doctor output, the serve log and all three responses were scanned for the configured key value, `gsk_` keys and bearer tokens. **None found.**

## 9. Errors and Root-Cause Analysis

1. **Test bug.** `model_copy(update={"cache": {...}})` with a plain dict made Pydantic emit a serialiser warning, which the suite turns into an error.
2. **Code-review findings** (medium, on the new files):
   1. `--port 0` raised a raw `ValidationError` traceback.
   2. A missing or bad stand-in profile raised `FileNotFoundError` or `ValidationError` tracebacks.
   3. A corrupt corpus manifest crashed `doctor`.
   4. `doctor` passed settings that `serve` rejects: no TPM/RPM, a model missing from the uncalibrated profile.
   5. The example file falsely said that omitting a limit disables it.
   6. ONNX and index loading blocked the event loop after the embedded stand-in had started, against `CLAUDE.md` §4.
   7. Relative paths resolve against the working directory.
   8. `RELAY_*` placed in `.env` is ignored.
   9. Environment-override conflict detection depends on iteration order.
   10. `doctor` duplicated logic owned by `compose`.
3. **An old `doctor` test** asserted the previous message wording, which changed with finding 2.4.
4. **A flaky v1 test** (P07, `tests/test_eval_runner.py::test_rows_traces_grades_audit_and_summary`) failed once, with no code change since the previous 508-pass run: `assert (3.566 - 3.45) <= 0.05`.
   - Root cause: the test required the first-byte stamp and the end of the client timer, two separate clock reads, to be at most 0.05 ms apart.
   - A scheduler or GC pause between them breaks that arbitrary bound. The runner code is correct.

## 10. Fixes Applied

1. The test builds settings through `model_validate`.
2. Fixes for the review findings:
   - **2.1:** `settings.with_overrides` converts errors to `ConfigurationError`.
   - **2.2:** `standin_profile` wraps load errors.
   - **2.3:** `doctor` uses `load_corpus_manifest` and reports a FAIL.
   - **2.4 and 2.10:** `doctor` calls `compose.standin_profile`, `cache_mode` and `capacity_derivation`.
   - **2.5:** corrected the comment; it now names `RELAY_GUARD__RPD=null`.
   - **2.6:** the corpus digest and retrievers load with `asyncio.to_thread` before the stand-in starts.

   Each has a test in `tests/test_v2_01_review_fixes.py` that failed before its fix and passes after.
   - **2.7 and 2.8:** documented as rulings (§5.7).
   - **2.9:** deferred as minor (§11).
3. The assertions were updated to the shared messages.
4. The tolerance was replaced by the ordering the test means: `server_total_ms ≤ first_token_ms ≤ total_ms`. The runner stamps the first byte after the server spans finish and before the client timer stops. Validated by 30 of 30 isolated repeats and two consecutive full-suite runs (508 passed each).

## 11. Outstanding Issues

1. **The `v1-submission` git tag does not exist** (`git tag --list` is empty). `docs/DECISIONS.md` refers to it as instructed. **The owner needs to create it**: a git operation, reserved to the owner by `CLAUDE.md` §5.1.
2. **Not done (outside this prompt): the V2-01 plan's Dockerfile, `compose.yaml` and README quickstart.** The plan's "docker compose up works on a clean machine" criterion is therefore open. Docker itself is not yet on the approved dependency list.
3. **Deferred minor:** environment-override conflict detection depends on iteration order (review finding 2.9).
4. **Embedded stand-in access-log lines appear in the `serve` log**, because uvicorn's logging configuration is process-wide. Cosmetic.
5. **On Windows, `TerminateProcess` stops `serve` with exit 1**, as my test script did. Graceful Ctrl+C in a console was not exercised by an automated test.
6. **`relay.exe` is on PATH only when the conda environment is activated**; pip warned that `Scripts` was not on PATH in the non-activated shell.
7. **The plan's "load harness smoke run still passes" criterion was not run in this phase.** The harness tests within the 508 passed, but `scripts/run_load_tests.py --scenario smoke` was not executed.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| Allowed dependencies listed in `CLAUDE.md` | Met (§4.1) |
| `docs/DECISIONS.md` with the V2-00 entry | Met (tag missing, §11.1) |
| `CLAUDE.md` names `ARCHITECTURE_V2.md` for v2 design; v1 evidence frozen | Met (§2.1) |
| Typed settings from `relay.toml` with environment overrides | Met |
| `relay.example.toml` works with the stand-in, no key | Met |
| `relay serve`, `doctor` and `ingest` (stub) | Met |
| `relay serve` answers `POST /ask` for the three v1 papers | Met (3/3 `VERIFIED`) |
| `relay doctor` reports clearly on the key and the index | Met (including a missing index → FAIL) |
| All 467 tests pass | Met (inside 508 passed) |
| No secrets in any output | Met (scan: none) |
| Plan's Docker/compose/README tasks | **Not done** (not in this prompt) |

## 13. Remaining Risks

- **The derived capacity of 1** with the default inputs makes a second concurrent request queue or be shed. That is v1 behaviour, but surprising for a first-time user. Revisit in V2-10.
- **The default profile path points into `evidence/`** (read-only use). A future clean-up of evidence could break the default.

## 14. Recommended Next Phase

Owner actions first:
1. Create the `v1-submission` tag.
2. Decide whether the Docker/compose/README part of V2-01 is done now, which needs Docker added to the approved dependencies.

Then **V2-02 — Metadata store**. Nothing has been started.
