# Phase 05 Implementation Report — Pipeline, Evidence Gate, Verifier, Safe Refusal

> **Phase ID:** `p05`
> **Date:** `2026-11-13 06:33 IST`
> **Status:** `Completed`. Verified in Conda env `the-relay` (Python 3.12.16): 338 passed, 0 failed, 0 skipped; ruff clean. **0 Groq calls. No network use.**
> **Owner Approval:** `Owner Approved (2026-11-13)`

---

## 1. Objective

Per `IMPLEMENTATION_PLAN.md` P05, build the request pipeline:
- the single tool `retrieve_passages` behind a validated, timed gateway;
- an evidence gate with at most one paper-scoped re-query;
- a prompt with `[P1]..[Pk]` metadata and a cite-or-refuse rule;
- buffered generation that records `provider_first_token_ms`;
- a verifier implementing checks (a)–(e) of `ARCHITECTURE.md` §7.3;
- on verification failure, a safe refusal with no second LLM call;
- server spans.

Out of scope, and not done: retries, quota or admission (P06), the HTTP service and cache (P08), any real provider call.

## 2. Implementation Summary

The new package is `src/relay/orchestrator/`. One request flows through five steps:

```
1. scope analysis (P04, deterministic)
2. TOOL: retrieve_passages            -> tool failure: status=error / internal_error
3. evidence gate
     EARLY_REFUSAL  -> fixed refusal, no provider call, 0 tokens
     LOOP_EXPAND    -> ONE paper-scoped re-query, then the gate again (never loops twice)
     SUFFICIENT     -> step 4
4. ONE buffered, streamed LLM call    -> provider failure: failed outcome, nothing delivered
5. verifier
     VERIFIED          -> deliver the answer
     REFUSAL_VERIFIED  -> deliver a fixed "not in the papers" message
     BLOCKED           -> deliver the safe refusal (status=ok, citation_verification_failed)
```

**`tools.py`: the gateway for the single tool**
- **Validation:** `RetrievePassagesArgs` is strict Pydantic with unknown fields forbidden. The gateway also rejects a blank query, an unknown retrieval setting, and an unknown or empty paper filter.
- **Execution:** retrieval runs in `asyncio.to_thread` (embedding and reranking are CPU-bound) under `asyncio.timeout`.
- **Recording:** every call, successful or not, yields a `ToolCallRecord` with its arguments, `ok`, elapsed ms, passage count and a sanitised error.
- **Errors** are typed: `ToolInputError`, `ToolTimeoutError`, `ToolExecutionError`.

**`evidence_gate.py`: a deterministic branch and one bounded loop**
- **`EARLY_REFUSAL`** fires only on clearly out-of-corpus signals:
  - nothing retrieved;
  - a question about a non-pinned arXiv version;
  - the scope analyzer's "no cues and low coverage" signal;
  - no strong paper cue and, in a reranked setting, a best cross-encoder score below −5.0.
- **`LOOP_EXPAND`** fires when a paper the question expects has no passage in the first result. Expected papers are the strong cues, plus the weak cues when the question is a comparison. The pipeline re-queries once, scoped to the missing paper, using only the question sentence(s) that name it. Its best new passages are appended: nothing from the first result is dropped, and at most `top_k // 2` are added.
- **Bound:** `loop_count` ensures the gate never asks for a second re-query.

**`prompt.py`: the versioned prompt (`PROMPT_VERSION = "p05-1"`)**
- Each passage is listed as `[Pi] "<title>" (arXiv:<id>v<ver>), pp. x-y, section: <n title>`, followed by its text.
- The rules given to the model:
  - use the passages only;
  - put a tag in every sentence;
  - copy numbers and identifiers exactly, with no arithmetic;
  - any paper, arXiv ID or page named must match a cited header;
  - otherwise reply with exactly `INSUFFICIENT_EVIDENCE`.

**`verifier.py`: checks (a)–(e), sentence by sentence**
- **(a)** No tag at all, or a claim sentence without a tag. A short lead-in ending in ":" with no numbers is exempt, but only when more content follows.
- **(b)** A tag outside `P1..Pk`.
- **(c)** A paper name, arXiv ID/version or page that the cited passages' metadata does not support. A mention that literally appears in the cited passage text is allowed.
- **(d)** A number or technical identifier that is not in the passages *that sentence cites*, where a passage means its text plus its metadata line.
  - Numbers are compared as normalised tokens: `30,000` = `30000`, `12.50` = `12.5`; `51` does not match `512`.
  - Identifiers are `d_model`-style tokens and letter+digit tokens, tolerant of LaTeX or markdown wrappers.
