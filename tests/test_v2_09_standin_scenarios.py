"""V2-09 stand-in scenarios: POST /v1/ask over a router of real stand-in lanes (HTTP through
``httpx.ASGITransport``; no network, no Groq). Each lane is its own stand-in app with its
own ProviderGuard, so its quota, circuit and daily budget are its own.

1. primary exhausted (RPM) -> the fallback lane serves, nothing is dropped;
2. a truncated answer -> exactly one escalation retry on the stronger lane;
3. cost ledger -> cost_usd from the reported usage, rewrite included, in request_log;
4. every lane exhausted -> the honest "unavailable" final with passage pointers and a wait;
plus the plan's: primary failing (circuit open) and primary daily budget spent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay.api.app import MeteredProvider, create_app
from relay.api.routes.ask_v1 import AskV1Context, ask_v1_router
from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.app.collections import StoreCollectionResolver
from relay.app.request_log import RequestLogSink
from relay.control.admission import AdmissionQueue
from relay.control.pricing import LanePricing
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.control.retry import RetryBudget, RetryBudgetConfig, RetryConfig, RetryPolicy
from relay.control.router import ProviderLane, ProviderRouter, RoutedGenerator
from relay.orchestrator.pipeline import PipelineConfig
from relay.orchestrator.rewriter import LLMQueryRewriter
from relay.providers.http_provider import HttpOpenAIProvider
from relay.retrieval.registry import IndexRegistry
from relay.standin.profile import (
    FaultModel,
    LatencyModel,
    Limits,
    LoadModel,
    ModelProfile,
    StandinProfile,
    TokenModel,
)
from relay.standin.server import API_PREFIX, create_standin_app
from relay.store import open_store
from tests.test_ask_v1 import StubRetriever, ask
from tests.test_ingest_worker import Env, FakeEmbedder

TEXT = ("Moshi is a speech dialogue model built for full duplex conversation. Moshi has a "
        "theoretical latency of 160 ms and a practical latency of 200 ms on one GPU. Moshi "
        "models the user and the system as two parallel audio streams.")
# Test-only prices (not any provider's real price list): they make the arithmetic visible.
PRICE_A = LanePricing(input_per_m=0.50, output_per_m=1.50)
PRICE_B = LanePricing(input_per_m=2.00, output_per_m=6.00)


def standin_profile(model: str, *, reasoning: int = 0, error_rate: float = 0.0,
                    header_ms: float = 2.0) -> StandinProfile:
    return StandinProfile(
        label="uncalibrated_defaults", seed=3,
        models={model: ModelProfile(
            latency=LatencyModel(header_median_ms=header_ms, header_sigma=0.0,
                                 prompt_ms_per_1k_tokens=0.0, inter_token_ms=0.05,
                                 reasoning_tokens=reasoning),
            tokens=TokenModel(base_prompt_tokens=10, prompt_chars_per_token=4.0,
                              output_chars_per_token=4.0))},
        limits=Limits(), load=LoadModel(knee_concurrency=64),
        faults=FaultModel(error_rate=error_rate))


class Lane:
    """A stand-in app and the ProviderLane in front of it."""

    def __init__(self, name: str, *, rpm: int | None = None, rpd: int | None = None,
                 max_tokens: int = 400, escalate_to: str | None = None,
                 pricing: LanePricing | None = PRICE_A, reasoning: int = 0,
                 error_rate: float = 0.0, circuit_threshold: int = 5,
                 header_ms: float = 2.0) -> None:
        self.model = f"model-{name}"
        self.app = create_standin_app(standin_profile(self.model, reasoning=reasoning,
                                                      error_rate=error_rate,
                                                      header_ms=header_ms))
        provider = HttpOpenAIProvider(base_url=f"http://standin-{name}{API_PREFIX}",
                                      source="stand-in",
                                      transport=httpx.ASGITransport(app=self.app))
        self.provider = provider
        guard = ProviderGuard(GuardConfig(max_concurrency=2, rpm_limit=rpm, rpd_limit=rpd,
                                          daily_safety_fraction=1.0,
                                          circuit_failure_threshold=circuit_threshold))
        self.lane = ProviderLane(name=name, provider=MeteredProvider(provider),
                                 model=self.model, guard=guard, pricing=pricing,
                                 max_completion_tokens=max_tokens, escalate_to=escalate_to)

    @property
    def calls(self) -> int:
        return int(self.app.state.standin.stats.requests)


class Routed:
    """/v1/ask over a router of stand-in lanes, with the request log on."""

    def __init__(self, env: Env, lanes: list[Lane], *, max_wait_ms: float = 2000.0,
                 retry: RetryPolicy | None = None) -> None:
        self.env, self.lanes = env, lanes
        self.router = ProviderRouter([lane.lane for lane in lanes], max_wait_ms=max_wait_ms,
                                     retry=retry or RetryPolicy(RetryConfig(max_attempts=1)))
        self.sink = RequestLogSink(env.store.request_log)
        self.started = False
        resolver = StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                                    embedder=FakeEmbedder()))
        self.app = create_app(
            router=self.router, retrievers={"small_fast": StubRetriever(())},
            top_k={"small_fast": 4},
            pipeline_config=PipelineConfig(model_id=lanes[0].model, max_completion_tokens=400),
            queue=AdmissionQueue(capacity=8, service_rate_per_s=10.0, max_active=2),
            corpus_sha256="c" * 64, collections=resolver, run=self.sink)
        self.app.include_router(ask_v1_router(AskV1Context(service=self.app.state.relay,
                                                           store=env.store)))

    async def ask(self, cid: str, question: str, **extra: Any) -> Any:
        if not self.started:
            await self.sink.start()
            self.started = True
        return await ask(self.app, {"question": question, "collection_id": cid, **extra})

    async def rows(self) -> list[Any]:
        await self.sink.flush()
        return list(reversed(await self.env.store.request_log.list_recent()))

    async def aclose(self) -> None:
        await self.sink.aclose()
        for lane in self.lanes:
            await lane.provider.aclose()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


async def collection(env: Env, *, buffered: bool = False) -> str:
    cid = (await env.store.collections.create(DEV_TENANT_ID, "speech", buffered=buffered)).id
    await env.upload(cid, [TEXT], title="Moshi")
    await env.worker().run_once()
    return cid


@pytest.fixture
async def routed(env: Env) -> AsyncGenerator[list[Routed]]:
    made: list[Routed] = []
    yield made
    for harness in made:
        await harness.aclose()


def build(made: list[Routed], env: Env, lanes: list[Lane], **kwargs: Any) -> Routed:
    harness = Routed(env, lanes, **kwargs)
    made.append(harness)
    return harness


# 1 ---------------------------------------------------------------------------------------
async def test_a_full_primary_lane_sends_the_next_request_to_the_fallback(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", rpm=1), Lane("b", rpm=100, pricing=PRICE_B)
    h = build(routed, env, [a, b])
    first, second = await asyncio.gather(
        h.ask(cid, "What is the latency of Moshi?"),
        h.ask(cid, "How does Moshi model the conversation?"))
    finals = [s.of("final")[0] for s in (first, second)]
    assert [f["status"] for f in finals] == ["ok", "ok"]  # nothing dropped
    assert all(f["verdict"] == "VERIFIED" for f in finals)
    assert sorted(f["lane_name"] for f in finals) == ["a", "b"]
    served_by_b = next(f for f in finals if f["lane_name"] == "b")
    assert served_by_b["fallback"] is True and served_by_b["model"] == "model-b"
    assert (a.calls, b.calls) == (1, 1)
    rows = await h.rows()
    assert sorted(r.lane_name for r in rows) == ["a", "b"]
    health = h.app.state.relay.health()  # /healthz: every lane's own guard state
    assert [(x["name"], x["requests_last_minute"]) for x in health["lanes"]] == [
        ("a", 1), ("b", 1)]


# 2 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("buffered", [False, True], ids=["streaming", "buffered"])
async def test_a_truncated_answer_is_escalated_exactly_once(
        env: Env, routed: list[Routed], buffered: bool) -> None:
    cid = await collection(env, buffered=buffered)
    a = Lane("a", max_tokens=20, escalate_to="b")  # cuts every answer at 20 tokens
    b = Lane("b", pricing=PRICE_B)
    h = build(routed, env, [a, b])
    stream = await h.ask(cid, "What is the latency of Moshi?")
    final = stream.of("final")[0]
    assert (final["status"], final["verdict"]) == ("ok", "VERIFIED")
    assert final["escalated"] is True and final["lane_name"] == "b"
    assert [u["purpose"] for u in final["ledger"]] == ["answer", "escalation"]
    assert [u["lane"] for u in final["ledger"]] == ["a", "b"]
    assert (a.calls, b.calls) == (1, 1)  # one escalation retry, no more
    assert stream.of("stopped") == []  # nothing from lane a reached the client
    (row,) = await h.rows()
    assert (row.escalated, row.lane_name) == (True, "b")


async def test_an_escalation_that_also_truncates_is_not_escalated_again(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", max_tokens=20, escalate_to="b"), Lane("b", max_tokens=20, escalate_to="a")
    h = build(routed, env, [a, b])
    final = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    assert (a.calls, b.calls) == (1, 1)
    assert final["escalated"] is True and final["status"] == "ok"
    assert final["verdict"] == "BLOCKED"  # truncated twice: nothing could be verified


async def test_no_escalation_once_the_retry_budget_is_spent(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", max_tokens=20, escalate_to="b"), Lane("b")
    none = RetryPolicy(RetryConfig(max_attempts=1),
                       budget=RetryBudget(RetryBudgetConfig(ratio=0.0, min_retries=0)))
    h = build(routed, env, [a, b], retry=none)
    final = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    assert (a.calls, b.calls) == (1, 0)
    assert final["escalated"] is False and final["lane_name"] == "a"


async def test_a_reasoning_model_that_spends_its_budget_thinking_is_escalated(
        env: Env, routed: list[Routed]) -> None:
    # v1 P07: gpt-oss spent all 512 completion tokens reasoning and answered nothing
    cid = await collection(env)
    a = Lane("a", max_tokens=64, reasoning=500, escalate_to="b")
    b = Lane("b")
    h = build(routed, env, [a, b])
    stream = await h.ask(cid, "What is the latency of Moshi?")
    final = stream.of("final")[0]
    assert (final["verdict"], final["escalated"], final["lane_name"]) == ("VERIFIED", True, "b")
    escalated = [s for s in stream.of("status") if s.get("escalated")]
    assert escalated and escalated[0]["reason"] == "reasoning_exhausted"


# 3 ---------------------------------------------------------------------------------------
def cost(use: dict[str, Any], price: LanePricing) -> float:
    return (use["tokens_in"] * price.input_per_m + use["tokens_out"] * price.output_per_m) / 1e6


async def test_cost_is_computed_from_reported_usage_and_stored(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", max_tokens=20, escalate_to="b"), Lane("b", pricing=PRICE_B)
    h = build(routed, env, [a, b])
    final = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    answer, escalation = final["ledger"]
    assert answer["usage_source"] == escalation["usage_source"] == "provider"
    expected = cost(answer, PRICE_A) + cost(escalation, PRICE_B)
    assert final["cost_usd"] == pytest.approx(expected, abs=1e-12)
    assert final["tokens_in"] == answer["tokens_in"] + escalation["tokens_in"]
    assert final["tokens_out"] == answer["tokens_out"] + escalation["tokens_out"]
    (row,) = await h.rows()
    assert row.cost_usd == pytest.approx(expected, abs=1e-12)
    assert (row.tokens_in, row.tokens_out) == (final["tokens_in"], final["tokens_out"])


async def test_a_rewrite_call_is_part_of_the_request_cost(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a = Lane("a")
    h = build(routed, env, [a])
    service = h.app.state.relay
    service.rewriter = LLMQueryRewriter(RoutedGenerator(h.router, purpose="rewrite",
                                                        escalate=False), model=a.model)
    conversation = (await env.store.conversations.create(cid)).id
    await h.ask(cid, "What is the latency of Moshi?", conversation_id=conversation)
    final = (await h.ask(cid, "And how does it model the conversation?",
                         conversation_id=conversation)).of("final")[0]
    rewrite, answer = final["ledger"]
    assert (rewrite["purpose"], answer["purpose"]) == ("rewrite", "answer")
    assert rewrite["tokens_in"] > 0  # the stand-in reported the rewrite call's usage
    assert final["cost_usd"] == pytest.approx(cost(rewrite, PRICE_A) + cost(answer, PRICE_A),
                                              abs=1e-12)
    assert final["tokens_in"] == rewrite["tokens_in"] + answer["tokens_in"]
    assert a.calls == 3  # answer, rewrite, answer
    rows = await h.rows()
    assert rows[-1].cost_usd == pytest.approx(final["cost_usd"], abs=1e-12)


async def test_an_unpriced_lane_reports_an_unknown_cost(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    h = build(routed, env, [Lane("a", pricing=None)])
    final = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    assert final["status"] == "ok" and final["cost_usd"] is None  # never guessed
    (row,) = await h.rows()
    assert row.cost_usd is None and row.lane_name == "a"


# 4 ---------------------------------------------------------------------------------------
async def test_with_every_lane_exhausted_the_answer_is_honestly_unavailable(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", rpm=1), Lane("b", rpm=1)
    h = build(routed, env, [a, b])
    await h.ask(cid, "What is the latency of Moshi?")
    await h.ask(cid, "How does Moshi model the conversation?")
    stream = await h.ask(cid, "What is Moshi built for?", deadline_ms=5000)
    assert stream.status == 200 and stream.names[0] == "accepted"
    final = stream.of("final")[0]
    assert final["status"] != "ok"
    assert final["error_code"] == "predicted_wait_exceeds_deadline"
    assert final["message"]  # the honest sentence, not an exception
    assert final["lane_name"] is None and final["cost_usd"] == 0.0
    assert 50_000 < final["retry_after_ms"] <= 60_000  # RPM frees in about a minute
    assert stream.of("sources")[0]["passages"]  # the passage pointers are still sent
    assert (a.calls, b.calls) == (1, 1)  # no provider call for the third question


# plan: primary failing, primary daily budget spent ----------------------------------------
async def test_a_failing_primary_opens_its_circuit_and_new_requests_go_elsewhere(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", error_rate=1.0, circuit_threshold=1), Lane("b")
    h = build(routed, env, [a, b])
    failed = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    assert failed["status"] != "ok" and failed["lane_name"] == "a"  # failure is honest
    assert a.lane.guard.circuit_state() == "open"
    final = (await h.ask(cid, "How does Moshi model the conversation?")).of("final")[0]
    assert (final["status"], final["lane_name"], final["fallback"]) == ("ok", "b", True)
    assert a.calls == 1  # the open circuit kept lane a out of it


async def test_with_the_primary_daily_budget_spent_the_second_lane_serves(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", rpd=1), Lane("b")
    h = build(routed, env, [a, b])
    first = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    second = (await h.ask(cid, "How does Moshi model the conversation?")).of("final")[0]
    assert (first["lane_name"], second["lane_name"]) == ("a", "b")
    assert second["status"] == "ok" and a.lane.guard.daily_stop_reason() is not None


# review fixes ------------------------------------------------------------------------------
async def test_a_failed_streaming_escalation_is_reported_as_a_provider_failure(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a, b = Lane("a", max_tokens=20, escalate_to="b"), Lane("b", error_rate=1.0)
    h = build(routed, env, [a, b])
    final = (await h.ask(cid, "What is the latency of Moshi?")).of("final")[0]
    assert (a.calls, b.calls) == (1, 1)
    assert final["status"] != "ok"
    assert final["error_code"] == "upstream_error"  # not "citation_verification_failed"
    assert final["escalated"] is True and len(final["ledger"]) == 2


async def test_a_coalesced_follower_records_no_lane_of_its_own(
        env: Env, routed: list[Routed]) -> None:
    cid = await collection(env)
    a = Lane("a", header_ms=150.0)  # slow enough for the second request to join the flight
    h = build(routed, env, [a])
    question = "What is the latency of Moshi?"
    finals = [s.of("final")[0] for s in await asyncio.gather(h.ask(cid, question),
                                                             h.ask(cid, question))]
    follower = next(f for f in finals if f["coalesced"])
    leader = next(f for f in finals if not f["coalesced"])
    assert leader["lane_name"] == "a" and a.calls == 1
    assert (follower["lane_name"], follower["escalated"], follower["cost_usd"]) == (
        None, False, 0.0)
