"""V2-11: GET /metrics, request tracing and GET /v1/requests/{id}, privacy of every label and
span attribute, and sanitised transport-error categories. Scripted provider, :memory: store,
httpx MockTransport; no network, no Groq."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import ssl
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay.api.routes.requests import requests_router
from relay.app.bootstrap import ensure_dev_tenant
from relay.app.request_log import RequestLogSink
from relay.control.admission import AdmissionQueue
from relay.errors import sanitize_error
from relay.orchestrator.pipeline import SingleCallGenerator
from relay.providers.base import ChatMessage, ChatRequest
from relay.providers.http_provider import HttpOpenAIProvider, transport_category
from relay.store import open_store
from relay.telemetry.metrics import RelayMetrics, layer_of
from relay.telemetry.spans import ServerSpanTracker
from relay.telemetry.tracing import WATERFALL
from tests.test_ask_v1 import answer
from tests.test_ingest_worker import Env
from tests.test_v2_10_tenants import ABOUT, TEXT, World

QUESTION = "What kind of speech model is Moshi really?"  # in scope; must never leak


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        environment = Env(store, tmp_path)
        yield environment
        for sink in getattr(environment, "sinks", []):  # before the store closes
            await sink.aclose()


class Observed(World):
    """World with the request log on and GET /v1/requests/{id} mounted."""

    def __init__(self, env: Env, replies: list[Any], **kw: Any) -> None:
        super().__init__(env, replies, **kw)
        self.sink = RequestLogSink(env.store.request_log)
        self.service._run = self.sink
        env.sinks = [*getattr(env, "sinks", []), self.sink]  # type: ignore[attr-defined]
        self.h.app.include_router(requests_router(env.store, self.auth))

    async def start(self) -> None:
        await self.sink.start()

    async def flushed(self) -> None:
        await self.sink.flush()

    async def metrics(self) -> str:
        response = await self.client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        return response.text


def sample(text: str, name: str, **labels: str) -> float:
    """The value of one Prometheus sample (0.0 when it is absent)."""
    for line in text.splitlines():
        if not line.startswith(name + "{") and line.split(" ")[0] != name:
            continue
        found = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if all(found.get(k) == v for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


# 1. /metrics ----------------------------------------------------------------------------
async def test_metrics_are_prometheus_text_and_count_a_request(env: Env) -> None:
    w = Observed(env, [answer(ABOUT)])
    await w.start()
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    before = await w.metrics()
    assert "# TYPE relay_requests_total counter" in before
    assert sample(before, "relay_requests_total", tenant=tenant) == 0.0
    final = (await w.ask(key, cid, QUESTION)).of("final")[0]
    assert final["status"] == "ok"
    await w.ask(key, cid, QUESTION)  # the same question again: a cache hit
    after = await w.metrics()
    assert sample(after, "relay_requests_total", status="ok", tenant=tenant,
                  lane="primary") == 1.0
    assert sample(after, "relay_requests_total", status="ok", tenant=tenant, lane="none") == 1.0
    assert sample(after, "relay_request_latency_seconds_count", stage="total",
                  lane="primary") == 1.0
    assert sample(after, "relay_request_latency_seconds_count", stage="first_sentence",
                  lane="primary") == 1.0
    assert sample(after, "relay_cache_events_total", event="miss") == 1.0
    assert sample(after, "relay_cache_events_total", event="hit") == 1.0
    assert sample(after, "relay_tokens_total", direction="in", lane="primary") > 0
    for layer in ("L1", "L2", "L3", "L4"):  # every layer exported, none blocked yet
        assert sample(after, "relay_verifier_blocks_total", layer=layer) == 0.0


async def test_a_blocked_sentence_counts_against_its_verifier_layer(env: Env) -> None:
    w = Observed(env, [answer("Moshi is a speech dialogue model with no citation at all.")])
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    await w.ask(key, cid, "What is Moshi?")
    assert sample(await w.metrics(), "relay_verifier_blocks_total", layer="L1") == 1.0


async def test_queue_depth_and_quota_headroom_are_read_live() -> None:
    metrics = RelayMetrics()
    depth = {("batch", "acme"): 3, ("interactive", "dev"): 1}
    metrics.watch(queue_depth=lambda: depth, headroom=lambda: {"fast": 6200, "slow": None})
    text = metrics.render().decode()
    assert sample(text, "relay_queue_depth", priority="batch", tenant="acme") == 3.0
    assert sample(text, "relay_quota_headroom_tokens", lane="fast") == 6200.0
    assert 'lane="slow"' not in text  # no TPM limit known: no made-up headroom
    depth[("batch", "acme")] = 0
    assert sample(metrics.render().decode(), "relay_queue_depth", priority="batch",
                  tenant="acme") == 0.0


async def test_the_queue_reports_its_waiters_by_priority_and_tenant() -> None:
    queue = AdmissionQueue(capacity=10, service_rate_per_s=1.0)
    gate = asyncio.Event()

    async def hold() -> None:
        async with queue.admit():
            await gate.wait()

    async def wait(tenant: str, priority: str) -> None:
        async with queue.admit(tenant=tenant, priority=priority):
            pass

    tasks = [asyncio.create_task(hold())]
    await asyncio.sleep(0)
    tasks += [asyncio.create_task(wait("a", "batch")) for _ in range(2)]
    tasks.append(asyncio.create_task(wait("b", "interactive")))
    await asyncio.sleep(0)
    assert queue.depth_by_class() == {("batch", "a"): 2, ("interactive", "b"): 1}
    gate.set()
    await asyncio.gather(*tasks)
    assert queue.depth_by_class() == {}


def test_verifier_layers_follow_the_v2_07_checks() -> None:
    assert layer_of("uncited") == "L1"
    assert layer_of("ungrounded") == layer_of("metadata_mismatch") == "L2"
    assert layer_of("refusal_as_claim") == "L3"
    assert layer_of(None, ["ungrounded_number"]) == "L2"
    assert layer_of("truncated") is None  # not a verifier block


# 2. privacy -----------------------------------------------------------------------------
async def test_no_label_attribute_or_breakdown_holds_text_or_keys(env: Env) -> None:
    w = Observed(env, [answer(ABOUT), answer(ABOUT)], delay_s=0.005)
    await w.start()
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    conversation = (await env.store.conversations.create(cid)).id
    first = (await w.ask(key, cid, QUESTION, conversation_id=conversation)).of("final")[0]
    follow = (await w.ask(key, cid, "And how fast is it?",
                          conversation_id=conversation)).of("final")[0]
    await w.flushed()
    view = (await w.get(key, f"/v1/requests/{first['request_id']}")).json()
    leaks = [QUESTION, "kind of speech model", "And how fast is it", "speech dialogue model",
             TEXT[:40], key, key[len("rel_live_"):]]
    spans = w.service.tracing.recent.spans()
    assert spans  # the local exporter kept them
    attributes = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    rows = await env.store.request_log.list_recent()
    stored = json.dumps([r.breakdown for r in rows])
    for blob in (await w.metrics(), attributes, stored, json.dumps(view)):
        for secret in leaks:
            assert secret not in blob
    from relay.control.cache import question_hash

    assert view["question_hash"] == question_hash(QUESTION)  # only the hash
    assert any("question_hash" in (s.attributes or {}) for s in spans)
    assert follow["status"] == "ok"


# 3. GET /v1/requests/{id} -----------------------------------------------------------------
async def test_the_request_view_has_a_waterfall_that_matches_its_spans(env: Env) -> None:
    w = Observed(env, [answer(ABOUT, LATENCY := "Moshi has a latency of 200 ms [P1].")],
                 delay_s=0.01)
    await w.start()
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    final = (await w.ask(key, cid, "What is Moshi and its latency?")).of("final")[0]
    await w.flushed()
    response = await w.get(key, f"/v1/requests/{final['request_id']}")
    assert response.status_code == 200
    view = response.json()
    assert (view["status"], view["lane"], view["verdict"]) == ("ok", "primary", "VERIFIED")
    assert view["tokens"] == {"in": final["tokens_in"], "out": final["tokens_out"]}
    assert view["verifier"] == {"sentences_checked": 2, "sentences_passed": 2,
                                "partial": False}
    names = [span["name"] for span in view["trace"]]
    assert names[0] == "request"
    for name in ("queue.wait", "pipeline.retrieve", "pipeline.generate", "pipeline.verify"):
        assert name in names
    assert names.count("verifier.sentence") == 2
    waterfall = view["waterfall"]
    for stage, span_name in WATERFALL:  # each stage is exactly its span's duration
        assert waterfall[stage] == pytest.approx(
            sum(s["duration_ms"] for s in view["trace"] if s["name"] == span_name), abs=1e-3)
    assert waterfall["generation_ms"] > 0  # the scripted model took 10 ms a piece
    assert waterfall["total_ms"] >= sum(waterfall[s] for s, _ in WATERFALL) - 1e-3
    generate = next(s for s in view["trace"] if s["name"] == "pipeline.generate")
    assert generate["attributes"]["lane"] == "primary"
    sentence = next(s for s in view["trace"] if s["name"] == "verifier.sentence")
    assert sentence["attributes"]["l1_pass"] is True
    del LATENCY


async def test_another_tenants_request_is_404(env: Env) -> None:
    w = Observed(env, [answer(ABOUT)])
    await w.start()
    a, key_a = await w.tenant("a")
    _, key_b = await w.tenant("b")
    cid = await w.collection(a)
    request_id = (await w.ask(key_a, cid, "What is Moshi?")).of("final")[0]["request_id"]
    await w.flushed()
    assert (await w.get(key_a, f"/v1/requests/{request_id}")).status_code == 200
    assert (await w.get(key_b, f"/v1/requests/{request_id}")).status_code == 404
    assert (await w.get(key_a, "/v1/requests/v1ask-nope")).status_code == 404
    assert (await w.client.get(f"/v1/requests/{request_id}")).status_code == 401


async def test_a_follow_up_trace_has_its_rewrite(env: Env) -> None:
    w = Observed(env, [answer(ABOUT), answer(ABOUT)])
    await w.start()
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    conversation = (await env.store.conversations.create(cid)).id
    await w.ask(key, cid, "What is Moshi?", conversation_id=conversation)
    final = (await w.ask(key, cid, "And how fast is it?",
                         conversation_id=conversation)).of("final")[0]
    await w.flushed()
    view = (await w.get(key, f"/v1/requests/{final['request_id']}")).json()
    rewrite = next(s for s in view["trace"] if s["name"] == "pipeline.rewrite")
    assert rewrite["attributes"]["method"] == "rules"
    assert view["waterfall"]["rewrite_ms"] == pytest.approx(rewrite["duration_ms"], abs=1e-3)


# review fixes ----------------------------------------------------------------------------
async def test_an_abandoned_answer_still_counts_its_tokens(env: Env) -> None:
    w = Observed(env, [answer(ABOUT, ABOUT, ABOUT, pieces=40)], delay_s=0.02)
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    gone = asyncio.Event()

    def leave(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "sentence":
            gone.set()

    from tests.test_ask_v1 import ask as raw_ask

    stream = await asyncio.wait_for(raw_ask(w.h.app, {"question": "What is Moshi?",
                                                      "collection_id": cid},
                                            auth=f"Bearer {key}", gone=gone,
                                            on_event=leave), 5)
    assert "final" not in stream.names
    for _ in range(100):
        if not w.h.context.flights:
            break
        await asyncio.sleep(0.01)
    text = await w.metrics()
    assert sample(text, "relay_tokens_total", direction="in", lane="primary") > 0


async def test_a_leader_leaving_while_a_follower_stays_keeps_a_clean_trace(
        env: Env, caplog: Any) -> None:
    w = Observed(env, [answer(ABOUT, pieces=20)], delay_s=0.02)
    tenant, key = await w.tenant("acme")
    cid = await w.collection(tenant)
    gone = asyncio.Event()

    def leave(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "sentence":
            gone.set()

    from tests.test_ask_v1 import ask as raw_ask

    body = {"question": "What is Moshi?", "collection_id": cid}
    leader = asyncio.create_task(raw_ask(w.h.app, body, auth=f"Bearer {key}", gone=gone,
                                         on_event=leave))
    for _ in range(100):  # the flight exists: the follower joins it
        if w.h.context.flights:
            break
        await asyncio.sleep(0.005)
    follower = asyncio.create_task(raw_ask(w.h.app, body, auth=f"Bearer {key}"))
    await leader  # it left at its first sentence; the follower keeps the flight going
    final = (await follower).of("final")[0]
    assert final["status"] == "ok" and final["coalesced"] is True
    noisy = [r.getMessage() for r in caplog.records if "ended span" in r.getMessage()
             or "on an ended" in r.getMessage()]
    assert noisy == []  # no span is ended or changed after its trace was closed


def test_span_work_after_a_trace_is_finished_is_a_silent_no_op(caplog: Any) -> None:
    """Review fix: a flight that outlives its leader's trace (the leader left, a follower
    stayed) must not log OTel warnings or keep spans that are never ended."""
    from relay.telemetry.tracing import Tracing

    tracing = Tracing()
    trace = tracing.request(request_id="r")
    generate = trace.start("pipeline.generate")
    trace.finish(status="ok")
    caplog.clear()
    trace.end(generate, lane="primary")  # ended by finish(): left alone
    late = trace.start("pipeline.verify")
    trace.end(late, verdict="VERIFIED")
    trace.set(status="late")
    trace.instant("verifier.sentence", sentence_idx=1)
    assert [r.getMessage() for r in caplog.records] == []
    assert [s["name"] for s in trace.breakdown()["spans"]] == ["request", "pipeline.generate"]


# 4. transport categories ----------------------------------------------------------------
def dns_error() -> httpx.ConnectError:
    error = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    error.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    return error


def tls_error() -> httpx.ConnectError:
    error = httpx.ConnectError("certificate verify failed")
    error.__cause__ = ssl.SSLError(1, "[SSL: CERTIFICATE_VERIFY_FAILED]")
    return error


@pytest.mark.parametrize(("make", "category"), [
    (dns_error, "DNS_FAILURE"),
    (lambda: httpx.ConnectTimeout("timed out"), "CONNECT_TIMEOUT"),
    (tls_error, "TLS_ERROR"),
    (lambda: httpx.ReadTimeout("timed out"), "READ_TIMEOUT"),
    (lambda: httpx.RemoteProtocolError("peer closed connection"), "STREAM_DISCONNECT"),
    (lambda: httpx.ConnectError("[Errno 111] Connection refused"), "CONNECT_FAILURE"),
], ids=["dns", "connect_timeout", "tls", "read_timeout", "disconnect", "refused"])
async def test_transport_failures_get_a_sanitised_category(make: Any, category: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise make()

    provider = HttpOpenAIProvider(base_url="http://secret-host.internal:9/openai/v1",
                                  source="stand-in", transport=httpx.MockTransport(handler))
    request = ChatRequest(model="m", max_completion_tokens=8,
                          messages=(ChatMessage(role="user", content="hello"),))
    try:
        result = await SingleCallGenerator(provider).generate(request, ServerSpanTracker())
    finally:
        await provider.aclose()
    assert result.error is not None and result.error.category == category
    assert transport_category(make()) == category
    logged = sanitize_error(result.error)
    assert f"category={category}" in logged
    assert "secret-host" not in logged and "getaddrinfo" not in logged


def test_a_stream_that_ends_early_is_a_disconnect() -> None:
    from relay.providers.errors import ProviderStreamIncompleteError

    error = ProviderStreamIncompleteError("stream ended before [DONE]")
    assert sanitize_error(error) == "ProviderStreamIncompleteError(category=STREAM_DISCONNECT)"
