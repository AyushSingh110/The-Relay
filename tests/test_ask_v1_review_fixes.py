"""V2-05 review fixes for POST /v1/ask: one regression test per accepted finding of the
code review of ``relay/api/routes/ask_v1.py`` (see the phase report). No network."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from relay.api.routes.ask_v1 import ReplayChannel
from relay.app.bootstrap import ensure_dev_tenant
from relay.control.admission import AdmissionQueue
from relay.control.cache import CachedAnswer
from relay.providers.base import ScriptedReply
from relay.providers.errors import ProviderConnectionError
from relay.store import open_store
from tests.test_ask_v1 import S1, S2, Harness, answer, ask, geology, question
from tests.test_ingest_worker import Env


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


# 1: a leader that leaves before its work starts must not leave a stuck flight ---------
async def test_a_leader_gone_right_after_accepted_leaves_no_stuck_flight(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1), answer(S1)])
    gone = asyncio.Event()

    def leave(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "accepted":
            gone.set()

    await asyncio.wait_for(ask(harness.app, question(cid), gone=gone, on_event=leave), 5)
    for _ in range(100):
        if not harness.context.flights:
            break
        await asyncio.sleep(0.01)
    assert not harness.context.flights
    later = await asyncio.wait_for(ask(harness.app, question(cid)), 5)
    assert later.of("final")[0]["status"] == "ok"


# 2: once its last listener leaves, a flight cannot be joined ----------------------------
async def test_a_channel_is_closed_as_soon_as_its_last_listener_leaves() -> None:
    channel = ReplayChannel()

    async def work() -> None:
        await asyncio.sleep(10)

    channel.task = asyncio.create_task(work())
    channel.join()
    channel.leave()
    assert channel.closed  # immediately: a request arriving now starts a new flight
    with pytest.raises(asyncio.CancelledError):
        await channel.task


# 3: a provider failure never releases the cut-off tail ---------------------------------
async def test_the_unfinished_tail_after_a_provider_failure_is_not_sent(env: Env) -> None:
    cid = await geology(env)
    tail = "Basalt is a volcanic rock formed [P1]"  # would verify if it were released
    broken = ScriptedReply(content_chunks=(f"{S1} ", tail, " more"),
                           error=ProviderConnectionError("reset", reason="test"),
                           fail_after_chunks=2)
    harness = Harness(env, [broken])
    stream = await ask(harness.app, question(cid))
    assert [s["text"] for s in stream.of("sentence")] == [S1]
    assert tail not in stream.raw
    stopped = stream.of("stopped")[0]
    assert (stopped["reason_class"], stopped["partial"]) == ("provider_error", True)
    assert stream.names[-1] == "final"


# 4: only verified answers are replayed from the cache -----------------------------------
async def test_a_cached_refusal_is_never_replayed_as_sentences(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)])
    service = harness.app.state.relay
    target = await service.target(__import__("relay.api.app", fromlist=["AskRequest"])
                                  .AskRequest(request_id="r", question="What is basalt?",
                                              collection_id=cid))
    refusal = "I can't answer this from the one document in the library."
    service.cache.put("What is basalt?", target.cache_setting, CachedAnswer(
        delivered_text=refusal, refused=True, citations=(), verification_verdict="REFUSAL_VERIFIED",
        model_id="m", source_request_id="old"), degraded=False)
    stream = await ask(harness.app, question(cid))
    assert refusal not in [s["text"] for s in stream.of("sentence")]
    assert stream.of("sentence") == []
    final = stream.of("final")[0]  # served from the cache, as a message (review fix 7)
    assert (final["cache_status"], final["refused"], final["message"]) == (
        "exact_hit", True, refusal)


# 5: a failing on_queued callback leaves no ghost waiter --------------------------------
async def test_a_failing_queue_callback_leaves_no_ghost_waiter() -> None:
    queue = AdmissionQueue(capacity=4, service_rate_per_s=1.0, max_active=1)

    def explode(_: Any) -> None:
        raise RuntimeError("callback failed")

    async with queue.admit():
        with pytest.raises(RuntimeError):
            async with queue.admit(on_queued=explode):
                pass
        assert queue.depth == 0
    assert queue.active == 0
    async with queue.admit():  # the slot is still usable
        assert queue.active == 1


# 8: an internal failure after sentences still says the answer was partial --------------
async def test_an_internal_failure_after_sentences_is_reported_as_partial(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2)])

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(harness.app.state.relay.pipeline, "complete_streaming", boom)
    stream = await ask(harness.app, question(cid))
    assert [s["text"] for s in stream.of("sentence")] == [S1, S2]
    assert stream.of("stopped")[0] == {"reason_class": "internal_error", "partial": True}
    final = stream.of("final")[0]
    assert (final["error_code"], final["partial"]) == ("internal_error", True)


# 9: a follower of a queued flight is told it is queued ----------------------------------
async def test_a_follower_of_a_queued_flight_gets_its_queue_position(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)], keepalive_s=0.1)
    release = asyncio.Event()

    async def occupy() -> None:
        async with harness.queue.admit():
            await release.wait()

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    leader = asyncio.create_task(ask(harness.app, question(cid)))
    for _ in range(100):
        if harness.context.flights and harness.queue.depth == 1:
            break
        await asyncio.sleep(0.01)
    follower = asyncio.create_task(ask(harness.app, question(cid)))
    await asyncio.sleep(0.1)
    release.set()
    streams = await asyncio.gather(leader, follower)
    await holder
    accepted = streams[1].of("accepted")[0]
    assert accepted["queue_position"] == 1 and accepted["expected_wait_ms"] > 0
    assert streams[1].of("final")[0]["coalesced"] is True
