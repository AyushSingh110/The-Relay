# Phase V2-05 Implementation Report — Verified Streaming

> **Phase ID:** `v2-05`
> **Date:** `2026-11-19 20:12 IST`
> **Status:** `Completed`.
> - Verified in Conda env `the-relay` (Python 3.12.16): **718 passed**, 0 failed, 0 skipped (656 before the phase, 62 new); `ruff check .` clean.
> - **One done-criterion is not met:** the median first-sentence time is 649 ms against a 350 ms target. The stand-in's own first token is already 436 ms (see §8).
> - **Real Groq calls: 0** (none requested; the 15-call cap was not used). `evidence/` and `results.csv` are unchanged.
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

From the owner's prompt (plan V2-05; `docs/ARCHITECTURE_V2.md` §6):

**Streaming**
- Generate a token stream, split it into sentences as tokens arrive, verify each sentence, and send it.
- When a sentence fails: cancel generation, never send the failed sentence, and mark the answer partial.
- `POST /v1/ask` with the event protocol `accepted`, `status`, `sources`, `sentence`, `stopped`, `final`.

**Service behaviour**
- Queue keepalive: never silent for 5 s.
- Coalesced followers get a replay of the events so far.
- A per-collection buffered fallback.
- Only fully verified answers are cached.
- The old `POST /ask` is unchanged.

**Measurement**
- Harness: first-status and first-sentence times; a `steady_streaming` scenario.
- The partial-answer rate on the v1 questions.

**Done when:**
- no event carries a failed sentence (property test);
- `accepted` comes before any work;
- the keepalive works;
- median first sentence < ½ the v1 median total (701.1 ms → 350.6 ms);
- the partial rate is measured;
- all tests pass; ruff clean; `evidence/` and `results.csv` unchanged.

## 2. Implementation Summary

**`orchestrator/streaming.py`**
- **`IncrementalSentenceSplitter`** re-runs v1's `split_sentences` on the text so far. It releases a sentence only when nothing still to come could change it:
  - never the last unit;
  - held while the next unit could still start with a citation tag, an unfinished bracket, or a list number.

  A guard raises if a released sentence would ever change. A property test, over 40 random chunkings each of tricky texts and all 63 real Table 3 answers, shows it always equals the whole-answer split. That covers tags inside sentences, decimals, ellipses, abbreviations, list items and tags written after the full stop.
- **`StreamingVerifier`** runs the v1 per-sentence checks (L1 structure, L2 literal grounding). They run through `SentenceChecker`, the same code the whole-answer verifier now uses for each sentence, extracted from `verifier.py` without changing behaviour (the 84-row Table 3 replay is unchanged). `confidence` is `None` until L4 exists.
- **`SentenceStream`** yields `VerifiedSentence` / `StoppedEvent` / `StreamRefusal`.
  - On the first failure it closes the token stream (cancelling generation) before yielding the stop.
  - A lead-in line ("… these parts:") is held until the item after it verifies.
  - The refusal marker is checked on the whole text, as v1 does.
  - After a provider failure the unfinished tail is never released.

