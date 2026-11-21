# Fix: CI workflow rejected by GitHub

**Status:** completed, pending owner review. **Date:** 2026-11-21. **Real Groq calls:** 0.

## 1. Objective

The first CI run, on commit 88296df (V2-12), failed after 0 s. The goal is to find the cause and make every CI job pass on a fresh checkout. Nothing is committed.

## 2. Implementation Summary

There were two problems. Only the first one was visible, because it stopped any job from running.

1. **The workflow file was invalid.** `ci.yml` set `RELAY_PATHS__STORE_PATH: ${{ runner.temp }}/...` in the eval-gate job's `env`. GitHub does not allow the `runner` context there, so it rejected the whole file and no job started. The variable now lives in the step that needs it.
2. **Four tests depended on gitignored local data.** Once the file parses, the backend job would have failed: four `relay doctor` tests assumed the built `data/indexes` and downloaded `data/models`, which a fresh checkout doesn't have. Those tests now create an installed-looking index and model directory under `tmp_path`. That is what doctor checks: one index manifest per preset, and one `models--*` folder. The corpus manifest is still the tracked repository one, so the assertions are unchanged.

## 3. Files Created

`reports/fix-ci-workflow.md`

## 4. Files Modified

- `.github/workflows/ci.yml`
- `tests/test_cli.py`
- `tests/test_store_integration.py`
- `ENGINEERING_LOG.md` (appended)

## 5. Technical Decisions

- **The tests were fixed, not doctor.** Doctor correctly fails where no index or model is installed; only the tests assumed a developer machine.
- **No job was weakened.** No test was skipped, no step removed, no `continue-on-error` added.

## 6. Commands Actually Executed

- `gh run list` and `gh run view 44904000059`: "This run likely failed because of a workflow file issue."
- `actionlint`, installed as `actionlint-py` in the conda env:
  - on the committed file: `ci.yml:46:36: context "runner" is not allowed here`, exit 1;
  - on the fixed file: exit 0.
- **A clean-clone simulation.** I cloned HEAD into the scratchpad and overlaid the working-tree changes, so no untracked data was present. Then I ran each job's steps:
  - **backend:** `ruff check .` and `pytest -q`;
  - **eval-gate:** `relay eval run --replay ...` and `relay eval gate ...`;
  - **web:** `npm ci`, `tsc`, `eslint` and `jest`.
- `bash .claude/skills/relay-verify/verify.sh`, then a second `pytest -q`.

## 7. Test Results

- **Clean clone, before the test fix:** 4 failed, 992 passed, 11 skipped. The failures were the doctor tests, which reported `data\indexes not found` and `data\models has no cached models`.
- **Clean clone, after:**
  - **backend:** ruff clean; pytest **996 passed, 11 skipped**. The skips are tests that need the real PDFs, indexes or the L4 model, and skip by design.
  - **eval-gate:** run exit 0; `gate: PASS`, with the candidate equal to the baseline (grounded 73.3%, refusal precision 16.7%).
  - **web:** `npm ci` 0, tsc 0, eslint 0, Jest **60 passed** (9 suites).
- **Working tree:**
  - `verify.sh` gave 1006 passed and 1 failed. The failure was `test_v2_07_l4.py::test_scoring_does_not_block_the_event_loop`, the known 50 ms timing flake: it passed 5 of 5 alone, and its code is unchanged.
  - A full rerun gave **1007 passed**.
  - ruff clean; Jest 60; tsc and eslint 0; `results.csv` sha256 `8dfb10dfaadb`; 0 evidence changes.

## 8. Actual Output and Generated Artifacts

None. The simulation clone lives in the scratchpad, outside the repository.

## 9. Errors and Root-Cause Analysis

- **The `runner` context in a job-level `env`.** It is available only inside steps. The workflow passed my local checks because nothing validated it; no CI run had happened before the commit.
- **The doctor tests' environment assumption.** They passed locally because `data/` is populated on this machine.

## 10. Fixes Applied

See §2.

## 11. Outstanding Issues

- **Not yet run on Linux:** the simulation ran on Windows, while CI runs on ubuntu-latest. Linux-only differences (case-sensitive paths, for example) are not covered until GitHub runs it.
- **The L4 timing test** stays flaky under load on this machine. In CI it is skipped, because the model isn't downloaded there.

## 12. Acceptance-Criteria Status

- The workflow file is valid (actionlint exit 0).
- All three jobs pass on a clean checkout (simulated locally).

## 13. Remaining Risks

The first real GitHub run may still surface Linux-specific issues.

## 14. Recommended Next Action

When you commit and push, check the run with `gh run watch`.
