"""V2-05 tests: POST /v1/ask is served by ``relay serve`` (sqlite mode) and its flights
stop at shutdown. Embedded stand-in on 127.0.0.1; :memory: store; no API key."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from relay.api.routes.ask_v1 import AskV1Context, Flight
from relay.app import compose
from tests.test_compose_collections import env, settings_for
from tests.test_index_registry import FakeEmbedder
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever


async def test_sqlite_mode_serves_v1_ask_and_stops_its_flights_at_shutdown(
        tmp_path: Path) -> None:
    async with compose.open_runtime(
            settings_for(tmp_path, index_dir=tmp_path / "none"), env(),
            retrievers={"small_fast": StubRetriever(PASSAGES)},
            collection_embedder=FakeEmbedder()) as runtime:
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorised = await client.post("/v1/ask", json={"question": "q?"})
            assert unauthorised.status_code == 401  # mounted (not 404)
        assert runtime.ask_v1 is not None
        flight = Flight("k")
        flight.channel.task = asyncio.create_task(asyncio.sleep(30))
        runtime.ask_v1.flights["k"] = flight
        pending = flight.channel.task
    assert pending.cancelled()  # shutdown cancelled the live flight


async def test_aclose_cancels_every_live_flight() -> None:
    context = AskV1Context(service=None, store=None)  # type: ignore[arg-type]
    tasks = []
    for key in ("a", "b"):
        flight = Flight(key)
        flight.channel.task = asyncio.create_task(asyncio.sleep(30))
        context.flights[key] = flight
        tasks.append(flight.channel.task)
    await context.aclose()
    assert all(t.cancelled() for t in tasks) and not context.flights
