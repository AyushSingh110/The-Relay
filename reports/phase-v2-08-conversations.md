# Phase V2-08: Conversations and query rewriting

- **Date:** 2026-11-21 IST
- **Status:** completed, pending owner review
- **Real Groq calls:** 0 (cap 15). Everything ran offline with the scripted provider, the
  rule rewriter or a scripted LLM rewriter, and an in-memory store.
- **Frozen evidence:** `results.csv` sha256 still starts `8dfb10dfaadb`. `git status` shows no
  change under `evidence/` or to `results.csv`.

## 1. Objective

A follow-up question in a conversation must work on POST /v1/ask: "And what is its inference
latency?" after a Moshi question.

1. The follow-up is rewritten into a standalone question before scope analysis, retrieval, the
   exact cache and SingleFlight.
2. First questions cost nothing extra.
3. History is bounded.
4. The user's original question is what gets recorded.

The prompt also listed three L4 prerequisites:

- `huggingface_hub` in the dev dependencies;
- ONNX threads capped at 4;
- 2-sentence sliding windows scored in one batched call, advisory only.

## 2. Implementation Summary

**L4 prerequisites**

- `huggingface_hub>=0.20` is a dev dependency, used by `scripts/download_l4_model.py`.
- `EntailmentScorer` caps threads at 4, and `l4_threads` defaults to 4.
- The scorer splits each cited passage into overlapping 2-sentence windows. It keeps the 2
  windows with the most content-word overlap with the sentence. It scores those 2 pairs in one
  padded ONNX batch and returns the highest entailment probability.
- L4 stays advisory: it adds a confidence value to sentence events and never blocks.

**Rewriter** (`src/relay/orchestrator/rewriter.py`)

- `QueryRewriter` protocol: `resolve(question, history) -> Rewrite`. `Rewrite` carries:
  - the standalone question and the original question;
  - `rewritten`, `method` and `fallback`;
  - the tokens and milliseconds the rewrite cost.
- `LLMQueryRewriter` makes one model call through the service's **guarded** generator, so
  ProviderGuard limits and the real-call budget apply.
  - The prompt is few-shot, temperature 0, at most 64 completion tokens.
  - It holds the last 2 turns only, each message clipped to 600 characters.
  - It has a 1.5 s `asyncio.wait_for` timeout.
  - The output is cleaned: first line only, "Standalone question:" labels and quotes removed.
  - The output is rejected when it is empty, longer than 300 characters, a decline, or a
    question turned into a non-question.
  - Any failure keeps the original question: `timeout`, `provider_error` or `unusable_output`.
- `RuleQueryRewriter` is deterministic, makes no model call, and is used with the stand-in and
  in tests.
  - It resolves `it/they/them`, `its/their` and `this/that/the model|paper|…`.
  - The name it substitutes is the subject named most recently in the last 2 turns: user
    questions first, then answers.
  - Citation tags such as `[P1]` are never taken for a name.
- Both rewriters return the question unchanged, with method `none`, when there is no history.

**Store** (`ConversationRepo`)

- `append_turn(conversation_id, user, assistant)` writes both rows in one transaction.
- `get_recent_turns(conversation_id, limit=2)` returns complete turns, oldest first. It reads
  at most `2·limit+2` rows.

**POST /v1/ask** (`src/relay/api/routes/ask_v1.py`)

- **A conversation with earlier turns** (`_follow_up`) runs in this order:
  1. `accepted` is sent first, with `conversation_id`.
  2. `status {stage: "rewriting"}` is sent at once.
  3. The rewriter runs. If it raises, the original question is kept.
  4. A second `status {stage: "rewriting", rewritten_question, original_question, rewritten,
     method, fallback}` is sent.
  5. The ordinary flow runs on the standalone question. The cache key and the SingleFlight key
     therefore use the rewritten question.
- **A first question**, or a request with no conversation, takes the ordinary path. The
  rewriter is not called.
- `final` now carries `rewritten_question` and `conversation_id`.
- **Recording a turn:** a turn is recorded only when `final.status == "ok"`. It holds the
  user's original question and the text actually delivered: the verified sentences, or the
  refusal message.
  - The turn is written before `final` is sent.
  - A store failure is logged, sanitised, and does not change the answer.
  - Cache hits are recorded the same way.
- **New routes:**
  - `POST /v1/conversations {collection_id}` → 201 `{id, collection_id, created_at}`;
  - `GET /v1/conversations/{id}` → `{id, collection_id, created_at, messages[]}`.
  - Both are tenant-scoped. An unknown or foreign id returns 404; no key returns 401.

**Composition**

- A new `[rewriter]` settings section: `mode = auto | llm | rules`, `model`, `timeout_s`,
  `max_completion_tokens`.
