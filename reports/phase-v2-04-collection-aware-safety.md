# Phase V2-04 Implementation Report — Collection-aware Scope, Gate and Verifier

> **Phase ID:** `v2-04`
> **Date:** `2026-11-19 11:31 IST`
> **Status:** `Completed`.
> - Verified in Conda env `the-relay` (Python 3.12.16): **656 passed**, 0 failed, 0 skipped (620 before the phase, 36 new); `ruff check .` clean.
> - **v1 regression: 84 of 84 replays match Table 3** (21 questions × 3 settings on the v1 path, plus 21 through the `v1-papers` collection).
> - **Real Groq calls: 0** (the spot-check cap was not needed). `evidence/` and `results.csv` are unchanged.
>
> **Owner Approval:** `Pending Review`

---

## 1. Objective

From the owner's prompt (plan V2-04; `docs/ARCHITECTURE_V2.md` §6):

**Remove the three-paper constants**
- The scope analyser, evidence gate and verifier take a collection's cue set instead of the hard-coded v1 papers.
- Refusal messages name the collection's own documents.

**v1 compatibility**
- The pipeline builds the cue set from `DocumentRegistry.get_cues(collection_id)`.
- `v1-papers` must behave exactly as v1.

**Tests and regression**
- Tests 1–7.
- A 21-question v1 regression against Table 3, using recorded answers.

**Done when:**
- 620 + 7 tests pass;
- every Table 3 verdict matches;
- no paper names remain in the scope, gate and verifier modules;
- ruff is clean; no Groq calls; `evidence/` and `results.csv` unchanged.

**Corrections to the prompt**, found while reading the code and reported before starting:
- The v1 papers are Attention, Moshi and H-Net (not FlashAttention).
- Scope lives in `retrieval/scope.py` and the gate in `orchestrator/evidence_gate.py`. Both were functions, not classes.
- The user-facing refusal messages were in `pipeline.py`, and the system prompt in `prompt.py`, which hard-coded "three research papers". The verifier itself produces no messages.
- The pipeline was already fetching the collection's cues from `DocumentRegistry.get_cues` (since V2-03). The gate re-query, the verifier, the messages and the prompt were still v1.

## 2. Implementation Summary

**The cue set** (`relay/retrieval/cues.py`): `CueSet` / `DocumentCues`, with no paper data.
- Each document has `title`, `short_names`, `arxiv_id` / `version`, `display_name`, `strong` patterns (title, short names, aliases, arXiv id) and `weak` patterns (topics).
- `titles`, `short_names` and `arxiv_ids` are the plain views the prompt asked for.
- Patterns must compile. `derive_document_cues` derives cues for an upload.

**The v1 corpus** (`relay/ingestion/v1_corpus.py`): the only place the v1 papers are defined. The curated cues moved unchanged and are pinned to the old constants by a test.

**The three safety components:**
- **Scope** (`scope.py`): `ScopeAnalyser(cue_set).analyze(...)`, with patterns compiled once. `analyze_scope(q)` keeps the v1 default.
- **Gate** (`evidence_gate.py`): `EvidenceGate(cue_set)`. The re-query sentence choice and the version refusal use the corpus's documents. `decide` and `requery_text` keep the v1 default.
- **Verifier** (`verifier.py`): `Verifier(cue_set, foreign=...)`.
  - The name check uses the corpus's names.
  - **Foreign-name isolation:** the tenant's documents in other collections are blocked by their title and short names, never by aliases or topics. Documents with the same content are excluded, and so is any name a document in this corpus also uses.
  - `verify(a, p)` keeps the v1 default.

**Messages and prompt:**
- `orchestrator/messages.py`: `refusal_messages(cue_set)`. The v1 corpus renders the exact v1 texts. Collections name their documents, or give only the count above 10.
- `prompt.py`: `system_prompt(cue_set)`, `prompt_version(cue_set)` (`p05-1` for v1, `p06-1` for collections), and passage headers using the corpus's titles. The v1 prompt is byte-identical to the committed one.

**Wiring** (`pipeline.py`): `CorpusSafety` bundles the analyser, gate, verifier, messages and prompt version for one cue set.
- **When it is built:** the v1 path builds it once, when the pipeline is created. A collection's resolver builds it and caches it per (collection version, tenant fingerprint).
- **Cache key:** the answer cache includes the prompt version and the tenant fingerprint.

**Storage** (migration 003): `document.cues` (JSON).
- Written at ingestion (derived, or curated for v1 papers) and at `v1-papers` registration (curated).
- Refreshed at start-up when the stored v1 cues differ from the curated ones.
- Unreadable rows fall back to derivation, with a warning.

