"""V2-05 test 13: the steady_streaming harness scenario runs and records first-status and
first-sentence times. The app (scripted, slowed provider) is served by uvicorn on
127.0.0.1, so events really stream; no network beyond loopback, no API key."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from relay.app.bootstrap import ensure_dev_tenant
from relay.experiments.streaming_load import (
    STREAMING_SCENARIOS,
    StreamingScenario,
    run_streaming_scenario,
    serve_app_on_loopback,
    summarise_streaming,
)
from relay.store import open_store
from tests.test_ask_v1 import BAD, S1, S2, Harness, answer, geology
from tests.test_ingest_worker import Env


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


def test_the_canonical_scenario_is_five_concurrent_streams() -> None:
    steady = STREAMING_SCENARIOS["steady_streaming"]
    assert steady.concurrency == 5 and steady.rounds >= 1


async def test_steady_streaming_records_first_status_and_first_sentence(env: Env) -> None:
    cid = await geology(env)
    replies = [answer(S1, S2, pieces=8)] * 4 + [answer(S1, BAD, S2, pieces=8)]
    harness = Harness(env, replies, delay_s=0.01, max_active=5)
    scenario = StreamingScenario(name="steady_streaming", concurrency=5, rounds=1,
                                 round_gap_s=0.0, warmup_rounds=0)
    questions = [(f"q{i}", f"What is basalt, take {i}?") for i in range(5)]
    async with serve_app_on_loopback(harness.app) as base_url, httpx.AsyncClient(
            base_url=base_url, timeout=30) as client:
        results = await run_streaming_scenario(client, scenario, questions,
                                               collection_id=cid, auth="Bearer dev")
    summary = summarise_streaming(scenario, results)
    assert summary["requests"] == 5 and summary["http_200"] == 5
    assert summary["first_sentence_ms"]["n"] == 5
    assert summary["first_sentence_ms"]["p50"] is not None
    assert summary["first_status_ms"]["p50"] < summary["first_sentence_ms"]["p50"]
    assert summary["partial"] == 1 and summary["partial_rate"] == 0.2
    for result in results:
        assert result.first_status_ms is not None and result.first_sentence_ms is not None
        assert result.first_status_ms <= result.first_sentence_ms <= result.total_ms


async def test_a_failed_stream_is_recorded_and_does_not_abort_the_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    scenario = StreamingScenario(name="t", concurrency=2, rounds=1, round_gap_s=0.0,
                                 warmup_rounds=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://test") as client:
        results = await run_streaming_scenario(client, scenario, [("q1", "a?"), ("q2", "b?")],
                                               collection_id=None, auth="Bearer dev")
    assert [(r.http_status, r.error_code) for r in results] == [(0, "ConnectError")] * 2
    assert summarise_streaming(scenario, results)["http_200"] == 0


async def test_warm_up_rounds_are_reported_apart_from_the_steady_numbers(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, pieces=8)] * 4, delay_s=0.005, max_active=2)
    scenario = StreamingScenario(name="steady_streaming", concurrency=2, rounds=1,
                                 round_gap_s=0.0, warmup_rounds=1)
    questions = [(f"q{i}", f"What is basalt, take {i}?") for i in range(2)]
    async with serve_app_on_loopback(harness.app) as base_url, httpx.AsyncClient(
            base_url=base_url, timeout=30) as client:
        results = await run_streaming_scenario(client, scenario, questions,
                                               collection_id=cid, auth="Bearer dev")
    assert [r.round for r in results] == [-1, -1, 0, 0]
    summary = summarise_streaming(scenario, results)
    assert summary["requests"] == 2 and summary["warmup"]["requests"] == 2
    assert all(r.provider_first_token_ms is not None for r in results)
    assert summary["provider_first_token_ms"]["n"] == 2
    assert all(r.verdict == "VERIFIED" for r in results)  # warm-ups really ran the model