- In `auto` mode, a Groq lane gets the LLM rewriter and a stand-in lane gets the rule
  rewriter, because the stand-in cannot rewrite.
- `Pipeline.generator` is a read-only property, so the rewriter shares the guarded generator.
- The chosen rewriter is printed as a start-up note.

**UI** (`web/`)

- ChatPage creates one conversation per page visit on the first question and passes
  `conversation_id` on every question.
- If creating the conversation fails, the question is asked without context, and the next
  question tries again.
- `conversation_not_found` resets the stored id.
- The banner shows "Reading your question in context…" during the rewrite.
- "Interpreted as: …" appears under a question only when the server changed it.
- The built UI was copied to `src/relay/app/static` by `relay ui build`.

## 3. Files Created

- `src/relay/orchestrator/rewriter.py`
- `tests/test_v2_08_l4_windows.py`: 7 tests
- `tests/test_v2_08_conversations.py`: 4 tests
- `tests/test_v2_08_rewriter.py`: 23 tests
- `tests/test_v2_08_followups.py`: 10 tests
- `web/src/__tests__/conversation.test.tsx`: 6 tests
- `reports/phase-v2-08-conversations.md` (this file)
- Gitignored scratch, not evidence: `scratch/v2_08_l4_latency.{py,json}` and
  `scratch/v2_08_l4_topk.{py,json}`

## 4. Files Modified

- `pyproject.toml`: the `huggingface_hub` dev dependency.
- `relay.example.toml`: `l4_threads = 4` and the `[rewriter]` section.
- `src/relay/app/settings.py`: the `l4_threads` default of 4 and `RewriterSettings`.
- `src/relay/orchestrator/verifier.py`: `passage_windows`, `best_windows`, the windowed and
  batched `EntailmentScorer`, and the 4-thread cap.
- `src/relay/store/repositories.py`: `Turn`, `append_turn` and `get_recent_turns`.
- `src/relay/orchestrator/pipeline.py`: the `generator` property.
- `src/relay/api/app.py`: `RelayService.rewriter`, defaulting to `RuleQueryRewriter()`.
- `src/relay/api/routes/ask_v1.py`: the follow-up flow, turn recording, final fields and the
  conversation routes.
- `src/relay/app/compose.py`: `make_rewriter` and the start-up note.
- Web files:
  - `web/src/api.ts`: the `rewriting` stage, `StatusData`, `AskBody` and
    `createConversation`;
  - `web/src/answer.ts`: `rewrittenQuestion`;
  - `web/src/components/AnswerView.tsx`;
  - `web/src/pages/ChatPage.tsx`;
  - `web/src/styles.css`;
  - `web/src/test/fakeApi.ts`.
- `web/src/__tests__/pages.test.tsx`: one V2-06 expectation now includes `conversation_id`.
  The contract changed by design.
- `src/relay/app/static/*`: the rebuilt UI bundle. The hashed asset names changed.
- `reports/v2-07-l4-calibration.md`: regenerated with the windowed scorer (§8). **This
  overwrote the exploratory windows section that had been appended by hand in V2-07.** That
  section's numbers are still in the V2-07 log entry.

## 5. Technical Decisions

These are my rulings where the prompt left room. Each says what it costs if wrong.

1. **The rewrite runs in the `/v1/ask` route, before the cache and SingleFlight, not inside
   `pipeline.run`.**
   - The prompt asks for the rewritten question to key the cache and SingleFlight, and both
     sit in the route, in front of the pipeline.
   - The pipeline receives the standalone question, so scope analysis, retrieval, the evidence
     gate and the prompt all use it.
   - v1 `/ask` is unchanged.
   - Cost if wrong: moving the call later is local to `_follow_up`.
2. **The rewrite is reported in `status: rewriting` events, not inside `accepted` or
   `status: retrieving`.**
   - `accepted` must stay first and immediate (ARCHITECTURE_V2 §6), and the rewrite only ends
     after it.
   - The result also appears in `final.rewritten_question`.
   - Cost if wrong: a client that expected the field on `retrieving` must read the
     `rewriting` status instead.
3. **The 1.5 s timeout includes any wait at ProviderGuard,** because the rewrite shares the
   guarded generator. When the lane is saturated, the follow-up falls back to the original
   question instead of waiting.
4. **The recorded answer text** is the delivered verified sentences joined with spaces, or the
   refusal message. A non-ok final (provider failure, shed, deadline) records nothing, so a
   failed attempt never pollutes the context.
5. **In `auto` mode the stand-in lane uses rules.** The stand-in returns scripted text, so an
   LLM rewrite against it would be meaningless.