**Regression harness:**
- `relay/experiments/table3_replay.py` and `scripts/replay_table3.py` replay the Table 3 run: the recorded raw answers through a `ScriptedProvider`, with real retrieval.
- The evidence run is only read.

## 3. Files Created

**Source:** `src/relay/retrieval/cues.py`, `src/relay/ingestion/v1_corpus.py`, `src/relay/orchestrator/messages.py`, `src/relay/experiments/table3_replay.py`, `scripts/replay_table3.py`.

**Tests:**

| File | Tests | Covers |
| :-- | --: | :-- |
| `tests/test_cues.py` | 3 | The cue-set model and derivation |
| `tests/test_v1_corpus.py` | 2 | The v1 cue set equals the old constants |
| `tests/test_collection_aware_safety.py` | 14 | Tests 1–4 and 7 (verifier level), the default v1 corpus, source checks for paper names, prompt and message review fixes |
| `tests/test_cue_storage.py` | 5 | Migration 003, stored and derived cues, v1 backfill |
| `tests/test_v1_regression.py` | 1 | Test 6 |
| `tests/test_v2_04_review_fixes.py` | 7 | Whole-phase review fixes |

**Report:** this report.

## 4. Files Modified

**Source**
- `src/relay/retrieval/scope.py`, `src/relay/orchestrator/evidence_gate.py`, `src/relay/orchestrator/verifier.py`: constants removed; classes added.
- `src/relay/orchestrator/prompt.py`, `src/relay/orchestrator/pipeline.py`.
- `src/relay/ingestion/registry.py`: cue set, stored cues, `foreign_cues`, `tenant_fingerprint`.
- `src/relay/app/collections.py`: cached `CorpusSafety`; `describe` returns a cache tag.
- `src/relay/api/app.py`: the cache key includes the tag.
- `src/relay/ingestion/worker.py`: stores cues at completion.
- `src/relay/app/bootstrap.py`: stores and refreshes the v1 cues.
- `src/relay/store/migrations.py`, `src/relay/store/repositories.py`: migration 003; `Document.cues`, `set_cues`, `complete(cues=)`.

**Docs:** `docs/DECISIONS.md` (four owner decisions, one [PROPOSED] entry).

**Tests**
- Updated from the V2-03 cue model to `CueSet`: `tests/test_scope_cues.py`, `tests/test_document_registry.py`, `tests/test_compose_collections.py`, `tests/test_v2_03_review_fixes.py`.
- `tests/test_store_migration_002.py`: made version-agnostic.
- `tests/test_pipeline_collections.py`: 4 tests added (test 5, test 7, the foreign-name variant, cross-tenant isolation).

**Log:** `ENGINEERING_LOG.md`.

## 5. Technical Decisions

**Owner decisions (2026-11-19, recorded in `docs/DECISIONS.md`):**
1. The cue set has aliases and topics beyond titles, short names and arXiv ids. The v1 values live in one v1 corpus definition.
2. Cues are stored in `document.cues` (migration 003).
3. Verifier isolation is a foreign-name check over the tenant's other documents.
4. The prompt and messages are rendered from the cue set, with the v1 text byte-identical (`p05-1`) and collections on `p06-1`.

**My rulings ([PROPOSED]; each says what it costs if wrong):**

1. **Aliases and topics are stored as the `strong` / `weak` pattern lists, not as separate fields.** v1's pattern order interleaves the arXiv id among H-Net's aliases, and that order appears in recorded scope reasons. This is recorded in DECISIONS as pending your approval.
   - Cost if wrong: a later data migration if separate alias fields are wanted.
2. **The gate's re-query cues are the corpus's full strong and weak cues, not only short names and arXiv ids as the prompt says.** Using only those would change three v1 re-queries.
   - Cost if wrong: none for v1, which is identical either way.
3. **Defaults stay v1.** `analyze_scope`, `decide`, `requery_text`, `verify` and `build_messages` without a cue set use the v1 corpus, so every v1 test and caller is unchanged. The modules themselves name no paper.
4. **When the safety objects are built.**
   - v1: once, when the pipeline is created.
   - Collections: in the resolver, cached per (version, tenant fingerprint).
   - The fingerprint changes when any of the tenant's collections changes, which rebuilds the foreign names and invalidates cached answers.
