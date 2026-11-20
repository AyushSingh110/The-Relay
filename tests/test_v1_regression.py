"""V2-04 test 6: the v1 Table 3 questions give the v1 verdicts through the generalised path.

Each of the 21 questions (setting small_fast) is replayed through today's pipeline twice:
the v1 path, and the ``v1-papers`` collection (store, document registry, index registry).
The model is replaced by the recorded v1 answer (``relay.experiments.table3_replay``), so
no real call is made. Retrieval uses the real v1 indexes and local models (offline); the
test is skipped when those gitignored files are absent. The evidence run is only read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relay.app.bootstrap import V1_COLLECTION_ID, ensure_dev_tenant, register_v1_papers
from relay.app.collections import StoreCollectionResolver
from relay.experiments.table3_replay import (
    TABLE3_RUN,
    RecordedRow,
    load_rows,
    markdown_table,
    pipeline_factory,
    replay,
)
from relay.orchestrator.verifier import is_refusal_form, split_sentences
from relay.retrieval.hybrid_index import INDEX_MANIFEST, RETRIEVAL_SETTINGS
from relay.retrieval.registry import IndexRegistry
from relay.store import open_store

PAPERS, INDEXES, MODELS = Path("data/papers"), Path("data/indexes"), Path("data/models")
RUN = Path("evidence/experiments") / TABLE3_RUN
SETTINGS = ("small_fast", "balanced_rerank", "large_wide")
pytestmark = pytest.mark.skipif(
    not (all((INDEXES / s / INDEX_MANIFEST).is_file() for s in SETTINGS) and MODELS.is_dir()
         and (RUN / "traces.jsonl").is_file()),
    reason="the v1 indexes, local models or the Table 3 evidence run are absent")


def refusal_shaped(rows: list[RecordedRow]) -> set[str]:
    """V2-07 L3: questions whose recorded v1 answer contains a refusal-shaped sentence
    ("the passages do not state ..."). v1 verified them (every one was rubric-graded wrong);
    L3 now changes their verdict, as the owner decided (docs/DECISIONS.md, 2026-11-20)."""
    return {r.question_id for r in rows if r.raw_answer is not None
            and any(is_refusal_form(s) for s in split_sentences(r.raw_answer))}


async def test_v1_papers_and_the_v1_path_reproduce_every_table3_verdict(
        tmp_path: Path) -> None:
    from relay.experiments.eval_runner import load_retrievers
    from relay.retrieval.embedder import FastEmbedEmbedder, FastEmbedReranker

    rows = [r for r in load_rows(RUN) if r.setting == "small_fast"]
    assert len(rows) == 21 and len({r.question_id for r in rows}) == 21
    retrievers, vocabulary = load_retrievers(SETTINGS, papers_dir=PAPERS, indexes_dir=INDEXES,
                                             models_dir=MODELS)  # as the v1 eval run did
    top_k = {name: RETRIEVAL_SETTINGS[name].top_k for name in retrievers}
    model_id = "qwen/qwen3.8-27b"
    v1 = await replay(rows, pipeline_factory(retrievers, top_k, vocabulary, model_id=model_id),
                      path="v1")
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        await register_v1_papers(store, papers_dir=PAPERS, index_dir=INDEXES,
                                 collections_dir=tmp_path / "collections")
        indexes = IndexRegistry(tmp_path / "collections",
                                embedder=FastEmbedEmbedder(MODELS, local_files_only=True),
                                reranker=FastEmbedReranker(MODELS, local_files_only=True))
        resolver = StoreCollectionResolver(store, indexes)
        build = pipeline_factory(retrievers, top_k, vocabulary, model_id=model_id,
                                 collections=resolver)
        collection = await replay(rows, build, path="v1-papers", collection_id=V1_COLLECTION_ID)
    failed = [r for r in v1 + collection if not r.matches]
    shaped = refusal_shaped(rows)
    assert shaped == {"q10", "q13"}  # the small_fast answers with a refusal-shaped sentence
    unexpected = [r for r in failed if r.row.question_id not in shaped]
    assert unexpected == [], "\n" + markdown_table(unexpected)
    # every other row still reproduces v1 exactly; these change only as decided: a
    # refusal-shaped sentence among claims blocks the buffered answer (a safe refusal)
    assert {(r.row.question_id, r.path) for r in failed} == {
        (q, path) for q in shaped for path in ("v1", "v1-papers")}
    for r in failed:
        assert (r.row.verdict, r.verdict, r.kind) == ("VERIFIED", "BLOCKED", "safe_refusal")


async def test_streaming_the_table3_answers_agrees_with_every_v1_verdict(
        tmp_path: Path) -> None:
    """V2-05: the recorded answers, streamed token by token through the per-sentence
    verifier and the final pass, give v1's verdict on every small_fast row."""
    from relay.experiments.eval_runner import load_retrievers
    from relay.experiments.table3_replay import replay_streaming

    rows = [r for r in load_rows(RUN) if r.setting == "small_fast"]
    retrievers, vocabulary = load_retrievers(SETTINGS, papers_dir=PAPERS, indexes_dir=INDEXES,
                                             models_dir=MODELS)
    top_k = {name: RETRIEVAL_SETTINGS[name].top_k for name in retrievers}
    results = await replay_streaming(
        rows, pipeline_factory(retrievers, top_k, vocabulary, model_id="qwen/qwen3.8-27b"))
    assert len(results) == 21
    disagreements = [r for r in results if not r.agrees]
    shaped = refusal_shaped(rows)
    assert {r.row.question_id for r in disagreements} == shaped, disagreements
    for r in disagreements:  # L3 stops them; verified sentences before it stay (partial)
        assert r.verdict == "BLOCKED"
        assert r.stopped in ("refusal_as_claim", "refusal_with_content"), r
    blocked = [r for r in results if r.row.verdict == "BLOCKED"]
    assert blocked and all(r.verdict == "BLOCKED" for r in blocked)
