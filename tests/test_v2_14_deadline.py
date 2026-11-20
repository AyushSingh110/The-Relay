"""V2-14 fix B: POST /v1/ask accepts ``deadline_ms`` and passes it on exactly as v1's
POST /ask does: to the admission queue (a request predicted to wait past its deadline is
shed up front instead of queued) and to the generation (it stops when the deadline passes).
Scripted provider, :memory: store; no network."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from relay.app.bootstrap import ensure_dev_tenant
from relay.store import open_store
from tests.test_ask_v1 import S1, S2, Harness, answer, ask, geology, question
from tests.test_ingest_worker import Env


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


async def test_a_deadline_too_short_for_the_queue_is_rejected_up_front(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, S1, S2, pieces=40), answer(S1)], delay_s=0.02,
                      max_active=1, cache=False)
    busy = asyncio.Event()

    def on_event(event: tuple[float, str, dict[str, object]]) -> None:
        if event[1] == "sentence":
            busy.set()  # the only slot is taken

    first = asyncio.create_task(ask(harness.app, question(cid), on_event=on_event))
    await asyncio.wait_for(busy.wait(), 5)
    late = await asyncio.wait_for(ask(harness.app, {**question(cid, "Is basalt dark?"),
                                                    "deadline_ms": 1}), 5)
    assert late.names == ["accepted", "final"]  # not queued, nothing generated
    final = late.of("final")[0]
    assert (final["status"], final["error_code"]) == ("rate_limited",
                                                      "predicted_wait_exceeds_deadline")
    assert final["provider_called"] is False and final["message"]
    await asyncio.wait_for(first, 10)
    assert len(harness.provider.requests) == 1  # only the first request reached the model


async def test_without_a_deadline_the_same_request_waits_its_turn(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, S1, S2, pieces=40), answer(S1)], delay_s=0.02,
                      max_active=1, cache=False)
    busy = asyncio.Event()
    first = asyncio.create_task(ask(harness.app, question(cid), on_event=lambda e: (
        busy.set() if e[1] == "sentence" else None)))
    await asyncio.wait_for(busy.wait(), 5)
    waited = await asyncio.wait_for(ask(harness.app, question(cid, "Is basalt dark?")), 10)
    assert waited.of("status")[0]["stage"] == "queued"
    assert waited.of("final")[0]["status"] == "ok"
    await first


async def test_the_deadline_also_reaches_the_provider_guard(env: Env) -> None:
    """As in v1: with a free slot the request is admitted, but a deadline that has passed
    by the time the model would be called ends it with deadline_expired, uncalled. (v1's
    deadline bounds admission, guard waits and retries; it does not cut a running answer.)"""
    cid = await geology(env)
    harness = Harness(env, [answer(S1)], cache=False)
    stream = await asyncio.wait_for(ask(harness.app, {**question(cid), "deadline_ms": 1}), 10)
    final = stream.of("final")[0]
    assert (final["status"], final["error_code"]) == ("timeout", "deadline_expired")
    assert final["provider_called"] is False and not harness.provider.requests


async def test_deadline_ms_must_be_a_positive_integer(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)])
    for bad in (0, -5, "soon", 1.5):
        reply = await ask(harness.app, {**question(cid), "deadline_ms": bad})
        assert reply.status == 422, bad


async def test_a_followers_own_deadline_bounds_its_wait_as_in_v1(env: Env) -> None:
    """Review finding: v1 bounds a coalesced follower's wait by its own deadline
    (``deadline_expired``); the leader's answer carries on for the leader."""
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, S1, S2, pieces=40)], delay_s=0.03)
    started = asyncio.Event()
    leader = asyncio.create_task(ask(harness.app, question(cid), on_event=lambda e: (
        started.set() if e[1] == "sentence" else None)))
    await asyncio.wait_for(started.wait(), 5)
    follower = await asyncio.wait_for(ask(harness.app, {**question(cid), "deadline_ms": 200}), 5)
    final = follower.of("final")[0]
    assert final["coalesced"] is True
    assert (final["status"], final["error_code"]) == ("timeout", "deadline_expired")
    assert final["total_ms"] < 1000  # gave up at its deadline, not at the leader's end
    led = (await asyncio.wait_for(leader, 10)).of("final")[0]
    assert (led["status"], led["verdict"]) == ("ok", "VERIFIED")  # the leader is unaffected
