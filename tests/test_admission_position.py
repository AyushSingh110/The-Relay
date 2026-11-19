"""V2-05 tests: a waiting request can read its live queue position (for status events)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from relay.control.admission import AdmissionQueue


async def test_waiters_see_their_live_position() -> None:
    queue = AdmissionQueue(capacity=4, service_rate_per_s=1.0, max_active=1)
    getters: dict[str, Callable[[], int]] = {}
    release = asyncio.Event()

    async def hold() -> None:
        async with queue.admit():
            await release.wait()

    async def wait(name: str) -> None:
        async with queue.admit(on_queued=lambda get: getters.__setitem__(name, get)):
            pass

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    first, second = asyncio.create_task(wait("a")), asyncio.create_task(wait("b"))
    async with asyncio.timeout(2):  # fail, never hang
        while len(getters) < 2:
            if first.done() or second.done():
                await asyncio.gather(first, second)  # surface their error
            await asyncio.sleep(0)
    assert (getters["a"](), getters["b"]()) == (1, 2)
    release.set()
    await asyncio.gather(holder, first, second)
    assert (getters["a"](), getters["b"]()) == (0, 0)  # admitted: no longer queued


async def test_an_immediately_admitted_request_is_never_reported_as_queued() -> None:
    queue = AdmissionQueue(capacity=4, service_rate_per_s=1.0, max_active=1)
    called: list[object] = []
    async with queue.admit(on_queued=called.append):
        pass
    assert called == []