- **(e)** The refusal marker combined with any other content.
- A reply that is exactly the marker is `REFUSAL_VERIFIED`.
- Every ambiguous parse resolves towards blocking.

**`pipeline.py`: orchestration and contracts**
- **Contracts:** `PipelineRequest`, `PipelineConfig`, `PipelineOutcome` and `PipelineTrace` are frozen strict Pydantic models. The outcome enforces the `results.csv` outcome rules itself: `ok` ⇔ delivered; `BLOCKED` ⇔ `citation_verification_failed`; no provider call ⇒ 0 tokens.
- **Generation interface:** generation goes through `AnswerGenerator`. P05 ships `SingleCallGenerator`: one streamed call, buffered, with no retry. It marks the first non-empty content delta on the span tracker; reasoning deltas are excluded.
- **Token accounting:**
  - provider `usage` when present;
  - `estimated` (3.0 characters/token, the owner-approved fallback) when a stream opened without usage;
  - 0 with `none` when the call was rejected before any response.
- **Spans:** the caller owns the `ServerSpanTracker`. The pipeline switches it to `working`.
- **Cancellation:** `CancelledError` is never caught.

**Integration with earlier phases.** The code reuses the following without modifying them:
- P04's `analyze_scope`, `HybridRetriever`, `RetrievedPassage` and `STRONG_CUES`;
- P02's `ChatProvider`, `ChatRequest`, `collect_stream`, `ScriptedProvider` and provider error codes;
- P01's `ServerSpanTracker`, `sanitize_error` and the `Status` / `ErrorCode` / `VerificationVerdict` vocabularies.

## 3. Files Created

- `src/relay/orchestrator/__init__.py`: package marker.
- `src/relay/orchestrator/tools.py`: `ToolGateway`, `RetrievePassagesArgs`, `ToolCallRecord`, typed tool errors.
- `src/relay/orchestrator/evidence_gate.py`: `decide`, `GateDecision`, `expected_papers`, `requery_text`, `merge_expansion`, `expansion_size`.
- `src/relay/orchestrator/prompt.py`: `PROMPT_VERSION`, `REFUSAL_MARKER`, `build_messages`, `build_request`, `passage_metadata`.
- `src/relay/orchestrator/verifier.py`: `verify`, `VerificationResult`, `VerifierFailure`, `CitedPassage`, `split_sentences`.
- `src/relay/orchestrator/pipeline.py`:
  - `Pipeline`;
  - the request, config, outcome and trace models;
  - `AnswerGenerator`, `SingleCallGenerator`, `GenerationResult`;
  - the fixed refusal messages.
- `tests/test_pipeline_and_verifier.py`: 64 tests.
- `reports/phase-05-pipeline-and-verifier.md`: this report.

## 4. Files Modified

- `ENGINEERING_LOG.md`: P05 entry appended.
- **No source file from P01–P04 was changed** (`git diff --stat` is empty). No dependency was added or installed.

## 5. Technical Decisions

All of these need owner review; thresholds are marked [PROPOSED] in code.

1. **Early-refusal rules are deliberately narrow** (ARCH §6: "only clearly out-of-corpus").
   - The rerank threshold of −5.0 is calibrated on the P04B data: off-topic questions scored −8.5 or lower; every answerable or near-topic question scored +0.22 or higher.
   - A strong paper cue always prevents a score-based refusal.
   - I measured a purely lexical alternative (unknown content terms) and rejected it: answerable questions have up to 2 unknown terms and off-topic ones as few as 3, which is too thin a margin.
2. **Settings without a reranker keep the scope analyzer's lexical signal only.** As a result, 4 of the 5 off-topic owner questions reach the LLM in `small_fast` and `large_wide`, while `balanced_rerank` refuses all 5 early. This is the documented known limitation, and it gives Table 3 an honest, measurable difference. Using the cross-encoder as a gate scorer in every setting would blur the "re-rank yes/no" comparison, so it was not done.
3. **Questions about another arXiv version are refused early** (`version_not_in_corpus`). The library is pinned to exact versions, and the scope analyzer already detects the mismatch.
4. **The re-query uses the cue sentence.** Measured on q13: the whole question, scoped to the Attention paper, never returned the gold page 4. The sentence naming the Transformer returned it in the re-query's top-k in 2 of 3 settings. No model rewrites the query.
5. **Merge policy: append, do not replace.** The first implementation replaced the lower half of the first result to keep the prompt size fixed. Measured on q13, that dropped gold pages (2/3 → 0/3 in two settings). The final policy keeps the whole first result and appends at most `top_k // 2` re-query passages. The prompt grows only on looped requests.
6. **A refusal must be exactly the marker.** §7.3(e) says a refusal "also containing factual claims" is rejected. The implementation is stricter: *any* extra content blocks, because deciding which extra sentences count as claims would itself be a heuristic. The user still receives a refusal either way.
7. **Verifier limits are stated, not hidden.** It checks citation integrity and literal grounding, not entailment. Spelled-out numbers and bare acronyms are not checked. Numbers the model derives by arithmetic are blocked, and the prompt forbids arithmetic.
8. **A truncated answer** (`finish_reason="length"`) is flagged in the trace and passes through the same five checks. No sixth check was added beyond §7.3.
9. **`max_completion_tokens` defaults to 512 [PROPOSED].** P02's 256 was accepted by all models but never reached, and reasoning models spend completion tokens on reasoning. The value is a `PipelineConfig` field, so P07 can measure and adjust it.
10. **Failure mapping without the guard (P06):**
    - a tool failure on the first retrieval → `status=error`, `internal_error`;
    - a failed re-query → continue with the first result;
    - a provider error → its own `error_code` and status, no retry, nothing delivered.