5. **A collection holding exactly the three v1 papers gets the v1 cue set**, and so the v1 wording and prompt. Adding any document gives "documents" wording and `p06-1`.
6. **A v1 paper keeps its curated cues in any collection.** It is identified by exact arXiv id and version.
7. **The regression replays recorded answers through a `ScriptedProvider`.** The stand-in writes its own synthetic answers and cannot reproduce Table 3's verdicts. Everything except the model is the real code path.
8. **Foreign names exclude aliases and topics.** This came from a review finding: v1's alias "helium" is an ordinary word in a chemistry collection.
   - Cost: a hallucinated foreign alias, for example "Kyutai", is not blocked by name in another collection. Number and identifier grounding still apply.

## 6. Commands Actually Executed

```text
python -m ruff check .                         # after every file; last: All checks passed!
python -m pytest -q -p no:cacheprovider        # after every file (counts in §7)
python -m pytest tests/<new file> -q           # each new test RED first
code-review skill (low, per file x 7; medium, whole phase x 1)
python scratchpad/v204_sensitivity.py          # sabotage checks of the replay harness
python scripts/replay_table3.py --out <scratchpad>/v204_table3_replay.md   # twice
python scratchpad/v204_demo.py                 # unanswerable questions in a new collection
git status --short ; git show HEAD:src/relay/orchestrator/prompt.py (byte comparison)
```

## 7. Test Results

**Full suite after each file:**

| After | Result |
| :-- | :-- |
| `cues.py` | 623 passed |
| `v1_corpus.py` | 625 passed (first run 1 failed: the known v1 timing flake, see below) |
| scope | 629 passed |
| gate | 631 passed |
| verifier | 634 passed |
| messages / prompt | 636 passed, then 639 after the review fixes |
| storage | 644 passed |
| pipeline | 648 passed |
| regression | 649 passed |
| whole-phase review fixes | 656 passed |

**Final:** `656 passed`; `ruff check .` → `All checks passed!`

**Prompt tests 1–7:**

| # | Test | Where |
| :-- | :-- | :-- |
| 1 | Custom cue set: a question about its document is in scope | `test_collection_aware_safety::test_a_question_about_a_collection_document_is_in_scope` |
| 2 | Custom cue set: an unrelated question is out of scope | `…::test_an_unrelated_question_is_out_of_scope_for_the_collection` |
| 3 | The gate's re-query uses the collection's short names | `…::test_the_gate_requery_uses_the_collection_short_names` |
| 4 | Refusal messages name the collection's documents | `…::test_refusal_messages_name_the_collections_own_documents` (plus the v1 byte-identity test) |
| 5 | Pipeline on a new collection: correct scope decision | `test_pipeline_collections::test_a_new_collection_gets_its_own_scope_gate_prompt_and_messages` |
| 6 | `v1-papers`: identical verdicts for all 21 questions | `test_v1_regression::test_v1_papers_and_the_v1_path_reproduce_every_table3_verdict` |
| 7 | A Moshi question in an Attention-only collection is refused at verifier level | `test_pipeline_collections::test_a_moshi_answer_in_an_attention_only_collection_is_blocked`, plus `…::test_names_of_the_tenants_other_documents_are_blocked_not_just_v1_names` and the verifier-level unit test |

All pass.

**Test 7 needed a stronger variant.** The literal test 7 also passed before the wiring, because the old v1 verifier knew "Moshi" globally. I added a variant with two uploaded documents ("Basalt Atlas", "Granite Primer"): it failed before the foreign-name wiring (VERIFIED) and passes after (BLOCKED).

**The v1 timing flake.** `tests/test_standin.py::test_stragglers_are_much_slower` failed once more, for the third time across V2-03 and V2-04: 186 ms against its 190 ms floor. The v1 test and the stand-in code are untouched. It passed on the rerun and in every later run.

## 8. Actual Output and Generated Artifacts

### V1 regression check (Table 3, run `exp-20261113T064542Z-c482696`)

The replay compares each row's verdict, kind, error code, delivered text, retrieved passages, gate actions and full scope decision (including its reason strings). Each cell below reads: v1 verdict / replay verdict(s).

