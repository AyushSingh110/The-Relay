"""V2-03 tests: ``POST /ask`` with and without ``collection_id`` (test 9 of the phase prompt).

The app is ``create_app`` with a scripted provider (no network, no API key), a :memory:
store and collections ingested by the real worker with the fake embedder.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay.api.app import create_app
from relay.app.collections import StoreCollectionResolver
from relay.control.admission import AdmissionQueue
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.orchestrator.pipeline import PipelineConfig
from relay.providers.base import ScriptedProvider
from relay.retrieval.registry import IndexRegistry
from relay.store import open_store
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import GOOD_ANSWER, PASSAGES, StubRetriever, reply

BASALT = "Basalt is a volcanic rock formed from lava [P1]."


class ListSink:
    run_id = None

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append_trace(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))


class Service:
    def __init__(self, env: Env, replies: list[Any], *, collections: bool = True) -> None:
        self.env = env
        self.provider = ScriptedProvider(replies)
        self.sink = ListSink()
        resolver = StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                                    embedder=FakeEmbedder()))
        app = create_app(
            provider=self.provider, retrievers={"small_fast": StubRetriever(PASSAGES)},
            top_k={"small_fast": 4},
            pipeline_config=PipelineConfig(model_id="test-model", max_completion_tokens=300),
            guard=ProviderGuard(GuardConfig(max_concurrency=2)),
            queue=AdmissionQueue(capacity=16, service_rate_per_s=100.0, max_active=1),
            corpus_sha256="c" * 64, run=self.sink,
            collections=resolver if collections else None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test")
        self.n = 0

    async def ask(self, question: str, **fields: Any) -> tuple[int, dict[str, Any]]:
        self.n += 1
        body = {"request_id": f"r{self.n}", "question": question, **fields}
        response = await self.client.post("/ask", json=body)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            events = {}
            for block in response.text.strip().split("\n\n"):
                name, data = block.split("\n", 1)
                events[name.removeprefix("event: ")] = json.loads(data.removeprefix("data: "))
            return response.status_code, events
        return response.status_code, response.json()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        yield Env(store, tmp_path)


async def test_ask_without_a_collection_is_the_v1_path(env: Env) -> None:
    service = Service(env, [reply(GOOD_ANSWER)])
    async with service.client:
        status, events = await service.ask("What is d_model in the encoder?",
                                           retrieval_setting="small_fast")
        assert status == 200
        assert events["final"]["retrieval_setting"] == "small_fast"
        assert events["final"]["verification_verdict"] == "VERIFIED"
        assert {c["paper_id"] for c in events["answer"]["citations"]} <= {
            p.paper_id for p in PASSAGES}
        status, body = await service.ask("What is d_model?")  # setting still required
        assert status == 422 and body["details"][0]["loc"] == ["retrieval_setting"]


async def test_ask_with_a_collection_answers_from_that_collection(env: Env) -> None:
    cid = await env.collection("geology")
    doc, _ = await env.upload(cid, ["Basalt is a volcanic rock formed from lava"],
                              title="Rocks")
    await env.worker().run_once()
    service = Service(env, [reply(BASALT)])
    async with service.client:
        status, events = await service.ask("What is basalt?", collection_id=cid)
    assert status == 200, events
    assert events["final"]["retrieval_setting"] == "small_fast"  # the collection's preset
    assert events["final"]["verification_verdict"] == "VERIFIED"
    assert {c["paper_id"] for c in events["answer"]["citations"]} == {doc}
    requests = [r for r in service.sink.records if r["record"] == "request"]
    assert requests[-1]["collection_id"] == cid


async def test_unknown_empty_and_ambiguous_collection_requests(env: Env) -> None:
    empty = await env.collection("empty")
    service = Service(env, [])
    async with service.client:
        status, body = await service.ask("q?", collection_id="no-such")
        assert (status, body["error"]) == (404, "collection_not_found")
        status, body = await service.ask("q?", collection_id=empty)
        assert (status, body["error"]) == (409, "collection_not_ready")
        status, body = await service.ask("q?", collection_id=empty,
                                         retrieval_setting="small_fast")
        assert status == 422 and body["details"][0]["type"] == "set_by_collection"
    assert service.provider.requests == []
    without = Service(env, [], collections=False)
    async with without.client:
        status, body = await without.ask("q?", collection_id=empty)
        assert status == 422 and body["details"][0]["type"] == "collections_disabled"


async def test_cached_answers_are_kept_per_collection_and_version(env: Env) -> None:
    first, second = await env.collection("a"), await env.collection("b")
    for cid in (first, second):
        await env.upload(cid, ["Basalt is a volcanic rock formed from lava"], title="Rocks")
    await env.worker().run_once()
    service = Service(env, [reply(BASALT)] * 4)
    async with service.client:
        statuses = []
        for cid in (first, second, first):
            _, events = await service.ask("What is basalt?", collection_id=cid)
            statuses.append(events["final"]["cache_status"])
        assert statuses == ["miss", "miss", "exact_hit"]
        await env.upload(first, ["Obsidian is volcanic glass"], title="Glass")
        await env.worker().run_once()  # version bump: earlier answers no longer apply
        _, events = await service.ask("What is basalt?", collection_id=first)
        assert events["final"]["cache_status"] == "miss"
        _, events = await service.ask("What is basalt?", retrieval_setting="small_fast")
        assert events["final"]["cache_status"] == "miss"  # the v1 path has its own entries