6. **L4 scores the top 2 windows by word overlap, not all windows.**
   - Scoring every window was too slow: p50 685 ms, p95 1,622 ms.
   - Top 2 gives the same AUROC: 0.771 vs 0.768 for all windows.
   - Top 2 costs p50 178 ms and p95 298 ms at 4 threads (§8).
7. **No model name is suggested** in `relay.example.toml` for `[rewriter].model`. Under
   CLAUDE.md §5.16, a model choice is not treated as verified without real evidence.
8. **Plan task 4 was not done:** the follow-up evaluation set must be written by the owner
   (CLAUDE.md §5.15). None was authored.

## 6. Commands Actually Executed

The Python interpreter is `C:/Users/ASUS/anaconda3/envs/the-relay/python.exe`.

- `python -m pytest tests/test_v2_08_followups.py -q` before the implementation: 9 failed,
  1 passed (RED). After it: 10 passed.
- `python -m pytest tests/test_v2_08_rewriter.py tests/test_app_settings.py
  tests/test_v2_01_review_fixes.py -q`: 47 passed.
- `npx jest src/__tests__/conversation.test.tsx` before the UI change: 4 failed, 2 passed
  (RED). The 2 that passed assert that something is absent.
- `npx jest`: 47 passed.
- `npx tsc -p tsconfig.json --noEmit`: exit 0.
- `npx eslint .`: exit 0.
- `relay ui build`: Vite built, and 3 files were copied to `src/relay/app/static`.
- `python -m pytest -q`: **866 passed** in 107.64 s.
- `python -m ruff check .`: All checks passed.
- `sha256sum results.csv`: `8dfb10dfaadb…`.
- `git status --short results.csv evidence/`: empty.
- Earlier in the phase:
  - the L4 latency and top-k scratch scripts;
  - the calibration regeneration;
  - the window, conversation and rewriter unit tests, each RED, then GREEN.

## 7. Test Results

| Suite | Result |
| :-- | :-- |
| Backend `pytest -q` | **866 passed**, 0 failed, 0 skipped (822 in V2-07 + 44 new) |
| `ruff check .` | clean |
| UI Jest | **47 passed** (41 + 6 new) |
| UI `tsc` / `eslint` | exit 0 / exit 0 |

The tests the owner asked for:

| Owner's test | Test |
| :-- | :-- |
| First questions never call the rewriter | `test_first_questions_never_call_the_rewriter`: no conversation, and the first turn of a conversation |
| "And what is its inference latency?" includes Moshi | `test_a_follow_up_is_rewritten_before_retrieval_and_generation`: rewritten to "What is Moshi's inference latency?"; the provider prompt contains it; `final.rewritten_question` is set |
| A self-contained follow-up is unchanged | `test_a_self_contained_follow_up_is_unchanged`: the BERT question, `rewritten` false |
| Failure and timeout fall back | `test_a_failing_rewrite_falls_back_to_the_original_question` (raises, 0.05 s timeout, provider error), plus unit tests for unusable output |
| Cache sharing | `test_a_rewritten_follow_up_shares_the_cache_entry_of_the_same_question`: "What is its latency?" gives `exact_hit` with 1 provider request in total |
| History persisted and bounded | `test_the_conversation_is_recorded_and_the_rewrite_sees_at_most_two_turns`: 10 messages; the user originals are stored; the rewriter saw history lengths [1, 2, 2, 2]. `test_a_failed_answer_is_not_recorded`. `test_the_prompt_is_bounded_whatever_the_history`: 30 turns of 5,000 characters, and only the last 2, clipped, reach the prompt |

## 8. Actual Output and Generated Artifacts

**L4 windowed scorer latency** (scratch, 200 real pairs, int8 AVX2, 4 threads)

- p50 180.3 ms, p95 309.4 ms.
- Windows per passage: median 8, maximum 23.

**Top-k comparison** (`scratch/v2_08_l4_topk.json`)

| Windows scored | AUROC (not fully supported) | p50 ms | p95 ms |
| :-- | --: | --: | --: |
| top 2 (adopted) | 0.771 | 178.2 | 298.1 |
| top 3 | 0.774 | 290.2 | 471.8 |
| top 4 | 0.770 | 349.5 | 562.6 |
| all | 0.768 | 684.7 | 1,622.2 |

**Regenerated calibration** (`reports/v2-07-l4-calibration.md`, the same proposed labels)

- Threshold 0.3 blocks 42 of 224 sentences:
  - TPR 61.5 % (16 of 26), FPR 13.1 % (26 of 198).
  - With the whole-passage scorer in V2-07, the FPR was 82.8 %.
- Medians by label:
  - supported 0.887;
  - partially supported 0.176;
  - not supported 0.004.
- No threshold is set. L4 stays advisory.

## 9. Errors and Root-Cause Analysis