| Q | small_fast: v1 / replay v1 path / replay v1-papers | balanced_rerank: v1 / replay | large_wide: v1 / replay | Match |
| :-- | :-- | :-- | :-- | :-- |
| q01 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q02 | BLOCKED / BLOCKED / BLOCKED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q03 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q04 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q05 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q06 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q07 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q08 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q09 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q10 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q11 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q12 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q13 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q14 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q15 | VERIFIED / VERIFIED / VERIFIED | VERIFIED / VERIFIED | VERIFIED / VERIFIED | yes |
| q16 | REFUSAL_VERIFIED / REFUSAL_VERIFIED / REFUSAL_VERIFIED | EARLY_REFUSAL / EARLY_REFUSAL | REFUSAL_VERIFIED / REFUSAL_VERIFIED | yes |
| q17 | REFUSAL_VERIFIED / REFUSAL_VERIFIED / REFUSAL_VERIFIED | EARLY_REFUSAL / EARLY_REFUSAL | REFUSAL_VERIFIED / REFUSAL_VERIFIED | yes |
| q18 | EARLY_REFUSAL / EARLY_REFUSAL / EARLY_REFUSAL | EARLY_REFUSAL / EARLY_REFUSAL | EARLY_REFUSAL / EARLY_REFUSAL | yes |
| q19 | REFUSAL_VERIFIED / REFUSAL_VERIFIED / REFUSAL_VERIFIED | EARLY_REFUSAL / EARLY_REFUSAL | REFUSAL_VERIFIED / REFUSAL_VERIFIED | yes |
| q20 | REFUSAL_VERIFIED / REFUSAL_VERIFIED / REFUSAL_VERIFIED | EARLY_REFUSAL / EARLY_REFUSAL | REFUSAL_VERIFIED / REFUSAL_VERIFIED | yes |
| q21 | REFUSAL_VERIFIED / REFUSAL_VERIFIED / REFUSAL_VERIFIED | REFUSAL_VERIFIED / REFUSAL_VERIFIED | NOT_RUN / NOT_RUN | yes |

**Result: 84 of 84 replays match. No regression; nothing to explain.**
- q02 small_fast is BLOCKED in v1 and in the replay. It is one of v1's own outcomes, not a new difference.
- q21 large_wide was a provider failure in v1 (`ProviderConnectionError`). It was replayed as the same failure, giving NOT_RUN.
- The full run was made twice, before and after the whole-phase fixes (which include compiling patterns once). Both runs gave 84 of 84.

**The harness detects differences.** Two deliberate sabotages:
- With the vocabulary removed, 21 of 21 rows differ (scope, and unexpected model calls).
- With one recorded answer corrupted, q01 flips from VERIFIED to BLOCKED and is reported.

### Demo (plan V2-04)

The six unanswerable questions (q16–q21) were asked in a new one-document geology collection, with a model that must not be called:
- **All six were refused early,** through the low-coverage gate.
- The refusal read: "I can only answer from one document (Columnar Basalt Jointing), and it does not cover this question."
- In v1 (small_fast), five of them were refused by the model itself (REFUSAL_VERIFIED).

### Other checks

- **v1 system prompt:** byte-identical to the committed `prompt.py` (compared by loading the `HEAD` copy).
- **Paper names:** a source scan of `scope.py`, `evidence_gate.py`, `verifier.py`, `messages.py`, `prompt.py` and `pipeline.py` finds none (also asserted by a test for the three named modules).

## 9. Errors and Root-Cause Analysis

| Problem | Cause | Fix |
| :-- | :-- | :-- |
| 15 collection errors after the scope change | The verifier still imported `STRONG_CUES` | Bridged to `V1_CUE_SET.name_cues()` (same values), then replaced in the verifier step |
| A patch script aborted | ruff's import sorting had moved an anchor line | Re-anchored. Patches are all-or-nothing, so nothing was half-applied |
| Test 5 ran out of scripted replies | Test bug: the final v1 call needed a second reply | Added the reply |
| Tests 7 and the foreign variant first passed `VERIFIED` | Test bug: `Env.collection()` creates a new tenant per call, so the two collections were in different tenants and correctly not foreign | Same-tenant helper |
| Gate test expectation wrong | My comparison question's first sentence named both documents | Reworded the question |
| Source-check test failed | My own docstring used "helium" as an example | Reworded |
| Two review-fix tests initially wrong | The strict schema needs a tuple, not a list; the helium scenario's passage itself contained "helium" | Corrected to the reviewer's scenario |

**Side effect from the review plugin.** The whole-phase `code-review` run executed `uv run --extra dev` on its own initiative. That installed packages from PyPI into the pre-existing, gitignored `.venv` and rewrote the gitignored `uv.lock` (modified 2026-11-19 11:20).
- Git tracks neither; no source, test, `evidence/` or `results.csv` file was affected.
- I did not revert it, because the previous `uv.lock` is not recoverable from git.

## 10. Fixes Applied

**Per-file code reviews** (low effort, 7 runs) found 6 issues:
- braces in document names crashed prompt formatting;
- the arXiv label came from `paper_id`;
- the version message had empty brackets without arXiv documents;
- `render_passages` showed v1 titles to the model while the verifier used corpus titles (closed by passing the cue set through);
- one duplication;
- a docstring that breaks under `-OO`.

All were fixed. The first three got RED tests first.

**The whole-phase review** (medium) found 10. Each accepted Important finding got a test that failed first (6 RED, then green):

