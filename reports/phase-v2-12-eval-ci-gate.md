# Phase V2-12: Evaluation system and CI gate

- **Date:** 2026-11-21 IST
- **Status:** completed, pending owner review.
- **Real Groq calls:** 0 (cap 60, none approved).
- **Evaluation questions written or edited by Claude Code:** none (CLAUDE.md §5.15). The
  owner's 21 v1 questions are imported verbatim.
- **Frozen evidence:** `results.csv` sha256 still starts with `8dfb10dfaadb`. `evidence/`
  was read, never written; `git status` shows no change under it.

## 1. Objective

Quality claims that hold up (ARCHITECTURE_V2 §10):

- evaluation sets, questions and grades in the store, imported from v1;
- a review screen for the owner's grading;
- three graders side by side: rubric, entailment, owner;
- a CI workflow with a stand-in pipeline run against an accepted baseline, which a deliberately
  broken prompt must fail;
- a budgeted real-evaluation command.

## 2. Implementation Summary

**Store (migration 9).**

- `eval_question` gets `source_id` (the owner's id, `q01`) and `spec` (the full owner
  question as JSON).
- New tables: `eval_run` (kind `imported`, `standin`, `replay` or `real`; lane, model,
  preset, source run, status) and `eval_answer` (answer text, refusal, verdict, citations,
  and the passages it was given).
- `grade` gets `eval_answer_id` and `verdict`.
- Repositories in `store/eval_repos.py`:
  - `EvalSetRepo`: create, get, find, list_for_collection;
  - `EvalQuestionRepo`: create_batch, list_for_set, get;
  - `EvalRunRepo`: create, get, set_status, find_by_source, list_for_set;
  - `EvalAnswerRepo`: create, get, list_for_run;
  - `GradeRepo`: record_grade, list_for_question, list_for_run, get_summary_by_run.
- The V2-10 collection delete now also removes the collection's eval runs, answers and
  their grades (review fix, §9).

**Import (`relay eval import-v1`).** It loads:

- the 21 questions from `eval/questions.json` into eval set `v1` of `v1-papers`;
- from the two canonical v1 eval runs, **80 answers** with the passages each was given
  (from the traces), the verifier verdicts, and the **80 rubric grades** from `grades.json`.
  Owner `final` grades were imported too: there were none.

These form 6 runs: 3 models × `balanced_rerank`, and `qwen` × 3 presets. Importing twice
adds nothing.

**Graders** (`eval/graders.py`):

- **Rubric:** v1's `propose_grade`, unchanged (required facts, gold page cited, no forbidden
  claims, refusal on unanswerable questions). For a question outside the v1 three-paper
  corpus, the same fields are read without v1's paper list.
- **Entailment:** the L4 windowed scorer. Each sentence's score is the best score over the
  passages it cites; the answer's score is the mean. Refusals are not scored. No verdict:
  L4 stays advisory.
- **Human:** CORRECT, PARTIAL, INCORRECT or UNANSWERABLE_CORRECT, with notes.

**Runs** (`eval/runner.py`, `eval/standin.py`).

- `run_eval` asks every question through a pipeline pinned to one lane, then stores the
  answer, its rubric grade, and its entailment grade when the L4 model is installed.
- `ReplayRetriever` serves v1's recorded passages per question.
- `replay_pipeline` runs the stand-in in process over `httpx.ASGITransport`. It uses the
  calibrated profile, read from `evidence/`, with faults and quota limits off and time
  scaled down.
- The result needs no index, no embedding model and no network, and it is deterministic:
  21 questions in about 0.5 s.

**Summary and gate** (`eval/report.py`).