1. **Scoring all windows was too slow** (p50 760 ms in the first measurement). One ONNX pair
   per window over long passages caused it. Fixed by the top-2 window selection (decision 6).
2. **Ruff's auto-fix for SIM905 produced over-long lines** in the stopword and `_NOT_NAMES`
   lists. The lists were wrapped by hand.
3. **The test helper built a `CompletionResult` by hand** and missed required fields. It was
   replaced by the real `SingleCallGenerator` over `ScriptedProvider`.
4. **The rule rewriter took "P1" as a subject** from a stored answer's citation tags ("What is
   P1's latency?"). Fixed by stripping `TAG_PATTERN` before finding names. A regression test
   was added.
5. **The route patch's first run failed its own assertion,** before writing anything: the
   app.py import anchor was `CitedPassage, EntailmentScore`. The anchor was corrected and the
   patch re-applied.
6. **The fresh-context code-review agent stopped on an API rate limit** (HTTP 429) before
   reporting. I reviewed the code myself instead (§11).

## 10. Fixes Applied

All of them are in §9. Each code fix was verified by a test run: items 1 and 4 by a test that
failed first.

## 11. Outstanding Issues

- **No independent review.** The review agent hit an API rate limit. My own review checked:
  - CancelledError propagation: the `except Exception` blocks do not catch it;
  - tenant scoping on the new routes;
  - `get_recent_turns` ordering: the rowid breaks the tie on equal `created_at`;
  - the cache key: SingleFlight keys on `normalise_question(ask.question)`, the rewritten
    question.

  It found nothing blocking. An author's review is weaker than a fresh one; the owner may want
  one before commit.
- **The LLM rewriter has never run against Groq** (0 real calls). Untested:
  - the 64-token cap with reasoning models: reasoning tokens could use up the budget, which
    gives `unusable_output` and the original question;
  - real latency against the 1.5 s timeout;
  - real output quality.

  A live check would cost about 5–10 calls of the 15-call cap, with owner confirmation.
- **Rewrite cost is not in the request log.**
  - The `rewrite` trace record carries method, tokens, ms and fallback, but `RequestLogSink`
    keeps only `request` records.
  - `final.tokens_in` and `final.tokens_out` exclude the rewrite.
  - The guard still counts the rewrite against the lane's limits.
- **The rule rewriter is heuristic.**
  - It cannot resolve "the second paper", "the former", or a plural antecedent.
  - It takes the first capitalised non-stopword as the subject, which can be wrong for
    sentence-initial words outside its list.
- **Concurrent follow-ups on the same conversation** (only possible outside the UI, which
  blocks while answering) both see the same history. Both turns are recorded, in completion
  order.
- **A disconnect during the rewrite** cancels the call through `wait_for`. No test covers this
  path through the SSE generator.
- **The L4 latency target is missed:** p50 180 ms against the 150 ms target, on this laptop at
  4 threads.
- **The compose selection test was written after `make_rewriter`,** not first.
- **Plan task 4** (the follow-up evaluation set) waits for owner-written questions.

## 12. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| `huggingface_hub` dev dependency; ONNX threads ≤ 4; 2-sentence windows in one batch, advisory | met |
| QueryRewriter: few-shot, low temperature, 64 tokens, fallback on a 1.5 s timeout or failure, rule rewriter for the stand-in and tests | met (the LLM path is verified offline only) |
| `get_recent_turns(limit=2)` and atomic append | met |
| Rewrite only with prior turns; first questions have 0 overhead | met (tested) |
| Cache and SingleFlight keyed on (collection, version, rewritten question) | met (cache-hit test; the SingleFlight key reads the same field) |
| Original question kept in history; the turn recorded after a verified delivery | met |
| `rewritten_question` in the SSE metadata | met, on `status: rewriting` and `final` (decision 2) |
| UI keeps `conversation_id` across turns | met |
| 822+ backend and 41+ UI tests pass; ruff clean | met: 866 and 47 |

## 13. Remaining Risks

- An LLM rewrite that changes meaning, for example by dropping a constraint, would retrieve
  for the wrong question. The UI's "Interpreted as" line is the user's only signal.
- A rule-rewriter false positive can substitute the wrong subject. The user sees it under the
  question.
- Every follow-up on a Groq lane spends one more provider call, plus RPM and TPM, through the
  same guard as answers. Under load this could raise the shed rate.

## 14. Recommended Next Phase

1. Owner review of this report.
2. Optionally, a capped live check of the LLM rewriter (about 5–10 Groq calls, with owner
   confirmation).
3. The owner writes the follow-up evaluation questions (plan task 4).
4. Then the next V2 phase in `docs/IMPLEMENTATION_PLAN_V2.md`.