| Finding | Fix |
| :-- | :-- |
| 1 Same document in two collections counted as foreign, blocking correct answers | Exclude same-content documents and names shared with own documents |
| 2 v1 aliases such as "helium" became foreign names in other collections | Foreign documents are named by title and short names only |
| 3 / 6 Cache key ignored tenant changes and the prompt version; foreign set and safety objects rebuilt per request | Tenant fingerprint plus prompt version in the cache key; `CorpusSafety` cached in the resolver |
| 4 Stale stored v1 cues never refreshed | Refreshed at start-up when they differ from the curated cues |
| 5 Stored regexes not validated | Patterns must compile; bad rows fall back to derivation |
| 8 Unbounded document list in refusals | Count only above 10 documents, as the prompt does |
| 7 Patterns recompiled per call | Compiled once (refactor; the 84-row replay is unchanged) |
| 9 The gate built a second analyser | Shares the bundle's analyser |
| 10 Representation differs from the DECISIONS wording | Recorded as a [PROPOSED] entry for your approval |

Ruling on a per-file finding: the replay replays only the recorded failure type present in Table 3 (`ProviderConnectionError`). Any other type would show up loudly as "replay failed", never as a false match.

## 11. Outstanding Issues

**Carried from V2-03 (not part of V2-04):**
- `/ask` with a `collection_id` still has no API-key or tenant check (V2-05).
- If the completion transaction itself fails after the index swap, the document's chunks stay in the index.

**New limits from this phase:**
- **Foreign-name check by design:**
  - a foreign document's aliases are not blocked by name (ruling 8);
  - foreign names come only from the same tenant;
  - a derived short name that is an ordinary word (for example "Survey" from "Survey: …") is still a foreign name, and could block an unrelated answer using that word without citing it.
- **Derived documents have no topic (weak) cues,** so the out-of-corpus signal on uploads relies on vocabulary coverage only.
- **The plan says "re-run the v1 evaluation questions".** That was done with recorded answers; no live spot-checks were needed.

## 12. Acceptance-Criteria Status

| Criterion | Status | Evidence |
| :-- | :-- | :-- |
| All 620 existing tests still pass | Met | 656 passed (the V2-03 tests that used the old cue model were updated to `CueSet`, see §4) |
| All 7 new tests pass | Met | §7 |
| 21-question v1 regression: every verdict matches Table 3 | Met | 21 of 21 via `v1-papers`; 84 of 84 including the v1 path and all settings |
| No hard-coded paper names in scope, gate and verifier | Met | Source tests and scan |
| Refusal messages use the collection's document names | Met | Test 4, test 5, demo |
| ruff clean | Met | `All checks passed!` |
| No real Groq calls | Met | Scripted provider only; spot-checks not needed |
| `results.csv` and `evidence/` unchanged | Met | §8 checks |

## 13. Remaining Risks

- **Foreign-name false blocks for generic short names.** They are bounded by the minimum length and by excluding aliases, but not eliminated.
- **Per-request overhead.** Each collection request now reads the tenant's collection list once, for the fingerprint, before using the cache.
- **The tenant fingerprint changes whenever any of the tenant's collections changes.** Cached answers in all of the tenant's collections are then recomputed once. That is correct, but costs extra model calls for busy tenants.

## 14. Recommended Next Phase

**V2-05** (verified streaming, and the authenticated `/v1/ask`). It would also close the `/ask` tenant gap.

Before that, please decide on:
- the [PROPOSED] cue representation (DECISIONS);
- rulings 2 and 8.

### Plugins used, and whether they helped

| Plugin | Used for | Helped? |
| :-- | :-- | :-- |
| `superpowers:test-driven-development` | Every behaviour test ran RED first. | **Yes.** It exposed that literal test 7 passed for the wrong reason (v1 global names), which led to the stronger foreign-name variant. It also caught 4 test-setup mistakes before they could hide behaviour. Exception: the replay harness was written before its test, as a characterization harness. |
| `code-review` (low, per file; medium, whole phase) | As requested, after every file, plus one whole-phase pass. | **Yes.** Per file, the brace crash and the prompt/verifier title mismatch were real bugs. The whole-phase pass found cross-file problems no single-file review could see: the same document counted as foreign, aliases as foreign names, the stale cache key, unrefreshed v1 cues. **Cost:** that run installed packages into `.venv` by itself (§9). |
| `superpowers:verification-before-completion` | A fresh full suite, ruff, the replay run twice, sabotage checks, and the byte comparison of the v1 prompt before any claim. | **Yes.** The sabotage checks show the 84/84 result is not vacuous. |

No additional plugins were installed: the three requested were already available.
