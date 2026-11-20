"""V2-14 part 1: a cache hit on POST /v1/ask replays the same passages as the live answer
(before, a hit sent only paper id and pages, so the UI could not show the passage).
Scripted provider, :memory: store; no network."""

from __future__ import annotations

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


async def test_a_cache_hit_sends_the_same_passages_as_the_live_answer(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2)])
    live = await ask(harness.app, question(cid))
    hit = await ask(harness.app, question(cid))
    assert hit.of("final")[0]["cache_status"] == "exact_hit"  # the second is a cache hit
    live_passages = live.of("sources")[0]["passages"]
    assert live_passages and live_passages[0]["text"]  # the live answer has passage text
    assert hit.of("sources")[0]["passages"] == live_passages
