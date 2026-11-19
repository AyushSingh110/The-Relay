"""V2-05 tests: the pipeline's streaming mode (prepare -> token stream -> complete).

``Pipeline.run`` is unchanged (its own tests and the Table 3 regression cover it). Scripted
providers; no network.
"""

from __future__ import annotations

from typing import Any

from relay.orchestrator.pipeline import (
    Pipeline,
    PipelineConfig,
    PipelineRequest,
    SingleCallGenerator,
)
from relay.orchestrator.streaming import SentenceStream, StoppedEvent, StreamRefusal
from relay.orchestrator.tools import ToolGateway
from relay.providers.base import ScriptedProvider, ScriptedReply
from relay.telemetry.spans import ServerSpanTracker
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever, reply

GOOD = "The encoder has N = 6 identical layers [P1]. The model uses h = 8 heads [P2]."


def pipeline(replies: list[Any]) -> tuple[Pipeline, ScriptedProvider]:
    provider = ScriptedProvider(replies)
    return Pipeline(gateway=ToolGateway({"unit": StubRetriever(PASSAGES)}),
                    generator=SingleCallGenerator(provider),
                    config=PipelineConfig(model_id="m", max_completion_tokens=200),
                    top_k={"unit": 4}), provider


def request(question: str = "What is d_model in the encoder?") -> PipelineRequest:
    return PipelineRequest(request_id="s1", question=question, retrieval_setting="unit")


async def stream_through(p: Pipeline, question: str | None = None) -> tuple[Any, list[Any]]:
    spans = ServerSpanTracker()
    plan = await p.prepare_streaming(request(question or "What is d_model in the encoder?"),
                                     spans)
    assert plan.early is None
    tokens = p.run_streaming(plan, spans)
    stream = SentenceStream(tokens, plan.passages, plan.streaming_verifier)
    events = [event async for event in stream]
    stop = next((e for e in events if isinstance(e, StoppedEvent)), None)
    refused = any(isinstance(e, StreamRefusal) for e in events)
    outcome = await p.complete_streaming(plan, tokens, stream, stop=stop, refused=refused)
    return outcome, events


async def test_a_fully_verified_stream_is_an_answer() -> None:
    p, provider = pipeline([reply(GOOD, chunks=9)])
    outcome, events = await stream_through(p)
    assert [e.text for e in events] == ["The encoder has N = 6 identical layers [P1].",
                                        "The model uses h = 8 heads [P2]."]
    assert (outcome.kind, outcome.verification_verdict) == ("answer", "VERIFIED")
    assert outcome.delivered_text == GOOD and outcome.provider_called
    assert outcome.usage_source == "provider" and len(provider.requests) == 1
    assert outcome.trace.verification is not None  # the whole-answer pass ran


async def test_an_early_refusal_needs_no_model_call() -> None:
    p, provider = pipeline([])
    plan = await p.prepare_streaming(request("In 1706.03762v5, what is d_model?"),
                                     ServerSpanTracker())
    assert plan.early is not None and plan.early.kind == "early_refusal"
    assert provider.requests == []


async def test_a_failed_sentence_cancels_generation_and_gives_a_partial_outcome() -> None:
    text = f"{GOOD.split('. ')[0]}. The encoder has 999 layers [P1]. More text [P1]."
    p, _ = pipeline([ScriptedReply(content_chunks=tuple(text[i:i + 4]
                                                        for i in range(0, len(text), 4)))])
    outcome, events = await stream_through(p)
    assert isinstance(events[-1], StoppedEvent) and events[-1].partial
    assert (outcome.kind, outcome.verification_verdict) == ("partial", "BLOCKED")
    assert outcome.error_code == "citation_verification_failed"
    assert outcome.delivered_text == "The encoder has N = 6 identical layers [P1]."
    assert outcome.provider_called and outcome.usage_source == "estimated"
    assert "999" not in (outcome.delivered_text or "")


async def test_a_streamed_refusal_marker_is_an_llm_refusal() -> None:
    p, _ = pipeline([reply("INSUFFICIENT_EVIDENCE")])
    outcome, events = await stream_through(p)
    assert [type(e) for e in events] == [StreamRefusal]
    assert (outcome.kind, outcome.verification_verdict) == ("llm_refusal", "REFUSAL_VERIFIED")
    assert outcome.refused


async def test_a_stream_abandoned_by_its_consumer_is_never_a_verified_answer() -> None:
    p, _ = pipeline([reply(GOOD, chunks=9)])
    spans = ServerSpanTracker()
    plan = await p.prepare_streaming(request(), spans)
    tokens = p.run_streaming(plan, spans)
    stream = SentenceStream(tokens, plan.passages, plan.streaming_verifier)
    async for _event in stream:
        break  # the client went away after the first sentence
    outcome = await p.complete_streaming(plan, tokens, stream, stop=None, refused=False)
    assert outcome.verification_verdict != "VERIFIED" and outcome.kind != "answer"
    assert outcome.error_code == "client_gave_up"


async def test_a_provider_failure_mid_answer_is_a_partial_with_the_failure_status() -> None:
    from relay.providers.errors import ProviderConnectionError

    first = "The encoder has N = 6 identical layers [P1]. "
    p, _ = pipeline([ScriptedReply(content_chunks=(first, "The model uses"),
                                   error=ProviderConnectionError("reset", reason="test"),
                                   fail_after_chunks=2)])
    outcome, events = await stream_through(p)
    sent = [e.text for e in events if not isinstance(e, StoppedEvent)]
    assert sent == [first.strip()]  # streamed and verified; the cut-off tail never is
    assert (outcome.kind, outcome.status, outcome.error_code) == (
        "partial", "error", "upstream_error")
    assert outcome.delivered_text is None  # v1 rule: status ok exactly when delivered
