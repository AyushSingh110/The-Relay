# Phase 01 Implementation Report — Foundation, Contracts, Run Directories and Aggregation

> **Phase ID:** `p01`
> **Date:** `2026-11-11 13:59 IST`
> **Status:** `Completed` — verified 2026-11-11 14:16 IST in Conda env `the-relay` (Python 3.12.16): 73 passed, ruff clean
> **Owner Approval:** `Owner Approved (2026-11-12)`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P01, build:

- the package skeleton;
- typed configuration;
- frozen, strict Pydantic contracts;
- a domain error hierarchy;
- client and server timing helpers;
- run-directory and manifest management;
- deterministic, validated, duplicate-rejecting aggregation of canonical runs into `results.csv`, through `scripts/build_results.py`.

The phase also records the owner's final decisions, without installing dependencies.

## 2. Implementation Summary

- **Contract (`relay.schemas.ResultRow`).** This is the single definition of the `results.csv` schema.
  - Columns 1–12 match `ASSIGNMENT.md` exactly, then come the 17 appended columns from ARCH §13.3.
  - All enumerations are `Literal` types, and the model is `strict` and `frozen`, so no silent coercion and no unknown columns.
  - The model validator enforces the ARCH §13.4 outcome rules, the span invariant, the provider/token rules, and the owner's probe-row rules.
- **CSV codec (`relay.telemetry.csv_codec`).**
  - Blank cells mean `None`, never `0`/`-1`.
  - Strict parsing: `"1.0"` is not an int, `"True"` is not a bool, `nan` is rejected.
  - Fixed formats: ms to 3 decimals, USD to 8 decimals, timestamps as `YYYY-MM-DDTHH:MM:SS.mmmZ`, `\n` line endings.
- **Spans (`relay.telemetry.spans`).**
  - `ServerSpanTracker` assigns every nanosecond to exactly one of waiting/working/retrying, so the three spans sum to the total by construction. It records the provider's first token once.
  - `ClientTimer` records the client-observed start, first byte and total.
- **Run store (`relay.telemetry.run_store`).**
  - Run IDs look like `<probe|cal|exp>-<YYYYMMDDTHHMMSSZ>-<git short sha>`, and directories are created exclusively, never reused.
  - The manifest starts as `in_progress` and is written atomically.
  - Probe and experiment runs get a header-only `rows.csv` at creation.
  - Rows are checked against the run's ID, its declared scenarios and its source, and duplicate request IDs within a run are rejected.
  - Trace records and config snapshots are redacted before they're written.
  - Writes are serialised with a lock. A context manager marks the run `aborted` if an exception escapes.
- **Aggregation (`relay.telemetry.aggregation` + `scripts/build_results.py`).**
  - Reads only the runs listed in `evidence/canonical_runs.json`, and rejects malformed or duplicate selections.
  - Requires every run to be `complete` and of a kind that records rows.
  - Re-validates every row, then rejects any duplicate `request_id` across runs.
  - Output is sorted by `(run_id, request_id)`, so it doesn't depend on the order of the canonical list.
  - Refuses to replace a differing `results.csv` unless `--replace` is given. `--check` writes nothing.
- **Config (`relay.config`).**
  - `pydantic-settings`. Defaults are offline: `PROVIDER_MODE=scripted`, `MAX_REAL_CALLS=0`.
  - The key is a `SecretStr`, required only when `PROVIDER_MODE=groq`.
  - Validation errors are re-raised as `ConfigurationError` naming the fields only, never their values.
  - No queue size, Groq limits or prices are defined here, because none have been measured.
- **Errors and redaction.** `relay.errors` provides the hierarchy plus `sanitize_error` (class name and status code only). `relay.redaction` strips Groq keys, bearer tokens, sensitive header/key names and `org_…` IDs.
- **Test guard.** `tests/conftest.py` blocks every non-loopback socket connection, so the offline-test rule is enforced, not just promised.

## 3. Files Created