11. **Reused definitions:** `CONSERVATIVE_CHARS_PER_TOKEN` and `status_for_error_code` are imported from `relay.providers.probing`, so they aren't duplicated.

## 6. Commands Actually Executed

Git Bash with `PY=/c/Users/ASUS/anaconda3/envs/the-relay/python.exe`. `.env` was not read; `HF_HUB_OFFLINE=1` was set for the local-model runs.

```bash
git status --short; git log --oneline -5              # clean; HEAD c51aaca
# Design measurements on the real indexes (offline):
#   per-question scope cues / coverage / unknown terms / top rerank score
#   q13 paper-scoped re-query: whole question vs cue sentence
$PY -c "import relay.orchestrator.pipeline"            # import ok
$PY -m ruff check src/relay/orchestrator               # 1 E501 -> fixed
$PY -m pytest -q tests/test_pipeline_and_verifier.py   # run 1: 1 failed, 63 passed
$PY -m pytest -q                                       # 338 passed ; ruff: 2 findings -> fixed
$PY <scratch>/p05_smoke.py <scratch>/p05_smoke.json    # ok 63 (21 questions x 3 settings)
# merge policy changed (replace -> append); tests updated
$PY -m pytest -q tests/test_pipeline_and_verifier.py   # 1 failed (test fixture) -> fixed -> 64 passed
$PY <scratch>/p05_smoke.py <scratch>/p05_smoke.json    # ok 63 (final numbers in section 8)
# Final, after the last edit:
$PY -m pytest -q -p no:cacheprovider -rs               # 338 passed in 4.58s
$PY -m ruff check .                                    # All checks passed!
git diff --stat                                        # empty (no earlier-phase file changed)
```

**Verification commands for the owner (PowerShell):**

```powershell
conda activate the-relay
cd C:\Users\ASUS\Desktop\The-Relay
python -m pytest -v tests/test_pipeline_and_verifier.py
python -m pytest -q
python -m ruff check .
git status --short
git diff --stat
```

## 7. Test Results

These are from the final runs, after the last code edit:
- `python -m pytest -q`: **338 passed, 0 failed, 0 skipped** (4.58 s; `filterwarnings=error`). That is 274 earlier tests plus 64 new.
- `tests/test_pipeline_and_verifier.py`: **64 passed**.
- `python -m ruff check .`: **All checks passed!**

What the 64 tests cover:
- **Verifier (23 cases):**
  - a verified answer with ordered citations;
  - 11 blocking cases covering every failure code (a–e, plus empty answers);
  - 4 exact-refusal forms;
  - metadata mentions that match;
  - number and identifier normalisation, including that `51` ≠ `512`;
  - per-sentence grounding against the sentence's own citations;
  - tag syntax variants;
  - a tag after the period not being credited twice;
  - lists, lead-ins and markdown;
  - abbreviation and decimal splitting;
  - an unknown tag next to a valid one.
- **Prompt (1):** tagged passages with metadata, the refusal rule, question placement, and an empty passage list rejected.
- **Gate (10 cases):**
  - sufficient;
  - loop once, then never again;
  - weak cues expected only for comparisons, with the cue-sentence re-query;
  - the re-query fallback;
  - 4 early-refusal reasons;
  - the rerank rule's narrowness (a named paper is never refused on score; the threshold boundary; no scores; the best score wins);
  - the append merge.
