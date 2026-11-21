"""V2-05 silent-failure review fixes: one regression test per accepted finding (see the
phase report). Scripted providers, :memory: stores; no network."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay.api.app import AskRequest
from relay.api.routes.ask_v1 import Flight, _respond
from relay.app.bootstrap import ensure_dev_tenant
from relay.experiments.streaming_load import StreamingScenario, StreamResult, summarise_streaming
from relay.store import open_store
from tests.test_ask_v1 import BAD, S1, S2, Harness, answer, ask, geology, question
from tests.test_ingest_worker import Env


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


# 1: a failing cache write never changes a delivered answer's final --------------------
async def test_a_failing_cache_write_keeps_the_verified_final(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2)])

    def broken_put(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("cache down")

    monkeypatch.setattr(harness.app.state.relay.cache, "put", broken_put)
    stream = await ask(harness.app, question(cid))
    final = stream.of("final")[0]
    assert (final["status"], final["verdict"], final["partial"]) == ("ok", "VERIFIED", False)
    assert final["tokens_in"] > 0 and "stopped" not in stream.names


# 2 and 5: the flight record says why it stopped, and what a cancelled flight spent ------
async def test_the_flight_record_says_why_the_stream_stopped(env: Env) -> None:
    cid = await geology(env)
    records: list[dict[str, Any]] = []
    harness = Harness(env, [answer(S1, BAD, S2)])
    harness.app.state.relay.record_trace = records.append
    await ask(harness.app, question(cid))
    flight = next(r for r in records if r["record"] == "flight")
    stop = flight["pipeline"]["stream_stop"]
    assert stop["reason_class"] == "ungrounded" and stop["sentence_index"] == 2
    assert stop["failure_codes"] == ["ungrounded_number"]


async def test_a_cancelled_flight_records_its_estimated_spend(env: Env) -> None:
    cid = await geology(env)
    records: list[dict[str, Any]] = []
    harness = Harness(env, [answer(S1, S2, S1, S2, pieces=40)], delay_s=0.02)
    harness.app.state.relay.record_trace = records.append
    gone = asyncio.Event()

    def leave(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "sentence":
            gone.set()

    await asyncio.wait_for(ask(harness.app, question(cid), gone=gone, on_event=leave), 5)
    for _ in range(100):
        if any(r.get("end") == "cancelled" for r in records):
            break
        await asyncio.sleep(0.01)
    cancelled = next(r for r in records if r.get("end") == "cancelled")
    assert cancelled["provider_called"] is True and cancelled["tokens_in"] > 0
    assert cancelled["usage_source"] == "estimated"


# 3: a stream closed by shutdown after sentences says it was partial -------------------
async def test_a_shutdown_after_sentences_is_reported_as_partial(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, S1, S2, pieces=40)], delay_s=0.02)
    service = harness.app.state.relay
    target = await service.target(AskRequest(request_id="a", question="What is basalt?",
                                             collection_id=cid))
    gen = _respond(harness.context, target, request_id="a", arrived_ns=service.clock(),
                   collection_id=cid, buffered=False)
    chunks: list[bytes] = []
    async with asyncio.timeout(5):
        async for chunk in gen:
            chunks.append(chunk)
            if b"event: sentence" in chunk:
                break
        await harness.context.aclose()  # shutdown while the answer is streaming
        chunks += [chunk async for chunk in gen]
    text = b"".join(chunks).decode()
    assert '"reason_class": "service_shutdown"' in text
    assert text.rstrip().split("\n\n")[-1].startswith("event: final")
    assert '"partial": true' in text.rstrip().split("\n\n")[-1]


# 4: the harness counts streams without a final and protocol errors --------------------
def test_the_harness_counts_missing_finals_and_protocol_errors() -> None:
    scenario = StreamingScenario(name="t", concurrency=1, rounds=1, round_gap_s=0.0,
                                 warmup_rounds=0)
    results = [StreamResult(question_id="a", round=0, http_status=200, status="ok"),
               StreamResult(question_id="b", round=0, http_status=200, status=None),
               StreamResult(question_id="c", round=0, http_status=-1,
                            error_code="protocol:KeyError"),
               StreamResult(question_id="d", round=0, http_status=0,
                            error_code="ConnectError")]
    summary = summarise_streaming(scenario, results)
    assert (summary["final_missing"], summary["protocol_errors"],
            summary["transport_errors"]) == (1, 1, 1)


async def test_a_malformed_stream_is_a_protocol_error_not_a_transport_error() -> None:
    from relay.experiments.streaming_load import stream_one

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"event: final\ndata: {not json}\n\n",
                              headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://test") as client:
        result = await stream_one(client, "q", "a?", collection_id=None,
                                  auth="Bearer relay-dev-key")
    assert (result.http_status, result.error_code) == (-1, "protocol:JSONDecodeError")


# 8: a flight whose task ends early is removed by the task itself ---------------------
async def test_a_flight_is_removed_when_its_task_ends_even_before_starting(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)], cache=False)
    service = harness.app.state.relay
    target = await service.target(AskRequest(request_id="a", question="What is basalt?",
                                             collection_id=cid))
    gen = _respond(harness.context, target, request_id="a", arrived_ns=service.clock(),
                   collection_id=cid, buffered=False)
    await gen.__anext__()  # accepted: the flight and its task exist
    flight: Flight = next(iter(harness.context.flights.values()))
    task = flight.channel.task
    assert task is not None
    task.cancel()  # cancelled before it ran its first step
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not harness.context.flights
    await gen.aclose()
