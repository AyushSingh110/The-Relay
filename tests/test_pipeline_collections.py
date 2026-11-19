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


# V2-04 test 5 ------------------------------------------------------------------------------
async def test_a_new_collection_gets_its_own_scope_gate_prompt_and_messages(env: Env) -> None:
    cid = await env.collection("geology")
    doc, _ = await env.upload(cid, ["Basalt is a volcanic rock formed from lava"],
                              title="Basalt Atlas: volcanic rocks of the world")
    await env.worker().run_once()
    pipeline, provider = pipeline_for(env, [reply("INSUFFICIENT_EVIDENCE")] * 2)
    outcome = await run(pipeline, "What does the Basalt Atlas say about basalt?", cid)
    assert outcome.trace.scope.strong_papers == (doc,)
    assert outcome.trace.prompt_version == "p06-1"
    system = provider.requests[0].messages[0].content
    assert system.startswith("You answer questions about one document (Basalt Atlas) using")
    assert "Basalt Atlas: volcanic rocks of the world" in provider.requests[0].messages[1].content
    assert outcome.kind == "llm_refusal"
    assert outcome.delivered_text == ("I can't answer this from the one document in the "
                                      "library: the passages I found do not contain the "
                                      "information needed.")
    v1 = await run(pipeline, "What is d_model in the encoder?")  # v1 path untouched
    assert v1.trace.prompt_version == "p05-1"


# V2-04 test 7 ------------------------------------------------------------------------------
async def same_tenant(env: Env, collection_id: str, name: str) -> str:
    """Another collection of the same tenant (``Env.collection`` makes a new tenant)."""
    first = await env.store.collections.get_by_id(collection_id)
    assert first is not None
    return (await env.store.collections.create(first.tenant_id, name)).id



async def test_a_moshi_answer_in_an_attention_only_collection_is_blocked(env: Env) -> None:
    attention = await env.collection("attention-only")
    await env.upload(attention, ["The Transformer uses multi-head attention over the encoder"],
                     title="Attention Is All You Need")
    speech = await same_tenant(env, attention, "speech")
    await env.upload(speech, ["Moshi is a speech-text foundation model for dialogue"],
                     title="Moshi: a speech-text foundation model for real-time dialogue")
    await env.worker().run_once()
    hallucinated = "Moshi uses multi-head attention over the encoder [P1]."
    pipeline, _ = pipeline_for(env, [reply(hallucinated)])
    outcome = await run(pipeline, "How does Moshi use attention over the encoder?", attention)
    assert {p.paper_id for p in outcome.passages} <= set(
        (await env.store.documents.list_for_collection(attention))[0].id.split())
    assert outcome.kind == "safe_refusal" and outcome.refused
    assert outcome.verification_verdict == "BLOCKED"
    failures = outcome.trace.verification.failures  # type: ignore[union-attr]
    assert [f.code for f in failures] == ["paper_mismatch"]


async def test_names_of_the_tenants_other_documents_are_blocked_not_just_v1_names(
        env: Env) -> None:
    rocks = await env.collection("rocks")
    await env.upload(rocks, ["Basalt is a volcanic rock formed from lava"],
                     title="Basalt Atlas: volcanic rocks of the world")
    minerals = await same_tenant(env, rocks, "minerals")
    await env.upload(minerals, ["Quartz is a hard mineral"],
                     title="Granite Primer: minerals of the crust")
    await env.worker().run_once()
    pipeline, _ = pipeline_for(env, [reply("Granite Primer says basalt forms from lava [P1].")])
    outcome = await run(pipeline, "What is basalt formed from?", rocks)
    assert outcome.verification_verdict == "BLOCKED"
    failures = outcome.trace.verification.failures  # type: ignore[union-attr]
    assert [f.code for f in failures] == ["paper_mismatch"]


async def test_foreign_names_come_only_from_the_same_tenant(env: Env) -> None:
    rocks = await env.collection("rocks")
    await env.upload(rocks, ["Basalt is a volcanic rock formed from lava"],
                     title="Basalt Atlas: volcanic rocks of the world")
    other_tenant = await env.collection("minerals")  # Env.collection: a different tenant
    await env.upload(other_tenant, ["Quartz is a hard mineral"],
                     title="Granite Primer: minerals of the crust")
    await env.worker().run_once()
    registry = StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                                embedder=FakeEmbedder()))
    assert await registry.documents.foreign_cues(rocks) == ()