- `.gitignore` — ignores `.env`/`.env.*` (keeps `.env.example`), caches, `scratch/`, PDFs and indexes; deliberately **does not** ignore `results.csv` or `evidence/`.
- `pyproject.toml` — setuptools `src` layout, runtime and dev dependencies (no tiktoken), plus pytest config (`filterwarnings=error`) and ruff config.
- `.env.example` — every setting with a safe default and no values.
- `README.md` — setup, check and results commands.
- `evidence/README.md` — the evidence layout and its rules.
- `evidence/canonical_runs.json` — `{"schema_version": 1, "runs": []}`.
- `src/relay/__init__.py` — package and version.
- `src/relay/config.py` — `Settings`, `load_settings`.
- `src/relay/errors.py` — the error hierarchy and `sanitize_error`.
- `src/relay/redaction.py` — `redact_secrets`, `redact_text`.
- `src/relay/schemas.py` — `ResultRow`, `RunManifest`, the vocabularies and `ERROR_CODES_BY_STATUS`.
- `src/relay/telemetry/__init__.py`
- `src/relay/telemetry/spans.py` — `ServerSpanTracker`, `ClientTimer`.
- `src/relay/telemetry/csv_codec.py` — row encode/decode and `read_rows`/`render_csv`.
- `src/relay/telemetry/run_store.py` — `create_run`, `RunHandle`, `load_manifest`, `run_dir_for`, `current_git_state`.
- `src/relay/telemetry/aggregation.py` — `build_results`, `collect_rows`, `load_canonical_runs`.
- `scripts/build_results.py` — the CLI. Exit codes: 0 ok, 1 validation failure, 2 overwrite refused.
- `tests/__init__.py`, `tests/conftest.py` (network guard, `make_row`), `tests/factories.py` (valid-row builders).
- `tests/test_schemas.py` — 22 test functions; parametrisation adds cases.
- `tests/test_foundation.py` — 18 test functions; parametrisation adds cases.
- `tests/test_run_store_and_aggregation.py` — 23 test functions.
- `reports/phase-01-foundation-contracts.md` — this report.

