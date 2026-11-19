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
    load_rows,
    markdown_table,
    pipeline_factory,
    replay,
)
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
    assert failed == [], "\n" + markdown_table(failed)