- **Tool gateway (10 cases):** a validated, timed, recorded call; 8 invalid-argument cases that never reach the retriever; timeout and execution errors typed and sanitised.
- **Pipeline (20 cases):** see §12 for the acceptance mapping. Beyond the acceptance criteria:
  - an LLM refusal;
  - another arXiv version refused early;
  - a failed re-query keeping the first result;
  - reference intent passed to the tool;
  - a provider rate limit reported with no retry;
  - a stalled stream with estimated tokens and no delivery;
  - missing usage estimated;
  - reasoning-only output not counting as a first token;
  - truncation flagged;
  - client cancellation propagating;
  - invalid requests and wiring failing loudly;
  - **every outcome kind building a valid `ResultRow`**.

## 8. Actual Output and Generated Artifacts

Everything here is offline. The model in the smoke run is a `ScriptedProvider` that always replies with the refusal marker, so **no answer quality is measured**: that is P07.

### 8.1 Smoke run on the real indexes (21 owner questions × 3 settings = 63 pipeline runs)

| Setting | Early refusals (0 tokens) | Sent to the model | Loops | Answerable wrongly refused early | Answerable with ≥ 1 gold page in the prompt | Gold pages in prompt |
| :--- | :--- | -: | :--- | :--- | :-: | :-: |
| `small_fast` | q18 | 20 | q13 | none | 15 / 15 | 20 / 36 |
| `balanced_rerank` | q16, q17, q18, q19, q20 | 16 | q13 | none | 14 / 15 | 19 / 36 |
| `large_wide` | q18 | 20 | q13 | none | 15 / 15 | 22 / 36 |

- **Early refusals.** `balanced_rerank` refuses all five off-topic questions early: four on the rerank score, and q18 on term coverage. The other two settings catch only q18; q16, q17, q19 and q20 go to the model, which must refuse.
- **q21** (near-topic unanswerable) passes the gate in every setting, as intended.
- **q13** (cross-paper) triggers the single re-query in every setting, and the prompt then contains passages from both papers. The gold Attention page 4 is still not in the prompt; the H-Net gold pages are kept (2/3, 2/3, 1/3).
- **Gold-page reachability** equals the P04B post-correction figures, so the gate and loop lost nothing.

### 8.2 Measured pipeline timings and prompt sizes

Single offline run on one machine. Server time excludes the model, since the scripted provider is instant.

| Setting | retrieval_ms median / max | Prompt chars median / max |
| :--- | :-: | :-: |
| `small_fast` | 29 / 40 | 5,790 / 8,293 |
| `balanced_rerank` | 1,121 / 1,502 | 7,086 / 9,165 |
| `large_wide` | 26 / 68 | 13,577 / 20,796 |

- The maxima are the looped q13 request: two tool calls and up to 2 extra passages.
- `balanced_rerank` retrieval time varied between runs on this machine (median 674 ms in the first smoke run, 1,121 ms in the final one). It is dominated by the cross-encoder.

### 8.3 Verifier false-block check on real paper text

For every gold quote contained in a retrieved passage, each sentence of the quote was tagged with that passage and verified: **64 / 64 `VERIFIED`, 0 false blocks.** The first version of this check reported 28 blocks. All were `uncited_sentence`, because the check tagged only the last sentence of multi-sentence quotes. That was a flaw in the check; the verifier correctly required a tag on every sentence.

### 8.4 Design measurements used for the gate (from the P04B question set)

- **Top rerank score:** answerable and near-topic questions +0.22 … +7.82; off-topic questions −8.48 … −10.28.
- **Unknown content terms:** answerable questions 0–2; off-topic questions 3–8.
- **q13 re-query scoped to the Attention paper:** with the whole question, gold page 4 is absent in all three settings. With the cue sentence, it is rank 3 (`small_fast`), absent (`balanced_rerank`) and rank 5 (`large_wide`).

## 9. Errors and Root-Cause Analysis

1. **Merge policy lost evidence (design error, found by the smoke run).** Replacing the lower half of the first result with re-query passages dropped q13's first-result gold pages (2/3 → 0/3 in two settings).
2. **Smoke check mis-tagged multi-sentence quotes.** That produced 28 apparent verifier blocks; the cause was in the check, not in the verifier.
3. **Test fixture errors (2).**
   - One "failed re-query" test used a first result that already contained the expected paper, so no loop was triggered.
   - One loop test listed the same passage twice in a first result.
4. **Draft pipeline code was loosely typed** (keyword dictionaries with `type: ignore`), and an unknown retrieval setting would have surfaced as `internal_error`. Both were found in self-review before the first test run.
5. **Ruff:** one E501 in `pipeline.py`; one import-order and one E501 finding in the test file.

## 10. Fixes Applied