(Function counts from `grep -c '^def test_'`. Pytest's collected count, including parametrised cases, will be the authoritative number.)

## 4. Files Modified

- `ARCHITECTURE.md`:
  - status set to `APPROVED BASELINE`;
  - §7.3(a): every factual sentence must carry a citation (owner decision);
  - §13.2: probe-row rule (owner decision), plus a note that the harness joins client and server timing and aggregation only validates;
  - §13.4: verifier-refusal status marked owner-decided, three added error codes, and the enforced-rules list;
  - §16: items 1, 2, 3 and 9 marked resolved.
- `ENGINEERING_LOG.md`: P01 entry appended.

## 5. Technical Decisions

1. **`Literal` types instead of `Enum`.** These work cleanly with Pydantic strict mode, and the string values match CSV cells exactly.
2. **Strict validation plus an explicit codec.** CSV text is converted to typed values by `csv_codec.parse_value`, then validated in strict mode. Pydantic never coerces types on its own.
3. **Invariants enforced on every row:**
   - `ok` ⇔ `first_token_ms` set;
   - non-`ok` rows need a status-appropriate `error_code` and have blank `first_token_ms` and grade;
   - verdict `BLOCKED` ⇔ `citation_verification_failed`;
   - span invariant (±1 ms), with the four span fields all set or all blank;
   - no provider call ⇒ 0 tokens, 0 retries, `usage_source=none`;
   - cache hit, coalesced and `EARLY_REFUSAL` ⇒ no provider call;
   - `probe_*` ⇒ `source=real`, `retrieval_setting=none`, blank grade, `provider_called=true`;
   - `request_id` must be prefixed by its `run_id`.
4. **Daily-exhausted notice and `first_token_ms`.** A daily-exhausted notice isn't an answer, so the row has `status=rate_limited` and blank `first_token_ms`. `first_token_ms` measures the first byte of an *answer*, including refusals.
5. **Run ID collisions.** Two runs of the same kind started in the same second at the same commit would collide. Creation fails loudly (`RunAlreadyExistsError`) rather than reusing the directory.
6. **The client/server join happens in the harness** when it writes each row (P10). Aggregation only validates and combines complete rows, because the server trace format doesn't exist until P08. Documented in ARCH §13.2.
7. **Overwrite refusal.** This honours CLAUDE §5.14 (never overwrite `results.csv`): regenerating requires an explicit `--replace`.
8. **Additions beyond the plan's file list:** `relay/redaction.py`, needed by both the manifests and the P02 probe evidence; and three test files instead of one, for readability.
9. **`pytest` runs with `filterwarnings = error`**, so warnings can't be silently ignored (CLAUDE §5.8). This may surface third-party deprecation warnings; if so, we deal with each one explicitly.

## 6. Commands Actually Executed

By Claude:

```bash
git status --short; git log --oneline -3; ls -la
git diff --stat HEAD -- ARCHITECTURE.md IMPLEMENTATION_PLAN.md ASSIGNMENT.md   # no output (docs == HEAD)
git check-ignore -v .env; echo $?                                              # exit 1 → .env NOT ignored (before .gitignore)
python -m py_compile src/relay/*.py src/relay/telemetry/*.py scripts/build_results.py tests/*.py
#   → "py_compile OK" — Anaconda BASE Python 3.13.5, syntax check only; NOT the-relay env, NOT tests
awk 'length > 100 {...}' ...   # → 3 over-long lines; fixed
date "+%Y-%m-%d %H:%M %Z"      # → 2026-11-11 13:59 IST
git status --short             # → .env no longer listed (now ignored)
```

**For the owner to run** (Anaconda Prompt, or PowerShell with conda initialised), from the repository root:

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
python -c "import sys; print(sys.version); print(sys.executable)"

# Install project + dev dependencies into the-relay
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check

# Tests and lint
python -m pytest -v
python -m ruff check .

# P01 validation
git check-ignore -v -n .env .env.example results.csv evidence/canonical_runs.json evidence/experiments/exp-x/rows.csv scratch/x.csv
git status --short
python scripts/build_results.py --check
python scripts/build_results.py --output scratch/p01_results_a.csv
python scripts/build_results.py --output scratch/p01_results_b.csv
python -c "import pathlib as p; a=p.Path('scratch/p01_results_a.csv').read_bytes(); b=p.Path('scratch/p01_results_b.csv').read_bytes(); print('IDENTICAL' if a==b else 'DIFFERENT', len(a), 'bytes')"
```

**Expected results for the validation step:**
- `check-ignore`: `.env` and `scratch/x.csv` show a matching rule. `.env.example`, `results.csv` and the `evidence/...` paths show `::` (not ignored).
- `--check`: `{"outcome": "checked", "rows": 0, "runs": 0, ...}`, exit 0.
- The two scratch builds come out `IDENTICAL`. Because the canonical list is still empty, each is a header-only file.

### 6b. Verification commands executed by Claude (owner request, 2026-11-11)

The owner had already installed the project (`pip show`: `the-relay 0.1.0`, editable at the repo; pydantic 2.14.3, pydantic-settings 2.16.2, pytest 9.2.0, ruff 0.17.4). Claude installed nothing. All commands below used `C:\Users\ASUS\anaconda3\envs\the-relay\python.exe` (Python 3.12.16).

```bash
python -m pytest -q                      # run 1 → 1 failed, 72 passed in 0.65s
python -m pytest -v                      # run 2 (after fix 4) → 73 passed in 0.49s
python -m ruff check .                   # run 2 → 2 errors (E501, SIM300)
python -m pip check                      # → No broken requirements found.
git check-ignore -v -n .env .env.example results.csv evidence/canonical_runs.json evidence/experiments/exp-x/rows.csv scratch/x.csv
python scripts/build_results.py --check  # → {"outcome": "checked", "rows": 0, "runs": 0, "would_change": true}, exit 0
python -m pytest -q                      # run 3 (final, after fixes 5–6) → 73 passed in 0.56s
python -m ruff check .                   # run 3 (final) → All checks passed!
rm -f scratch/p01_results_a.csv scratch/p01_results_b.csv
python scripts/build_results.py --output scratch/p01_results_a.csv   # → outcome "written"
python scripts/build_results.py --output scratch/p01_results_b.csv   # → outcome "written"
# byte comparison → IDENTICAL 357 bytes
```

## 7. Test Results

Final run, after the last edit:

- `python -m pytest -q`: **73 passed, 0 failed, 0 skipped** in 0.56 s. Warnings are treated as errors, and none occurred.
- `python -m ruff check .`: **All checks passed!** (exit 0).
- `python -m pip check`: **No broken requirements found.**

Earlier runs, kept for the record:

- Run 1: 1 failed (`test_key_never_appears_in_repr_snapshot_or_errors`), 72 passed.
- Run 2: 73 passed; ruff found 2 errors.

## 8. Actual Output and Generated Artifacts

- `git check-ignore -v -n` output:
  - `.gitignore:2:.env  .env` → ignored.
  - `.gitignore:4:!.env.example  .env.example` → matched only by the negation rule, so it is **not** ignored.
  - `::  results.csv`, `::  evidence/canonical_runs.json`, `::  evidence/experiments/exp-x/rows.csv` → not ignored.
  - `.gitignore:20:scratch/  scratch/x.csv` → ignored.
- `build_results.py --check` with the empty canonical list: 0 runs, 0 rows, exit 0. It wrote nothing; `results.csv` is still absent at the repo root.
- Two fresh builds into `scratch/` were **byte-identical, 357 bytes**: a header-only file whose header is the 29-column contract, starting with the 12 mandatory columns in assignment order.
- No real or stand-in runs exist yet. `evidence/canonical_runs.json` is still an empty selection.
- Observed at phase start: `.env` was untracked and not ignored. After `.gitignore` was written, `git status --short` no longer lists it. `.env` contents were not read.

## 9. Errors and Root-Cause Analysis

1. **Three lines over the 100-character limit** (found by the `awk` scan). Cause: long literal messages. Ruff `E501` would have failed.
2. **Placeholder code in `load_canonical_runs` during drafting.** Cause: an unfinished edit. Found on self-review before handoff.
3. **Ruff import-sorting risk.** With `src = ["src", "tests", "scripts"]`, `tests.factories` might have been classed as third-party, which would trigger `I001`. Cause: how ruff detects first-party modules. This is a predicted risk; it hasn't been observed.
4. **Test failure, observed in pytest run 1.** In `test_key_never_appears_in_repr_snapshot_or_errors`, `snapshot["groq_api_key_present"]` was `'[REDACTED]'` instead of `True`. Root cause: `Settings.manifest_snapshot()` named its boolean flag `groq_api_key_present`, and `redact_secrets` masks every key containing `api_key`. The redactor is correct to be conservative; the flag's name was the defect. Effect: manifests would have lost the "is a key configured" fact, though no secret was ever exposed.
5. **Ruff E501, observed in ruff run 2,** at `tests/test_run_store_and_aggregation.py:254`. Root cause: my earlier fix (error 1) edited a *different* long line in that file, and I didn't re-scan before handing over.
6. **Ruff SIM300, observed in ruff run 2,** at `tests/test_schemas.py:21`. A Yoda-style comparison (`CONSTANT == value`).

## 10. Fixes Applied

1. Reformatted or shortened the three lines. The scan wasn't re-run after the fix, so the owner's `ruff check .` will confirm it.
2. Replaced the placeholder with real run-ID format validation (`CanonicalRunsError`), covered by `test_malformed_canonical_list_rejected`.
3. Set ruff `src = ["src", "."]` so that both `relay` and `tests` resolve as first-party. Confirmed: the final ruff run reports no `I001`.
4. Renamed the flag to `groq_credential_configured` in `src/relay/config.py` and updated the test. The redaction rules are unchanged. Confirmed: the test passes in runs 2 and 3, and it still asserts that the fake key never appears in the repr, the snapshot or error text.
5. Moved the canonical-file construction onto its own line (`canonical = _write_canonical(...)`). Confirmed by ruff run 3.
6. Rewrote the comparison as `expected == MANDATORY_COLUMNS`. Confirmed by ruff run 3.

## 11. Outstanding Issues

- **Editor diagnostics:** VS Code/Pyright reports `Import "relay..." could not be resolved`. Cause: the editor is using a different interpreter than the `the-relay` env, where the package is installed and the tests pass. Fix on the owner's side: *Python: Select Interpreter* → `anaconda3\envs\the-relay\python.exe`. This doesn't affect code or tests.
- The IDE markdown-lint warnings on the docs are cosmetic and unfixed.
- The full dependency set installed cleanly (`pip check` clean). The P02–P04 libraries were installed but not exercised in this phase.

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P01) | Status | Evidence |
| :--- | :--- | :--- |
| `.gitignore` ignores `.env`, `scratch/`, PDFs, indexes, caches; not `results.csv`/`evidence/` | PASS | `git check-ignore -v -n` output (§8) + `test_gitignore_protects_secrets_but_keeps_evidence` |
| Columns 1–12 exact, then appended columns | PASS | `test_first_twelve_columns_match_assignment_exactly`; header in §8 |
| `answer_grade` only the four labels or blank | PASS | `test_allowed_answer_grades`, `test_unsupported_answer_grades_rejected` |
| Enums for status, cache_status, verdict, usage_source | PASS | `Literal` types; `test_strict_types_do_not_coerce` |
| Blank (not 0/-1) for missing values | PASS | `test_blank_cells_for_missing_values_never_zero` |
| Span invariant ±1 ms | PASS | `test_span_invariant_enforced`, `test_server_spans_sum_to_total_by_construction` |
| Aggregation: canonical-only, rejects duplicates, validates, deterministic | PASS | 13 aggregation/CLI tests + fresh byte-identical builds (357 bytes) |
| Manifest never serialises secrets | PASS | `test_secrets_never_reach_manifest_or_traces`, `test_key_never_appears_in_repr_snapshot_or_errors` |
| Owner decisions encoded (probe rows, blocked = ok) | PASS | `test_probe_rows_follow_owner_rules`, `test_verification_failure_refusal_is_ok_and_gradable`, `test_probe_rows_are_aggregated` |
| `pytest` and `ruff check .` pass | PASS | final run: 73 passed; ruff "All checks passed!" |

## 13. Remaining Risks

- **Strict mode vs ints for float fields.** Later phases that construct rows in Python must pass floats for ms fields if Pydantic's strict mode rejects ints. The tests use floats; any problem will show up in P02+ and the fix is local.
- **Same-second run collisions** (decision 5). They're unlikely in practice, but a harness that starts runs in a loop would need to wait between them, or the run ID format would need a sequence suffix.
- **New error codes.** Each one requires a synchronised change to `ERROR_CODES_BY_STATUS` and ARCH §13.4. This is deliberate friction.

## 14. Recommended Next Phase

P01 is verified, pending owner review. The next phase is **P02 — Provider Client and Probe Tooling** (offline only). It needs no Groq key; the live probes are P02-LIVE, under separate approval. Waiting for owner authorisation.