- The side-by-side summary covers:
  - rubric correctness, grounded rate and refusal precision;
  - the entailment mean;
  - the owner's reviewed count and verdicts;
  - rubric-versus-owner agreement;
  - an L4 calibration sweep at thresholds 0.1 to 0.9 (precision, recall and false-block
    rate against the owner's grades), shown once owner grades exist.
- `gate` fails when:
  - correctness **or grounded rate** drops more than 2 points; or
  - refusal precision on unanswerable questions falls below 100%, or below the baseline's
    own value if the baseline was already under 100%.

**CLI.**

- `relay eval import-v1 [--with-entailment]`;
- `relay eval run [--collection C] [--set S] [--preset P] [--lane L] [--replay [RUN]] [--summary-out F] [--real-calls N]`.
  A Groq lane is refused unless `--real-calls N` gives the owner's approved budget. This is
  the "budgeted real evaluation command" of plan task 5; it was not run.
- `relay eval report --run R [--json F]`;
- `relay eval gate --baseline RUN|F --candidate RUN|F`, which exits 1 on a regression.

**API** (`/v1/eval`, tenant-scoped through the set's collection; another tenant gets 404):

- `GET /sets`, `GET /sets/{id}/questions`, `GET /sets/{id}/runs`;
- `POST /runs`: admin only; runs in the background and returns 202. A lane on a real
  provider gets 403 `real_runs_disabled`.
- `GET /runs/{id}`: the answers with their passages and grades;
- `GET /runs/{id}/summary`;
- `POST /grades`: admin only; the owner's grade.

**UI.** The **`/eval`** review screen, with an "Evaluation" link in the navigation:

- pickers for the eval set and the run;
- the **Graders side by side** table;
- one card per answer: the question, the answer with its citation chips (a click shows the
  passage), all retrieved passages, the rubric verdict and reasons, the entailment score and
  the owner's verdict;
- one-click **Correct / Partial / Incorrect** (or **Correct refusal** for an unanswerable
  question), with notes. The totals refresh after each grade.

**CI.**

- `.github/workflows/ci.yml` has three jobs: backend (ruff, pytest), web (tsc, eslint, jest)
  and eval-gate (`relay eval run --replay`, then `relay eval gate` against
  `eval/baselines/standin-replay-small_fast.json`).
- The baseline was measured by `relay eval run --replay`, and a test checks that a fresh
  replay reproduces it exactly.

## 3. Files Created

- `src/relay/store/eval_repos.py`
- `src/relay/eval/graders.py`, `loader.py`, `runner.py`, `standin.py`, `report.py`,
  `commands.py`
- `src/relay/api/routes/eval.py`
- `web/src/pages/EvalReviewPage.tsx`
- `.github/workflows/ci.yml`
- `eval/baselines/standin-replay-small_fast.json`
- Tests:
  - `tests/test_v2_12_eval.py`: 11
  - `tests/test_ci_gate.py`: 8
  - `web/src/__tests__/eval.test.tsx`: 5
- `reports/phase-v2-12-eval-ci-gate.md` (this file)

## 4. Files Modified

- **Store:** `src/relay/store/migrations.py` (migration 9); `src/relay/store/__init__.py`
  (the 5 repositories); `src/relay/store/repositories.py` (`delete_cascade` covers the eval
  tables).
- `src/relay/orchestrator/pipeline.py`: `Pipeline.with_generator`.
- `src/relay/cli.py`: `relay eval`.
- `src/relay/app/compose.py`: mounts `/v1/eval`; tasks are cancelled on shutdown.
- **Web:** `web/src/api.ts` (eval types and methods), `web/src/router.tsx`, `web/src/App.tsx`,
  `web/src/styles.css`, `web/src/test/fakeApi.ts`.
- **Tests:**
  - `tests/test_store_migrations.py`: the expected tables now include `eval_run` and
    `eval_answer`;
  - `tests/test_v2_10_tenants.py`: the three fairness tests now measure the queue's
    admission order (§9).
- `src/relay/app/static/*`: the rebuilt UI.

## 5. Technical Decisions

These are my rulings. Each says what it costs if wrong.

1. **Grounded rate added to the gate.**
   - The stand-in answers by extracting sentences from the passages. It almost never states
     the rubric's required facts word for word, so its rubric correctness is 0%. Measured on
     the replay: 0 of 15 answerable.
   - A gate on correctness alone could never see a stand-in regression. The prompt's rule
     is kept, and the grounded rate (VERIFIED, with a gold page cited) is gated with the
     same 2-point rule.
   - This is ARCHITECTURE_V2 §10.3's stand-in "plumbing" check. Real quality needs a real
     run, which needs the owner's approval.
   - Cost if wrong: one more gated metric.
2. **The refusal-precision floor follows the baseline.** The stand-in baseline is 16.7%
   (1 of 6) on `small_fast`, because it answers unanswerable questions from loosely related
   passages. A fixed 100% floor would fail every stand-in run, so the floor is the
   baseline's own value when the baseline is under 100%. For a real model's baseline at
   100% (v1 qwen), the floor is 100%.
3. **The corrupted prompt also drops the passages.** The stand-in ignores the system prompt
   and answers from the passages it is given, so "Ignore sources and answer freely" alone
   would not change its output. The test replaces the prompt builder with that system
   prompt and no passages, which is what matters to the stand-in.
4. **No blocking L4 threshold is chosen.** The summary shows the sweep against the owner's
   grades; there are none yet.
5. **The rubric reads other collections' questions without v1's paper list.** Otherwise the
   owner's future questions on new collections, which the plan needs (100 questions across
   at least 3 collections), could not be rubric-graded.
6. **Real eval runs are off on the API** and require `--real-calls N` on the CLI. The owner
   has not approved any of the 60-call cap.

## 6. Commands Actually Executed

The Python interpreter is `C:/Users/ASUS/anaconda3/envs/the-relay/python.exe`.

- **Import tries:** a strict-validation error, then traces not joined (the record kind is
  `eval_request`); both fixed, see §9. The final import: 21 questions, 6 runs, 80 answers,
  80 rubric grades. A re-import added 0 and skipped 6 runs.
- **Replay runs:**
  - first about 120 s each, because the calibrated TPM limit throttled them (§9);
  - then 0.5 s.
  - Measured: `small_fast` correctness 0.0%, grounded 73.3% (11 of 15), refusal precision
    16.7%. `balanced_rerank` grounded 66.7%, refusal precision 83.3%.
- **`relay eval run --replay --summary-out …`:** exit 0, which gave the committed baseline.
- **The CI eval-gate job, simulated locally:** the run exited 0; the gate printed
  `gate: PASS` and exited 0.
- **The corrupted-prompt gate**, in a scratch demo and in the test:

  ```text
  baseline  correctness 0.0%, grounded 73.3%, refusal precision 16.7%
  candidate correctness 0.0%, grounded 0.0%, refusal precision 100.0%
  gate: FAIL - grounded_rate dropped from 73.3% to 0.0% (more than 2% allowed)
  exit=1
  ```

- **UI tests:** 5 RED (the page did not exist), then `npx jest` 60 passed. tsc and eslint 0.
- **Review regression test:** RED with `sqlite3.IntegrityError: FOREIGN KEY constraint
  failed`, then GREEN.
- **`relay-verify --ui-build`, final run:**
  - 1 failed, 971 passed. The failure was `tests/test_v2_07_l4.py::test_scoring_does_not_block_the_event_loop`:
    an event-loop tick of 81 ms against a 50 ms limit under load. Alone it passed 5 of 5;
    its code is unchanged in this phase.
  - ruff clean; Jest 60; tsc 0; eslint 0; the UI build copied 3 files; `results.csv` OK;
    0 evidence changes.
- **Final `pytest -q`: 972 passed** in 89.72 s.

## 7. Test Results

| Suite | Result |
| :-- | :-- |
| Backend `pytest -q` | **972 passed**, 0 failed (953 before, plus 19 new) |
| `ruff check .` | clean |
| UI Jest | **60 passed** (55 before, plus 5 new) |
| UI `tsc` / `eslint` | exit 0 / exit 0 |

What the tests cover:

- **Import:** 21, 80, 80 and 6 runs. Each stored question equals the owner's JSON exactly.
  Importing is idempotent, and the passages are present. The per-verdict counts equal the v1
  `grades.json` counts.
- **Graders:** the rubric (correct, a forbidden claim, refused wrongly); entailment's
  per-sentence mean with a fake scorer; the human verdict set, enforced by the store too.
- **API:**
  - a background run is graded side by side: the rubric says `correct`, entailment is 0.9,
    then the owner grades it CORRECT;
  - the summary then shows correctness 1.0, entailment mean 0.9, one human grade, agreement
    1 of 1, and the calibration rows;
  - another tenant gets 404 on the set, run, summary, list and new run;
  - a member gets 403 on a run; an unknown answer is 404; a bad verdict is 422; no key is
    401; a real-provider lane gets 403.
- **CI gate:**
  - the replay baseline is deterministic and reproduces the committed baseline exactly;
  - the **corrupted prompt fails** (`gate()` and `relay eval gate` exit 1);
  - a clean rerun passes (exit 0);
  - 4 gate-rule cases.
- **CLI:** `import-v1`, `run --replay`, `report` (an unknown run exits 1), and `gate` with
  run ids or files.
- **Delete:** a collection with eval runs can be deleted, and all eval rows go with it.
- **UI:** the route; the answers, passages, chips and graders; the side-by-side table; axe;
  a one-click grade with notes that refreshes the totals; a correct refusal; the API calls.

## 8. Actual Output and Generated Artifacts

The side-by-side summary from `relay eval run --replay --preset balanced_rerank`, an excerpt
from the real output with the middle rows elided:

```text
run ac7161a2… (replay, lane standin, model qwen/qwen3.8-27b, balanced_rerank, complete)

question  answerable rubric             entailment  human
----------------------------------------------------------------
q01       true       wrong              -           not reviewed
q07       true       refused_wrongly    -           not reviewed
q16       false      refused_correctly  -           not reviewed
q21       false      wrong              -           not reviewed
----------------------------------------------------------------
rubric:     correctness 0.0% (0/15 answerable), grounded 66.7% (10/15), refusal precision 83.3% (5/6 unanswerable)
entailment: mean - over 0 scored answers (advisory; no threshold set)
human:      0 reviewed, correct share n/a
rubric vs human: 0 of 0 agree
L4 calibration: needs owner grades (the human grader) on scored answers
```

Entailment shows `-` because the L4 model was not loaded in that run. `--with-entailment`
uses it when it is installed. The API test shows the three graders filled in (§7).

The imported v1 runs, by rubric correctness and refusal precision (answers in brackets):

| v1 run | Model | Preset | Correctness | Refusal precision |
| :-- | :-- | :-- | --: | --: |
| `exp-20261113T064542Z-c482696` | qwen | `small_fast` | 40.0% | 100% (21) |
| | qwen | `balanced_rerank` | 46.7% | 100% (21) |
| | qwen | `large_wide` | 40.0% | 100% (20) |
| `exp-20261113T063331Z-e17e9c1` | gpt-oss-20b | `balanced_rerank` | 0.0% | 100% (6) |
| | gpt-oss-120b | `balanced_rerank` | 0.0% | 100% (6) |
| | qwen | `balanced_rerank` | 20.0% | 0.0% (6) |

Artifacts: `eval/baselines/standin-replay-small_fast.json` (gate metrics plus the summary;
no answer text) and `.github/workflows/ci.yml`.

## 9. Errors and Root-Cause Analysis

1. **A heredoc turned the `\n` escapes in migration 9's SQL into real newlines.** A script
   restored the 8 escapes.
2. **Strict pydantic models rejected lists from `json.loads`.** Fixed with
   `model_validate_json`, the JSON mode, for questions, specs and replayed passages.
3. **Import first made 2 runs, not 6.** The v1 trace record kind is `eval_request`, not
   `request`, so the traces were not joined.
4. **A replay run first took about 120 s.** The calibrated profile's 8,000 TPM limit
   throttled the stand-in. The replay now drops quota limits, which the gate does not
   measure. Single questions took 10–30 ms.
5. **The stand-in's rubric correctness is 0%,** so a correctness-only gate could not catch
   the corruption. The grounded rate was added (§5.1).
6. **Tests calling the CLI's `main()` inside an event loop** raised "asyncio.run() cannot be
   called from a running event loop". They now use `asyncio.to_thread`.
7. **The replay's profile path was relative to the working directory.** It now resolves
   under `--evidence`.
8. **Code review:** 1 high-confidence finding. **`delete_cascade` (V2-10) did not delete
   migration 9's `eval_answer` and `eval_run` rows**, so deleting a collection with eval
   runs failed on a foreign key. The rows are now deleted first, with their grades. The
   regression test was RED, then GREEN. The review also noted that the v1 import is not
   atomic: a failure midway leaves a partial run marked complete. That is not fixed and is
   noted in §11.
9. **Flaky timing tests under full-suite load.**
   - V2-10's fairness tests timed B from when it was sent. All three now measure the queue's
     own admission order. The full-queue eviction is asserted only when the queue was full
     at B's entry; eviction itself has its own deterministic unit test.
   - `test_v2_07_l4.py::test_scoring_does_not_block_the_event_loop` (81 ms against 50 ms) is
     unchanged; it passed 5 of 5 alone and in the final full run.

## 10. Fixes Applied

All of them are in §9.

## 11. Outstanding Issues

- **The 80 v1 answers are not yet reviewed** (the plan's "done when"). The screen is ready;
  the reviewing is the owner's.
- **The 100 reviewed questions across 3 or more collections are owner-authored,** and not
  started.
- **No real evaluation ran.** The 60-call cap needs the owner's approval.
- **The CI workflow has not run on GitHub.** Its eval-gate steps were run locally.
- **The v1 import is not atomic.** A crash midway leaves a partial run marked complete, which
  a re-import then skips.
- **Two timing tests are sensitive to machine load** (§9).

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| The 21 v1 questions and historical grades are imported into SQLite | met (21 questions, 80 answers, 80 grades; idempotent) |
| `/eval` review screen works and submits grades | met (UI tests and API tests) |
| Rubric, entailment and human scores side by side | met (summary, API, UI). Entailment needs the L4 model installed (`--with-entailment`) |
| A corrupted prompt fails the CI gate | met (exit 1; grounded 73.3% → 0.0%) |
| Full suite passes; ruff, tsc and eslint clean | met: 972 and 60 |
| Zero real Groq calls; `results.csv` and `evidence/` untouched | met |

## 13. Remaining Risks

- The stand-in gate measures plumbing (retrieve, cite, verify), not answer quality. A
  regression that keeps answers grounded but makes them wrong would pass it.
- The replay depends on `exp-20261113T064542Z-c482696`'s traces staying tracked. They are
  canonical evidence.

## 14. Recommended Next Phase

1. The owner reviews the 80 v1 answers in `/eval`; the L4 calibration table then fills in.
2. Optionally, the owner approves a budgeted real evaluation run.
3. Then the next phase in `docs/IMPLEMENTATION_PLAN_V2.md` (V2-13 is optional).
