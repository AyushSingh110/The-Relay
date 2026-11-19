"""V2-03 tests: the pipeline with a collection_id (test 8) and without one (v1 path).

Collections are ingested with the real worker and the fake embedder; the model is a
ScriptedProvider; stores are :memory:. No network, no API key.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from relay.app.collections import StoreCollectionResolver
from relay.ingestion.registry import CollectionNotFoundError
from relay.orchestrator.pipeline import (
    Pipeline,
    PipelineConfig,
    PipelineRequest,
    SingleCallGenerator,
)
from relay.orchestrator.tools import ToolGateway
from relay.providers.base import ScriptedProvider
from relay.retrieval.registry import CollectionNotReadyError, IndexRegistry
from relay.store import open_store
from relay.telemetry.spans import ServerSpanTracker
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever, reply


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        yield Env(store, tmp_path)


def pipeline_for(env: Env, replies: list[Any]) -> tuple[Pipeline, ScriptedProvider]:
    provider = ScriptedProvider(replies)
    resolver = StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                                embedder=FakeEmbedder()))
    pipeline = Pipeline(
        gateway=ToolGateway({"small_fast": StubRetriever(PASSAGES)}),
        generator=SingleCallGenerator(provider),
        config=PipelineConfig(model_id="test-model", max_completion_tokens=200),
        top_k={"small_fast": 4}, collections=resolver)
    return pipeline, provider


async def run(pipeline: Pipeline, question: str, collection_id: str | None = None,
              setting: str = "small_fast") -> Any:
    request = PipelineRequest(request_id="r-1", question=question, retrieval_setting=setting,
                              collection_id=collection_id)
    return await pipeline.run(request, ServerSpanTracker())


async def test_a_collection_request_retrieves_only_from_that_collection(env: Env) -> None:
    first, second = await env.collection("geology"), await env.collection("biology")
    rocks, _ = await env.upload(first, ["Basalt is a volcanic rock formed from lava"],
                                title="Rocks")
    cells, _ = await env.upload(second, ["Mitochondria are the powerhouse of the cell"],
                                title="Cells")
    await env.worker().run_once()
    answer = "Basalt is a volcanic rock formed from lava line 0 [P1]."
    pipeline, provider = pipeline_for(env, [reply(answer), reply(answer)])

    in_first = await run(pipeline, "What is basalt?", first)
    assert {p.paper_id for p in in_first.passages} == {rocks}
    assert in_first.retrieval_setting == "small_fast"

    in_second = await run(pipeline, "What is basalt?", second)
    assert {p.paper_id for p in in_second.passages} <= {cells}
    assert rocks not in {p.paper_id for p in in_second.passages}

    default = await run(pipeline, "What is d_model in the encoder?")  # no collection: v1 path
    assert {p.paper_id for p in default.passages} <= {p.paper_id for p in PASSAGES}


async def test_collection_cues_drive_scope_analysis(env: Env) -> None:
    cid = await env.collection()
    doc, _ = await env.upload(cid, ["Plate tectonics explains continental drift"],
                              title="Plate Tectonics: an overview")
    await env.worker().run_once()
    pipeline, _ = pipeline_for(env, [reply("INSUFFICIENT_EVIDENCE")])
    outcome = await run(pipeline, "What does Plate Tectonics say about drift?", cid)
    assert outcome.trace.scope.strong_papers == (doc,)  # from the store, not v1 constants


async def test_unknown_or_empty_collections_fail_clearly(env: Env) -> None:
    pipeline, provider = pipeline_for(env, [])
    with pytest.raises(CollectionNotFoundError):
        await run(pipeline, "anything", "no-such-collection")
    empty = await env.collection("empty")
    with pytest.raises(CollectionNotReadyError):
        await run(pipeline, "anything", empty)
    assert provider.requests == []
