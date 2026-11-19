"""Replay the v1 Table 3 run through the generalised pipeline and print the comparison.

    python scripts/replay_table3.py [--out PATH]

All 63 rows (21 questions x 3 settings) go through the v1 path; the 21 small_fast rows also
go through the ``v1-papers`` collection. The model is replaced by the recorded v1 answers,
so no real call is made. The evidence run is only read; the table is printed and, with
``--out``, written to PATH (use a scratch location, never ``evidence/``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from relay.app.bootstrap import V1_COLLECTION_ID, ensure_dev_tenant, register_v1_papers
from relay.app.collections import StoreCollectionResolver
from relay.experiments.eval_runner import load_retrievers
from relay.experiments.table3_replay import (
    TABLE3_RUN,
    load_rows,
    markdown_table,
    pipeline_factory,
    replay,
    replay_streaming,
    streaming_table,
)
from relay.retrieval.embedder import FastEmbedEmbedder, FastEmbedReranker
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.retrieval.registry import IndexRegistry
from relay.store import open_store

PAPERS, INDEXES, MODELS = Path("data/papers"), Path("data/indexes"), Path("data/models")
SETTINGS = ("small_fast", "balanced_rerank", "large_wide")
MODEL_ID = "qwen/qwen3.8-27b"


async def run_streaming(out: Path | None) -> int:
    """V2-05: the recorded answers streamed through the per-sentence verifier."""
    rows = load_rows(Path("evidence/experiments") / TABLE3_RUN)
    retrievers, vocabulary = load_retrievers(SETTINGS, papers_dir=PAPERS, indexes_dir=INDEXES,
                                             models_dir=MODELS)
    top_k = {name: RETRIEVAL_SETTINGS[name].top_k for name in retrievers}
    results = await replay_streaming(rows, pipeline_factory(retrievers, top_k, vocabulary,
                                                            model_id=MODEL_ID))
    with tempfile.TemporaryDirectory() as tmp:
        async with open_store(":memory:") as store:
            await ensure_dev_tenant(store)
            await register_v1_papers(store, papers_dir=PAPERS, index_dir=INDEXES,
                                     collections_dir=Path(tmp))
            resolver = StoreCollectionResolver(store, IndexRegistry(
                Path(tmp), embedder=FastEmbedEmbedder(MODELS, local_files_only=True),
                reranker=FastEmbedReranker(MODELS, local_files_only=True)))
            collection = await replay_streaming(
                [r for r in rows if r.setting == "small_fast"],
                pipeline_factory(retrievers, top_k, vocabulary, model_id=MODEL_ID,
                                 collections=resolver), collection_id=V1_COLLECTION_ID)
    answered = [r for r in results if r.row.raw_answer is not None]
    partial = sum(r.partial for r in answered)
    lines = [streaming_table(results), "",
             f"v1 path: {sum(r.agrees for r in results)} of {len(results)} streamed rows reach "
             f"v1's verdict; v1-papers: {sum(r.agrees for r in collection)} of "
             f"{len(collection)}.",
             f"Partial answers: {partial} of {len(answered)} rows where the model answered "
             f"({partial / len(answered):.1%}); stopped before any sentence: "
             f"{sum(r.stopped is not None and r.sentences == 0 for r in answered)}."]
    text = "\n".join(lines) + "\n"
    print(text)
    if out is not None:
        out.write_text(text, encoding="utf-8")
    agreed = all(r.agrees for r in results + collection)
    return 0 if agreed else 1


async def run(out: Path | None) -> int:
    rows = load_rows(Path("evidence/experiments") / TABLE3_RUN)
    retrievers, vocabulary = load_retrievers(SETTINGS, papers_dir=PAPERS, indexes_dir=INDEXES,
                                             models_dir=MODELS)
    top_k = {name: RETRIEVAL_SETTINGS[name].top_k for name in retrievers}
    v1 = await replay(rows, pipeline_factory(retrievers, top_k, vocabulary, model_id=MODEL_ID),
                      path="v1")
    with tempfile.TemporaryDirectory() as tmp:
        async with open_store(":memory:") as store:
            await ensure_dev_tenant(store)
            await register_v1_papers(store, papers_dir=PAPERS, index_dir=INDEXES,
                                     collections_dir=Path(tmp))
            resolver = StoreCollectionResolver(store, IndexRegistry(
                Path(tmp), embedder=FastEmbedEmbedder(MODELS, local_files_only=True),
                reranker=FastEmbedReranker(MODELS, local_files_only=True)))
            collection = await replay(
                [r for r in rows if r.setting == "small_fast"],
                pipeline_factory(retrievers, top_k, vocabulary, model_id=MODEL_ID,
                                 collections=resolver),
                path="v1-papers", collection_id=V1_COLLECTION_ID)
    results = v1 + collection
    matched = sum(r.matches for r in results)
    text = (markdown_table(results)
            + f"\n\n{matched} of {len(results)} replays match v1 (verdict, kind, error code, "
              "delivered text, passages, gate actions and scope decision).\n")
    print(text)
    if out is not None:
        out.write_text(text, encoding="utf-8")
    return 0 if matched == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the v1 Table 3 run through the generalised pipeline.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--streaming", action="store_true",
                        help="stream the answers through the V2-05 per-sentence verifier")
    args = parser.parse_args(argv)
    if args.out is not None and "evidence" in args.out.resolve().parts:
        parser.error("--out must not point into evidence/")
    return asyncio.run(run_streaming(args.out) if args.streaming else run(args.out))


if __name__ == "__main__":
    sys.exit(main())