1. The merge now appends at most `top_k // 2` new passages and never drops first-result passages. Re-measured: q13 keeps 2/3, 2/3 and 1/3 gold pages.
2. The check now tags every sentence of a quote: 64/64 verified.
3. Fixtures corrected. The tests now assert the intended behaviour, including the appended `[P5]` passage in the prompt.
4. The pipeline was rewritten with explicit, typed helpers. An unknown setting raises `ContractViolationError`. `GenerationResult` enforces "exactly one of completion / error".
5. Lint fixes.

Re-verification: 338 passed; ruff clean (§7).

## 11. Outstanding Issues

1. **Thresholds and policies need owner approval:**
   - the rerank refusal score of −5.0;
   - the expansion size `top_k // 2`;
   - `max_completion_tokens = 512`;
   - the tool timeout of 10 s;
   - at most 6 answer sentences;
   - the early refusal for other arXiv versions;
   - the exact-marker refusal rule.
2. **Off-topic questions cost tokens in the two non-reranked settings** (4 of 5 owner questions). This is by design and will be visible in Table 3.
3. **Cross-paper evidence is still incomplete for q13.** The loop adds Attention passages, but not the gold page 4.
4. **Prompt size on looped `large_wide` requests** reached 20,796 characters. At the conservative 3.0 characters/token that is about 6,900 tokens before the completion reservation, which is close to the measured 8,000 TPM. P06's reservation or degraded top-k must handle it.
5. **No real model has seen this prompt.** Whether real models follow the tag-per-sentence and exact-marker rules is unmeasured until P07; a model that ignores them will be blocked into safe refusals.
6. **Verifier scope** (see §5 item 7): no entailment check; spelled-out numbers and acronyms are unchecked.
7. **A timed-out tool thread keeps running** to completion in the background. Python threads cannot be cancelled; only the wait is bounded.

## 12. Acceptance-Criteria Status

| Criterion (IMPLEMENTATION_PLAN P05) | Status | Evidence |
| :--- | :--- | :--- |
| Single tool `retrieve_passages` via a validated, timed gateway | `PASS` | `tools.py`; gateway tests (valid call, 8 invalid, timeout, execution error) |
| Evidence gate with max one paper-scoped re-query | `PASS` | `test_requery_loop_runs_at_most_once`, `test_loop_never_repeats_even_if_the_paper_is_still_missing` |
| Prompt with `[P1]..[Pk]` metadata and cite-or-refuse | `PASS` | prompt test |
| Buffered generation recording `provider_first_token_ms` | `PASS` | verified-answer test (first token set, ≤ total); reasoning-only → none |
| Verifier checks (a)–(e) | `PASS` | `test_blocking_checks` (11 cases) and the other verifier tests |
| Verified answer delivered | `PASS` | `test_verified_answer_is_delivered` |
| Out-of-range tag, untagged claim, unsupported number, mixed refusal → safe refusal, `citation_verification_failed`, exactly one provider call | `PASS` | `test_blocked_answers_become_a_safe_refusal_with_one_provider_call` (4 cases; a second scripted reply stays unused) |
| Re-query loop runs at most once | `PASS` | loop tests (2 tool calls, never 3) |
| Clear out-of-corpus → early refusal, 0 tokens, `provider_called=false` | `PASS` | `test_clear_out_of_corpus_question_is_refused_without_the_provider` |
| Tool timeout handled without crash | `PASS` | `test_tool_timeout_is_handled_without_a_crash`, `test_failed_requery_keeps_the_first_result` |
| Server spans recorded; span invariant holds | `PASS` | `assert_spans` in the pipeline tests; `ResultRow` built for every outcome kind |
| Offline, ScriptedProvider, no Groq | `PASS` | no network; conftest guards; 0 Groq calls |

## 13. Remaining Risks

- **Over-blocking by real models.** The verifier is strict by owner decision. If a real model writes uncited connective sentences or explanatory refusals, answers become safe refusals (graded `refused_wrongly` when answerable). P07 will measure this; the prompt can then be adjusted under a new `PROMPT_VERSION`.
- **Gate thresholds calibrated on 21 questions.** The margins are wide (−8.5 vs +0.22 around a −5.0 threshold), but graders will ask other questions.
- **Token budget.** Prompts of 5.8k–13.6k characters (20.8k when looped) plus a 512-token completion reservation sit close to the 8,000 TPM limit for `large_wide`. P06 must enforce it.

## 14. Recommended Next Phase

**P06: ProviderGuard and admission queue.** It plugs into the `AnswerGenerator` interface and adds retries, reservation and settlement, the `retrying` span, and deadlines.

**Owner decisions needed first:** the items in §11.1.

Stopping here; P06 does not start without explicit owner approval.