**Generation and pipeline**
- **Generators** (`pipeline.SingleCallGenerator`, `provider_guard.GuardedGenerator`): `on_content` forwards each content piece. An attempt that has forwarded content is never retried, because a retry would produce a different answer.
- **Pipeline:**
  - `run()` is unchanged in behaviour and now shares `_prepare` (scope, retrieval, gate, prompt) with streaming.
  - New: `prepare_streaming`, `run_streaming` (a `TokenStream`; `aclose` cancels generation and never swallows the caller's own cancellation), `complete_streaming` (spend, and the v1 whole-answer pass as the final check) and `complete_buffered`.
  - New outcome kind `partial`. The trace gains `stream_stop` (reason, sentence index, failure codes).

**`POST /v1/ask`** (`api/routes/ask_v1.py`)
- **Request and keepalive:**
  - Bearer stub (dev tenant); `collection_id` defaults to `v1-papers`; `conversation_id` is validated.
  - `accepted` is the first event, and the work starts only after it is sent.
  - The current stage (with the live queue position) repeats every 4 s of silence.
- **`ReplayChannel` and coalescing:**
  - Each flight's events are recorded in a `ReplayChannel`. Followers get the replay plus live events, and each request gets its own `accepted` and `final`.
  - When the last listener leaves, the flight is closed at once and cancelled, which frees its slot and stops generation.
- **Answers and cache:**
  - Only verified answers are cached; verified refusals are cached as v1 does and replayed as `final.message`, never as `sentence`.
  - Buffered collections run the buffered path and then emit every sentence after `verifying`.
- **Shutdown:** `AskV1Context.aclose` cancels live flights.

**Supporting changes**
- **Admission:** `admit(on_queued=…)` gives a waiter its live position; a failing callback leaves no ghost waiter.
- **Store:** migration 004 adds `collection.buffered` (default 0), settable on `POST /v1/collections`.
- **Compose:** mounts `/v1/ask` in sqlite mode; shutdown cancels its flights before the provider and store close.
- **Harness:**
  - `experiments/streaming_load.py`: per request, time to `accepted`, time to first `sentence`, total, provider first token and the longest silence; counts of missing finals and of protocol and transport errors.
  - `steady_streaming`: 1 warm-up round, then 3 rounds of 5 concurrent streams, 60 s apart.
  - `scripts/run_streaming_load.py` writes `runs/<id>/`.
- **Replay:** `table3_replay.replay_streaming` and `scripts/replay_table3.py --streaming` stream the recorded Table 3 answers through the new path.

## 3. Files Created

**Source**
- `src/relay/orchestrator/streaming.py`
- `src/relay/api/routes/__init__.py`, `src/relay/api/routes/ask_v1.py`
- `src/relay/experiments/streaming_load.py`
- `scripts/run_streaming_load.py`

**Tests (62 new; with prompt test numbers):**

| File | Tests | Covers |
| :-- | --: | :-- |
| `tests/test_streaming.py` | 9 | 1, 2, 3, 4, 5, 11 |
| `tests/test_guard_streaming.py` | 3 | |
| `tests/test_pipeline_streaming.py` | 6 | |
| `tests/test_store_migration_004.py` | 2 | |
| `tests/test_admission_position.py` | 2 | |
| `tests/test_ask_v1.py` | 11 | 6, 7, 8, 9, 10, 11, 14, disconnect |
| `tests/test_ask_v1_review_fixes.py` | 7 | |
| `tests/test_compose_streaming.py` | 2 | |
| `tests/test_streaming_load.py` | 4 | 13 |
| `tests/test_v2_05_review_fixes.py` | 8 | |
| `tests/test_v2_05_silent_failure_fixes.py` | 7 | |
| `tests/test_v1_regression.py` | +1 | Streaming replay |

**Runs (new, untracked, for you to commit or not), under `runs/`:**

| Run | What it is |
| :-- | :-- |
| `stream-20261119T143748Z` | **Canonical** |
| `stream-20261119T142820Z` | Superseded: before the silent-failure fixes |
| `stream-20261119T140452Z` | Superseded: before the whole-phase fixes |
| `stream-20261119T140921Z` | Concurrency-1 diagnostic |

**Report:** this report.

## 4. Files Modified

- `src/relay/orchestrator/verifier.py`: `SentenceChecker` extracted; public `LEADING_TAGS`, `WORD_CHAR`, `REFUSAL_MARKER_PATTERN`, `is_refusal_marker`.
- `src/relay/orchestrator/pipeline.py`, `src/relay/control/provider_guard.py`, `src/relay/control/admission.py`.
- `src/relay/api/app.py`: `RelayService.record_trace`.
- `src/relay/app/compose.py`.
- `src/relay/app/routes/ingestion.py`: `buffered`.
- `src/relay/store/migrations.py`, `src/relay/store/repositories.py`.
- `src/relay/experiments/table3_replay.py`, `scripts/replay_table3.py`.
- `tests/test_v1_regression.py` (+1).
- `docs/DECISIONS.md`: three owner decisions.
- **Old tests changed on purpose by fixes, each recorded in §10:**
  - `test_streaming.py`: the mixed-marker case now releases nothing.
  - `test_ask_v1_review_fixes.py`: a cached refusal is served as a message.

## 5. Technical Decisions

**Owner decisions (2026-11-19, in `docs/DECISIONS.md`):**
1. The partial-answer rate is measured by replaying the 63 recorded Table 3 answers through the streaming path.
2. Property tests use a seeded random generator; Hypothesis is not added.
3. `/v1/ask` without a `collection_id` uses the caller's `v1-papers` collection.

**My rulings ([PROPOSED]; each says what it costs if wrong):**
1. **`stopped` is followed by `final`.** The prompt says both "instead of final" and "then final"; `final` is always last.
2. **Keepalive every 4 s** (configurable), to stay safely under the 5 s silence limit.
3. **The splitter releases only sentences that can no longer change.**
   - Cost: a sentence goes out one token or so after its full stop, or after the next list number.
4. **No retry once content has been forwarded.**
   - Cost: a transient failure after the first token, but before any sentence went out, is not retried.
5. **Partial outcomes:**
   - stopped by verification: status `ok` with the released sentences;
   - stopped by a failure (provider error, client gone): the failure status with no delivered text (v1's rule: status `ok` exactly when text is delivered).
6. **A lead-in is released only together with the verified item after it.**
7. **Buffered collections** send `status: verifying`, then every `sentence` at once, then `final`; the event sequence is the same as streaming.
8. **The cache** stores verified answers and verified refusals (as v1 does). A refusal replays as `final.message`, never as `sentence`.
9. **Coalescing for `/v1/ask` is separate from `/ask`'s SingleFlight**, because the response shapes differ.
   - Cost: the same question arriving on both endpoints at the same moment makes two calls.
10. **The `steady_streaming` setup:**
    - 1 warm-up round, reported apart: it carries the lazy embedding-model load and the guard's first, conservative token estimates (one request in it waited 61 s for quota);
    - `max_completion_tokens = 256`, so a round of 5 fits the stand-in's 8,000 TPM;
    - guard and queue sized for 5 concurrent requests;
    - warm-up questions are the same text plus one `?` (a different cache key, the same words).
11. **`conversation_id` is validated** (it must exist in the collection) but not yet used: follow-up rewriting is a later phase.

## 6. Commands Actually Executed

```text
python -m ruff check .                         # after every file; last: All checks passed!
python -m pytest -q -p no:cacheprovider        # after every file (counts in §7)
python -m pytest tests/<new file> -q           # each new test file RED first
code-review skill: low per file x 9, medium (route), high (whole phase)
pr-review-toolkit:silent-failure-hunter agent (read-only)
python scripts/run_streaming_load.py --runs-dir runs                    # x3 (last canonical)
python scripts/run_streaming_load.py --runs-dir runs --concurrency 1 --rounds 10 --gap-s 7
python scripts/replay_table3.py --out <scratch>/…buffered.md            # 84/84
python scripts/replay_table3.py --streaming --out <scratch>/…stream.md  # 63/63 + 21/21
```

## 7. Test Results

**Full suite after each file:**

| After | Passed |
| :-- | --: |
| `streaming.py` | 665 |
| guard | 668 |
| pipeline | 672, 673, 674 (two more tests while fixing review findings) |
| migration 004 | 676 |
| admission position | 678 |
| `/v1/ask` | 689 |
| route review fixes, compose | 698 |
| harness | 700, 702 |
| streaming replay | 703 |
| whole-phase review fixes | 711 |
| silent-failure fixes | **718** |

**Final:** `718 passed`; `ruff check .` → `All checks passed!`

**Prompt tests 1–14:**

| # | Test | Result |
| :-- | :-- | :-- |
| 1 | Splitter: simple sentence, `[1]` mid-sentence, 3.14, ellipsis, e.g. | pass |
| 2 | `flush()` returns a fragment without final punctuation | pass |
| 3 | `StreamingVerifier` passes a cited sentence and fails an uncited one | pass |
| 4 | `SentenceStream` cancels the token generator on a failure | pass |
| 5 | `SentenceStream` yields every sentence when all pass | pass |
| 6 | `accepted` before any pipeline work (before sources and before `prepare_streaming` is called; under 200 ms) | pass |
| 7 | Keepalive while queued (0.2 s keepalive; largest gap < 0.35 s; default 4 s < 5 s) | pass |
| 8 | A failed sentence: `stopped`, no further `sentence`, then `final`; generation cancelled | pass |
| 9 | A full answer is cached; a partial one is not | pass |
| 10 | A coalesced follower gets the replay plus live sentences; one provider call | pass |
| 11 | Property: a failed sentence never appears in any event (500 stream cases and 25 HTTP cases) | pass |
| 12 | Old `/ask` unchanged; all 656 earlier tests pass | pass (718 in total) |
| 13 | `steady_streaming` completes and records the median first sentence | pass |
| 14 | A `buffered=true` collection uses the buffered path over SSE | pass |

## 8. Actual Output and Generated Artifacts

### Streaming latency (calibrated stand-in, real loopback HTTP, `v1-papers`, 0 real calls)

**Canonical run `runs/stream-20261119T143748Z`, 15 measured requests (3 rounds of 5 concurrent):**

| Metric | p50 | p95 | max |
| :-- | --: | --: | --: |
| First status (`accepted`) | **94.0 ms** | 120.3 ms | 120.3 ms |
| Provider first token (server side) | 435.8 ms | 565.2 ms | 565.2 ms |
| **First verified sentence** (n=14) | **649.0 ms** | 890.8 ms | 890.8 ms |
| Total | 832.9 ms | 1,130.6 ms | 1,130.6 ms |

- 15/15 HTTP 200; every stream ended with `final`; 0 protocol errors and 0 transport errors.
- Longest silence on any stream: 628 ms.
- Verdicts: 13 VERIFIED, 2 BLOCKED (both stopped `uncited`); 1 partial (1/15 = 6.7 % on stand-in answers).
- Warm-up round (reported apart): first sentence p50 1,664 ms, with one request at 61.5 s (a quota wait at cold start).
- The two earlier 5-concurrent runs gave 628.9 and 604.0 ms for the same metric.

**Target: under 350.6 ms (½ of v1's 701.1 ms). Not met.**
- The stand-in's own first token, 435.8 ms at the median at this concurrency, is already above the target.
- A sentence can only be released once it is complete. The stand-in's first sentence is a copied passage sentence of about 50 tokens at 2.6 ms each, which accounts for most of the remaining ~210 ms.
- **Concurrency-1 diagnostic** (`runs/stream-20261119T140921Z`, 10 requests):
  - provider first token p50 327.2 ms;
  - first sentence p50 492.4 ms;
  - total p50 633.1 ms.

  So even without contention, the stand-in's floor is above 350 ms.
- **Against v1:** buffered v1 delivered everything at 701 ms (p50). Streaming delivers the first verified sentence at 649 ms with 5 requests at once, and at 492 ms alone. ARCH_V2 §6's own goal (first verified sentence under 2.5 s at the median) is met.

### Partial-answer rate on the v1 questions (owner decision: replay Table 3)

The 63 recorded real answers (21 questions × 3 settings) were streamed in 4-character pieces through the splitter, the per-sentence verifier and the final pass:

- **63/63 rows reach v1's verdict** (and 21/21 through `v1-papers`).
- **Partial answers: 1 of 55 answered rows (1.8 %).** The other 8 rows were 7 early refusals and 1 v1 provider failure.
- **The one partial is q02 / small_fast.** v1 BLOCKED it as a whole and delivered a safe refusal. Streaming delivers 3 verified sentences, then `stopped` (`ungrounded`) on the fourth; that sentence is never sent.
- No row stopped before its first sentence. Every VERIFIED row streamed all its sentences (2–6) and verified.

**Buffered regression (V2-04 replay) on the final code: 84/84 still match.**

## 9. Errors and Root-Cause Analysis

| Problem | Cause | Fix |
| :-- | :-- | :-- |
| The splitter guard fired in the property test | A sentence that had just gained a tag ("Claim. [P1]") could still gain another ("[P2]") | Settledness now depends only on what follows |
| `on_queued` test hung (RED as a hang) | The test had no time bound | `asyncio.timeout` in the test |
| Outcome validation error on partial failures | v1's invariant (status `ok` exactly when text is delivered) | Ruling 5 |
| `/v1/ask` property test failure | Test bug: "Moshi" is not a name in a geology collection, so it is correctly not blocked | Replaced with an ungrounded identifier |
| Harness `IndexError` | v1's `pct` takes a fraction (0.5), not 50 | Fixed the call |
| Warm-up refused early | " (warm-up)" lowered vocabulary coverage | Warm-up adds one `?` |
| Shell heredocs mangled `\n` twice | Quoting | Edited with the Edit tool |

The known v1 timing flake (`test_stragglers_are_much_slower`) did not occur in this phase.

## 10. Fixes Applied

Every fix below got a test that failed first, except where noted.

**Per-file code reviews (9):**
- the splitter instability case;
- a disconnect recorded as a VERIFIED answer;
- a missing `/v1/ask` mount;
- the page-range duplication;
- the harness aborting on one failure;
- the loopback socket leak.

**Route review (medium), 10 findings, 7 fixed:**
1. the leader-start gate;
2. a cancelled flight that could still be joined;
3. a provider-failure tail being streamed;
4. a v1 cached refusal replayed as a sentence;
5. the `on_queued` ghost waiter;
6. an internal error after sentences reported as `partial: false`;
7. a follower's queue position reported as 0.

Shutdown cancellation and the compose mount were done in the compose step; finding 10 is ruling 9. Finding 1 could not be reproduced as RED through the test driver; its test stays as a guard.

**Whole-phase review (high), 10 findings, 8 fixed + 1 cheap minor:**
1. a hung follower when the leader leaves during `accepted`;
2. a list number then a leading tag losing an answer;
3. lead-ins released before their item;
4. `TokenStream.aclose` swallowing the caller's cancellation;
5. no spend on the flight record;
6. first-sentence time recorded as `provider_first_token_ms`;
7. refusals never cached (a repeat call each time);
8. a marker split across lines;
10. duplicated regexes (now imported from the verifier).

The quadratic re-split (9) is deferred.

**Silent-failure agent, 6 fixed:**
1. a failing cache write turning a VERIFIED answer into an error;
2. the stop reason missing from the trace;
3. a shutdown after sentences reported without `partial` or `stopped`;
4. the harness unable to see missing finals or protocol errors;
5. cancelled flights with no spend;
8. a flight cancelled before it started leaking.

**Old tests changed by these fixes (intended behaviour changes):**
- `test_streaming.py`: an answer mixing a verified sentence and the marker now releases nothing; v1 BLOCKS it.
- `test_ask_v1_review_fixes.py`: a cached verified refusal is served as a message.

## 11. Outstanding Issues

**Deferred minors and rulings, from the reviews:**
- **Splitter cost:** it re-splits the whole text per token (fine for answers of at most 6 sentences).
- **Retry rule:** "no retry after forwarding" covers content still held by the splitter (ruling 4).
- **Stop/finish race:** rare-timing.
- **Lead-in edge case:** a lead-in held at the very end stays in `delivered_text` (uncertain).
- **Stage labels:** `verifying` after a provider failure.
- **Channel:** `publish` after `close` is not enforced.
- **Admission catch:** covers the whole body.
- **No `/v1/ask` deadline:** transport timeouts only.
- **Reasoning tokens:** reasoning characters are not in `received_chars`.

**Not built:**
- conversation-based follow-up rewriting;
- the L4 entailment score (`confidence` is `None`);
- `/v1/ask` per-tenant quotas (V2-07).

**Latency target:** not met on the stand-in (§8).

## 12. Acceptance-Criteria Status

| Criterion | Status | Evidence |
| :-- | :-- | :-- |
| No SSE event carries a sentence that fails the verifier | Met | Test 11 (property, two levels); review fixes 3, 7 and 8 |
| `accepted` before any pipeline work | Met | Test 6 (two tests) |
| Never silent for more than 5 s while queued | Met | Test 7; canonical run longest silence 628 ms |
| Median first sentence < half the v1 median total (350.6 ms) | **Not met** | 649.0 ms; the stand-in's first token alone is 435.8 ms (§8) |
| Partial-answer rate measured on the v1 questions | Met | 1/55 (1.8 %), Table 3 replay; stand-in 1/15 |
| All 656 existing tests pass | Met | 718 passed |
| All 14 new tests pass | Met | §7 |
| ruff clean | Met | All checks passed |
| `results.csv` and `evidence/` unchanged | Met | git status empty; sha256 unchanged (§14 commands) |

## 13. Remaining Risks

- **First-sentence latency depends on the model's first token and its first sentence length.** It cannot fall below the provider's own first token plus one sentence.
- **Real-model behaviour is unmeasured.** The real Groq first token was 814 ms p50 (v1 real steady), so a live first sentence would land around 1 s. That needs a live run (≤15 calls) if you want it measured.
- **Streaming exposes partial answers:** about 2 % of v1 answers would show some verified sentences, then stop, where v1 showed a refusal.
- **The `/v1/ask` coalescing layer is new code.** It is tested (replay, disconnects, shutdown, races), but not yet load-tested beyond 5 concurrent streams.

## 14. Recommended Next Phase

**V2-06 (Web UI).** It will consume the `/v1/ask` events.

Optionally, before that:
- a ≤15-call live spot check of first-sentence latency on real Groq (needs your confirmation);
- your decision on rulings 4, 5 and 9.

### Plugins used, and whether they helped

| Plugin | Used for | Helped? |
| :-- | :-- | :-- |
| `superpowers:test-driven-development` | Every new module and every fix ran RED first (exceptions noted in §10). | **Yes.** RED runs exposed a hanging test and several wrong test assumptions. The splitter property test found a real instability on its first run. |
| `code-review` | After every file (low), the route (medium), and the whole phase (high). | **Yes, substantially.** It found the stuck-flight gate, the list-number splitter case, lead-ins sent unchecked, swallowed cancellation, provider-error tails and the cached-refusal replay. None was covered by my tests. |
| `pr-review-toolkit:silent-failure-hunter` | Failure paths of the streaming code (read-only). | **Yes.** Six real problems, mostly misleading final events and lost telemetry: the cache write turning VERIFIED into an error, the shutdown fallback, cancelled spend, and the harness blind to missing finals. |
| `superpowers:verification-before-completion` | Fresh suite, both replays, and the canonical load run re-done on the final code before any claim. | **Yes.** It caught that the first warm-up design was useless (early-refused); the numbers here come from the final code. |
| Property testing | Owner decision: a seeded generator instead of Hypothesis (no new dependency). No async-specific plugin was available. | **Yes.** Seeded cases found the splitter instability; it was reproducible from the seed, with no shrinking needed. |
