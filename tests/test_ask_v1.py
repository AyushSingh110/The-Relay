"""V2-05 tests: ``POST /v1/ask`` verified streaming (tests 6-11 and 14 of the phase prompt,
plus v1's disconnect and coalescing behaviour on the new endpoint).

The app is ``create_app`` plus the /v1/ask router over a :memory: store; the model is a
scripted provider (optionally slowed down per token); requests are driven over raw ASGI
so every event is timestamped as the server sends it. No network, no API key.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from relay.api.app import create_app
from relay.api.routes.ask_v1 import DEFAULT_KEEPALIVE_S, AskV1Context, ask_v1_router
from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.app.collections import StoreCollectionResolver
from relay.control.admission import AdmissionQueue
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.orchestrator.pipeline import PipelineConfig
from relay.providers.base import (
    ChatRequest,
    ContentDelta,
    ProviderEvent,
    ScriptedProvider,
    ScriptedReply,
)
from relay.retrieval.registry import IndexRegistry
from relay.store import open_store
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever

S1 = "Basalt is a volcanic rock formed from lava [P1]."
S2 = "Basalt is a volcanic rock [P1]."
S3 = "Lava forms basalt when it cools [P1]."
BAD = "Basalt has 999 layers [P1]."


class SlowProvider:
    """A scripted provider that waits ``delay_s`` before each content piece."""

    def __init__(self, replies: list[ScriptedReply], delay_s: float = 0.0) -> None:
        self.inner = ScriptedProvider(replies)
        self.delay_s = delay_s
        self.closed_early = 0

    @property
    def source(self) -> Any:
        return self.inner.source

    @property
    def requests(self) -> list[ChatRequest]:
        return self.inner.requests

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        finished = False
        try:
            async for event in self.inner.stream_chat(request):
                if isinstance(event, ContentDelta) and self.delay_s:
                    await asyncio.sleep(self.delay_s)
                yield event
            finished = True
        finally:
            if not finished:
                self.closed_early += 1

    async def complete_chat(self, request: ChatRequest) -> Any:
        return await self.inner.complete_chat(request)

    async def list_models(self) -> Any:
        return await self.inner.list_models()

    async def aclose(self) -> None:
        return None


def answer(*sentences: str, pieces: int = 6) -> ScriptedReply:
    text = " ".join(sentences)
    size = max(1, len(text) // pieces)
    return ScriptedReply(content_chunks=tuple(text[i:i + size]
                                              for i in range(0, len(text), size)))


class Harness:
    def __init__(self, env: Env, replies: list[ScriptedReply], *, delay_s: float = 0.0,
                 max_active: int = 1, keepalive_s: float = DEFAULT_KEEPALIVE_S,
                 cache: bool = True) -> None:
        self.env = env
        self.provider = SlowProvider(replies, delay_s)
        self.queue = AdmissionQueue(capacity=8, service_rate_per_s=10.0, max_active=max_active)
        resolver = StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                                    embedder=FakeEmbedder()))
        self.app = create_app(
            provider=self.provider, retrievers={"small_fast": StubRetriever(PASSAGES)},
            top_k={"small_fast": 4},
            pipeline_config=PipelineConfig(model_id="test-model", max_completion_tokens=300),
            guard=ProviderGuard(GuardConfig(max_concurrency=2)), queue=self.queue,
            corpus_sha256="c" * 64, cache_mode="exact" if cache else "off",
            collections=resolver)
        self.context = AskV1Context(service=self.app.state.relay, store=env.store,
                                    keepalive_s=keepalive_s)
        self.app.include_router(ask_v1_router(self.context))


class Stream:
    """A finished (or abandoned) /v1/ask response."""

    def __init__(self, status: int, raw: str, events: list[tuple[float, str, dict[str, Any]]]):
        self.status = status
        self.raw = raw
        self.events = events

    @property
    def names(self) -> list[str]:
        return [name for _, name, _ in self.events]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [data for _, n, data in self.events if n == name]

    def at(self, name: str) -> float:
        return next(t for t, n, _ in self.events if n == name)


async def ask(app: Any, body: dict[str, Any], *,
              auth: str | None = "Bearer relay-dev-key",
              gone: asyncio.Event | None = None,
              on_event: Any = None) -> Stream:
    payload = json.dumps(body).encode("utf-8")
    left = gone or asyncio.Event()
    sent = False
    started = time.perf_counter()
    status = 0
    raw: list[str] = []
    events: list[tuple[float, str, dict[str, Any]]] = []
    buffer = ""

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, buffer
        if message["type"] == "http.response.start":
            status = message["status"]
            return
        chunk = message.get("body", b"").decode("utf-8")
        raw.append(chunk)
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            name, data = block.split("\n", 1)
            event = (time.perf_counter() - started, name.removeprefix("event: "),
                     json.loads(data.removeprefix("data: ")))
            events.append(event)
            if on_event is not None:
                on_event(event)

    headers = [(b"content-type", b"application/json")]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "POST", "path": "/v1/ask",
             "raw_path": b"/v1/ask", "query_string": b"", "root_path": "", "scheme": "http",
             "headers": headers, "client": ("127.0.0.1", 50000),
             "server": ("127.0.0.1", 8000)}
    await app(scope, receive, send)
    return Stream(status, "".join(raw), events)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


async def geology(env: Env, *, buffered: bool = False) -> str:
    cid = (await env.store.collections.create(DEV_TENANT_ID, "geology", buffered=buffered)).id
    await env.upload(cid, ["Basalt is a volcanic rock formed from lava"], title="Rocks")
    await env.worker().run_once()
    return cid


def question(cid: str, text: str = "What is basalt?") -> dict[str, Any]:
    return {"question": text, "collection_id": cid}


# 6 --------------------------------------------------------------------------------------
async def test_accepted_comes_first_and_before_any_pipeline_work(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2)])
    stream = await ask(harness.app, question(cid))
    assert stream.status == 200
    assert stream.names == ["accepted", "status", "sources", "status", "sentence", "sentence",
                            "status", "final"]
    assert [s["stage"] for s in stream.of("status")] == ["retrieving", "generating",
                                                         "verifying"]
    accepted = stream.of("accepted")[0]
    assert accepted["request_id"] and accepted["queue_position"] == 0
    assert accepted["expected_wait_ms"] == 0.0
    assert stream.at("accepted") < 0.2  # within 200 ms of arrival
    assert stream.at("accepted") <= stream.at("sources")
    sources = stream.of("sources")[0]["passages"]
    assert sources[0]["id"] == "P1" and {"paper_id", "page_range", "section",
                                         "text_excerpt"} <= set(sources[0])
    assert [s["text"] for s in stream.of("sentence")] == [S1, S2]
    assert stream.of("sentence")[0]["citations"] == ["P1"]
    final = stream.of("final")[0]
    assert (final["status"], final["verdict"], final["partial"]) == ("ok", "VERIFIED", False)
    assert final["cache_status"] == "miss" and final["coalesced"] is False
    assert final["ttft_ms"] is not None and final["ttft_ms"] <= final["total_ms"]
    assert final["tokens_in"] > 0 and final["tokens_out"] > 0


async def test_retrieval_starts_only_after_accepted_was_sent(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)])
    marks: dict[str, float] = {}
    original = harness.app.state.relay.pipeline.prepare_streaming

    async def recording(*args: Any, **kwargs: Any) -> Any:
        marks.setdefault("work", time.perf_counter())
        return await original(*args, **kwargs)

    harness.app.state.relay.pipeline.prepare_streaming = recording
    started = time.perf_counter()
    stream = await ask(harness.app, question(cid))
    assert stream.at("accepted") <= marks["work"] - started


# 7 --------------------------------------------------------------------------------------
async def test_a_queued_request_gets_keepalives_and_never_goes_silent(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)], keepalive_s=0.2)
    release = asyncio.Event()

    async def occupy() -> None:
        async with harness.queue.admit():
            await release.wait()

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    asking = asyncio.create_task(ask(harness.app, question(cid)))
    await asyncio.sleep(1.0)
    release.set()
    stream = await asking
    await holder
    accepted = stream.of("accepted")[0]
    assert accepted["queue_position"] == 1 and accepted["expected_wait_ms"] > 0
    queued = [d for d in stream.of("status") if d["stage"] == "queued"]
    assert len(queued) >= 4 and all(d["queue_position"] == 1 for d in queued)
    times = [t for t, _, _ in stream.events]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert max(gaps) < 0.2 + 0.15  # never silent longer than the keepalive (+ scheduling)
    assert stream.of("final")[0]["status"] == "ok"


def test_the_default_keepalive_is_under_five_seconds() -> None:
    assert 0 < DEFAULT_KEEPALIVE_S < 5.0


# 8 --------------------------------------------------------------------------------------
async def test_a_failed_sentence_stops_the_stream(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, BAD, S2, S3, pieces=12)], delay_s=0.005)
    stream = await ask(harness.app, question(cid))
    names = stream.names
    assert names[-2:] == ["stopped", "final"]
    assert "sentence" not in names[names.index("stopped"):]
    assert [s["text"] for s in stream.of("sentence")] == [S1]
    stopped = stream.of("stopped")[0]
    assert (stopped["reason_class"], stopped["partial"]) == ("ungrounded", True)
    final = stream.of("final")[0]
    assert (final["partial"], final["verdict"]) == (True, "BLOCKED")
    # the failed sentence never left the server (its own words: a bare "999" can also
    # occur by chance inside a timing float such as total_ms, which made this flaky)
    assert "has 999 layers" not in stream.raw
    assert harness.provider.closed_early == 1  # generation was cancelled


# 9 --------------------------------------------------------------------------------------
async def test_only_fully_verified_answers_are_cached(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2), answer(S1, BAD), answer(S1, BAD)])
    first = await ask(harness.app, question(cid))
    again = await ask(harness.app, question(cid))
    assert first.of("final")[0]["cache_status"] == "miss"
    hit = again.of("final")[0]
    assert (hit["cache_status"], hit["verdict"], hit["tokens_in"]) == ("exact_hit", "VERIFIED", 0)
    assert [s["text"] for s in again.of("sentence")] == [S1, S2]
    assert len(harness.provider.requests) == 1
    partial = await ask(harness.app, question(cid, "Is basalt layered?"))
    partial_again = await ask(harness.app, question(cid, "Is basalt layered?"))
    assert partial.of("final")[0]["partial"] is True
    assert partial_again.of("final")[0]["cache_status"] == "miss"  # a partial is never cached
    assert len(harness.provider.requests) == 3


# 10 -------------------------------------------------------------------------------------
async def test_a_coalesced_follower_gets_the_replay_and_the_live_sentences(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, S3, pieces=30)], delay_s=0.02)
    first_sentence = asyncio.Event()

    def watch(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "sentence":
            first_sentence.set()

    leader = asyncio.create_task(ask(harness.app, question(cid), on_event=watch))
    await asyncio.wait_for(first_sentence.wait(), 5)
    follower = await ask(harness.app, question(cid))
    leading = await leader
    assert [s["text"] for s in follower.of("sentence")] == [S1, S2, S3]
    assert follower.of("sentence") == leading.of("sentence")
    assert len(harness.provider.requests) == 1
    f_final, l_final = follower.of("final")[0], leading.of("final")[0]
    assert (f_final["coalesced"], f_final["cache_status"], f_final["tokens_in"]) == (
        True, "coalesced", 0)
    assert l_final["coalesced"] is False and l_final["tokens_in"] > 0
    assert f_final["request_id"] != l_final["request_id"]
    assert follower.of("accepted")[0]["request_id"] == f_final["request_id"]


# 11 -------------------------------------------------------------------------------------
async def test_property_no_event_ever_carries_a_failed_sentence(env: Env) -> None:
    cid = await geology(env)
    rng = random.Random(51119)
    bad_pool = ("Basalt has 999 layers [P1].", "Basalt has no citation at all.",
                "Basalt uses the XQ-77 codec [P1].", "Basalt is a rock [P9].")
    for case in range(25):
        good = [rng.choice((S1, S2, S3)) for _ in range(rng.randint(0, 3))]
        bad = rng.choice(bad_pool)
        good.insert(rng.randint(0, len(good)), bad)
        harness = Harness(env, [answer(*good, pieces=rng.randint(1, 25))], cache=False)
        stream = await ask(harness.app, question(cid, f"What is basalt, case {case}?"))
        assert bad not in stream.raw, (case, good)
        body = bad.removesuffix(" [P1].").removesuffix(" [P9].").removesuffix(".")
        assert body not in stream.raw, (case, good)
        assert stream.names[-1] == "final" and "stopped" in stream.names


# 14 -------------------------------------------------------------------------------------
async def test_a_buffered_collection_answers_in_one_piece_over_sse(env: Env) -> None:
    cid = await geology(env, buffered=True)
    harness = Harness(env, [answer(S1, S2)], delay_s=0.01)
    stream = await ask(harness.app, question(cid))
    assert stream.names == ["accepted", "status", "sources", "status", "status", "sentence",
                            "sentence", "final"]
    assert [s["stage"] for s in stream.of("status")] == ["retrieving", "generating",
                                                         "verifying"]
    assert [s["text"] for s in stream.of("sentence")] == [S1, S2]
    sentences = [t for t, n, _ in stream.events if n == "sentence"]
    verifying = [t for t, n, d in stream.events if n == "status" and d["stage"] == "verifying"]
    assert min(sentences) >= verifying[0]  # nothing is released before the whole answer
    assert stream.of("final")[0]["verdict"] == "VERIFIED"
    assert stream.of("final")[0]["buffered"] is True


# disconnects and errors ----------------------------------------------------------------
async def test_a_client_disconnect_cancels_generation_and_frees_the_slot(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, S3, pieces=40)], delay_s=0.02)
    gone = asyncio.Event()

    def leave(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "sentence":
            gone.set()

    stream = await asyncio.wait_for(ask(harness.app, question(cid), gone=gone, on_event=leave),
                                    5)
    assert "final" not in stream.names  # nobody was listening any more
    for _ in range(100):
        if harness.queue.active == 0 and not harness.context.flights:
            break
        await asyncio.sleep(0.01)
    assert harness.queue.active == 0 and not harness.context.flights
    assert harness.provider.closed_early == 1


async def test_auth_collection_and_body_errors(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [])
    assert (await ask(harness.app, question(cid), auth=None)).status == 401
    foreign = await env.collection("theirs")  # another tenant
    assert (await ask(harness.app, question(foreign))).status == 404
    assert (await ask(harness.app, question("no-such"))).status == 404
    assert (await ask(harness.app, {"question": ""})).status == 422
    assert (await ask(harness.app, {"question": "q?", "extra": 1})).status == 422
    assert (await ask(harness.app, {"question": "q?"})).status == 404  # no v1-papers here
    assert harness.provider.requests == []
