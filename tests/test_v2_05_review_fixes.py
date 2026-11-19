"""V2-05 whole-phase review fixes: one regression test per accepted finding (see the phase
report). Scripted providers, :memory: stores; no network."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from relay.api.app import AskRequest
from relay.api.routes.ask_v1 import _request_record, _respond
from relay.app.bootstrap import ensure_dev_tenant
from relay.orchestrator.streaming import SentenceStream, StoppedEvent, StreamRefusal
from relay.orchestrator.verifier import split_sentences
from relay.providers.base import ScriptedReply
from relay.store import open_store
from tests.test_ask_v1 import S1, S2, Harness, answer, ask, geology, question
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import PASSAGES
from tests.test_streaming import chunks, collect, split_streaming


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


async def tokens(pieces: list[str]) -> AsyncIterator[str]:
    for piece in pieces:
        yield piece


# 1: a leader gone while sending "accepted" must not strand its followers -----------------
async def test_followers_finish_when_the_leader_leaves_during_accepted(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1)])
    service = harness.app.state.relay
    target = await service.target(AskRequest(request_id="a", question="What is basalt?",
                                             collection_id=cid))
    leader = _respond(harness.context, target, request_id="a", arrived_ns=service.clock(),
                      collection_id=cid, buffered=False)
    assert b"accepted" in await leader.__anext__()
    follower = _respond(harness.context, target, request_id="b", arrived_ns=service.clock(),
                        collection_id=cid, buffered=False)
    assert b"accepted" in await follower.__anext__()
    await leader.aclose()  # the leader's client went away right after "accepted"
    chunks_ = [chunk async for chunk in asyncio_timeout_iter(follower, 5)]
    assert b"event: final" in chunks_[-1] and b'"coalesced": true' in chunks_[-1]


async def asyncio_timeout_iter(gen: Any, seconds: float) -> AsyncIterator[bytes]:
    async with asyncio.timeout(seconds):
        async for chunk in gen:
            yield chunk


# 2: list items that start with a tag split exactly as v1 ----------------------------------
def test_numbered_items_with_leading_tags_split_as_v1() -> None:
    texts = ["Key components:\n1. [P1] The encoder maps tokens [P1].\n2. [P2] More [P2].",
             "Parts:\n(1) [P1] first part [P1]\n(2) second [P2]",
             "Claim one [P1].\n10. [P2] Tenth item [P2]."]
    rng = random.Random(7)
    for text in texts:
        assert split_streaming(list(text)) == split_sentences(text), text
        for _ in range(40):
            assert split_streaming(chunks(text, rng)) == split_sentences(text), text


# 3: a lead-in is released only together with the verified item after it -----------------
async def test_a_lead_in_is_never_released_before_its_item_verifies() -> None:
    bad = "The encoder has these parts:\n- Parts are X [P9]."
    events = await collect(SentenceStream(tokens([bad]), PASSAGES))
    assert [type(e) for e in events] == [StoppedEvent]  # the lead-in was not released
    good = "The encoder has these parts:\n- The encoder has N = 6 identical layers [P1]."
    texts = [e.text for e in await collect(SentenceStream(tokens(list(good)), PASSAGES))]
    assert texts == ["The encoder has these parts:",
                     "The encoder has N = 6 identical layers [P1]."]


# 4: closing a token stream never swallows the caller's own cancellation ------------------
async def test_a_cancelled_caller_stays_cancelled_while_closing_tokens() -> None:
    from relay.orchestrator.pipeline import (
        Pipeline,
        PipelineConfig,
        PipelineRequest,
        SingleCallGenerator,
    )
    from relay.orchestrator.tools import ToolGateway
    from relay.providers.base import ScriptedProvider
    from relay.telemetry.spans import ServerSpanTracker
    from tests.test_pipeline_and_verifier import StubRetriever

    class Slow:
        def __init__(self) -> None:
            self.inner = SingleCallGenerator(ScriptedProvider([ScriptedReply(
                content_chunks=("x",) * 3)]))

        async def generate(self, *args: Any, **kwargs: Any) -> Any:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # slow to wind down
                raise

    p = Pipeline(gateway=ToolGateway({"unit": StubRetriever(PASSAGES)}), generator=Slow(),
                 config=PipelineConfig(model_id="m"), top_k={"unit": 4})
    spans = ServerSpanTracker()
    plan = await p.prepare_streaming(PipelineRequest(
        request_id="c", question="What is d_model?", retrieval_setting="unit"), spans)
    stream = p.run_streaming(plan, spans)
    it = stream.__aiter__()
    first = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.01)

    async def closer() -> str:
        await stream.aclose()
        return "kept running"

    task = asyncio.create_task(closer())
    await asyncio.sleep(0.01)
    task.cancel()  # cancelled while aclose waits for generation to stop
    with pytest.raises(asyncio.CancelledError):
        await task
    first.cancel()


# 5: the flight record carries what the flight spent ---------------------------------------
async def test_the_flight_record_carries_the_spend(env: Env) -> None:
    cid = await geology(env)
    records: list[dict[str, Any]] = []
    harness = Harness(env, [answer(S1, S2)])
    harness.app.state.relay.record_trace = records.append
    await ask(harness.app, question(cid))
    flight = next(r for r in records if r["record"] == "flight")
    assert flight["tokens_in"] > 0 and flight["tokens_out"] > 0
    assert flight["provider_called"] is True


# 6: the request record keeps provider and first-sentence times apart ---------------------
def test_request_records_keep_provider_and_first_sentence_times_apart() -> None:
    final = {"status": "ok", "cache_status": "miss", "verdict": "VERIFIED", "ttft_ms": 900.0,
             "provider_first_token_ms": 300.0, "total_ms": 1000.0, "tokens_in": 1,
             "tokens_out": 1}
    record = _request_record("r", hit_question="q?", final=final, collection_id="c",
                             role="leader")
    assert record["final"]["provider_first_token_ms"] == 300.0
    assert record["final"]["first_sentence_ms"] == 900.0


# 7: refusals are cached and replayed as a message, never as sentences ---------------------
async def test_a_refusal_is_cached_and_replayed_without_sentences(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer("INSUFFICIENT_EVIDENCE")])
    first = await ask(harness.app, question(cid))
    again = await ask(harness.app, question(cid))
    assert first.of("final")[0]["verdict"] == "REFUSAL_VERIFIED"
    hit = again.of("final")[0]
    assert (hit["cache_status"], hit["verdict"], hit["refused"]) == (
        "exact_hit", "REFUSAL_VERIFIED", True)
    assert hit["message"] and again.of("sentence") == []
    assert len(harness.provider.requests) == 1


# 8: a refusal marker split across a line break is still a refusal ------------------------
async def test_a_marker_split_across_lines_is_a_refusal() -> None:
    events = await collect(SentenceStream(tokens(["INSUFFICIENT", "\nEVIDENCE"]), PASSAGES))
    assert [type(e) for e in events] == [StreamRefusal]
