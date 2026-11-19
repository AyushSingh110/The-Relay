"""V2-05 tests: generators forward content as it arrives, and never retry after forwarding.

A retry after text has reached the client would send a second, different answer. Scripted
providers; no network.
"""

from __future__ import annotations

import asyncio

from relay.control.provider_guard import GuardConfig, GuardedGenerator, ProviderGuard
from relay.control.retry import RetryPolicy
from relay.orchestrator.pipeline import SingleCallGenerator
from relay.providers.base import ChatMessage, ChatRequest, ScriptedProvider, ScriptedReply
from relay.providers.errors import ProviderConnectionError
from relay.telemetry.spans import ServerSpanTracker

REQUEST = ChatRequest(model="m", messages=(ChatMessage(role="user", content="q"),),
                      max_completion_tokens=50)


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def guarded(replies: list[ScriptedReply]) -> tuple[GuardedGenerator, ScriptedProvider]:
    provider = ScriptedProvider(replies)
    return GuardedGenerator(provider, ProviderGuard(GuardConfig(max_concurrency=1)),
                            RetryPolicy(), sleep=no_sleep), provider


def broken() -> ProviderConnectionError:
    return ProviderConnectionError("connection reset", reason="test")


async def test_content_is_forwarded_in_order_as_it_arrives() -> None:
    seen: list[str] = []
    generator = SingleCallGenerator(ScriptedProvider([ScriptedReply(content_chunks=("A ", "b."))]))
    result = await generator.generate(REQUEST, ServerSpanTracker(), on_content=seen.append)
    assert seen == ["A ", "b."] and result.completion is not None
    assert result.completion.content == "A b."


async def test_a_failure_after_forwarded_content_is_not_retried() -> None:
    seen: list[str] = []
    generator, provider = guarded([
        ScriptedReply(content_chunks=("First ", "part"), error=broken(), fail_after_chunks=1),
        ScriptedReply(content_chunks=("Retry text.",))])
    result = await generator.generate(REQUEST, ServerSpanTracker(), on_content=seen.append)
    assert result.error is not None and result.attempts == 1 and result.retries == 0
    assert seen == ["First "] and len(provider.requests) == 1


async def test_a_failure_before_any_content_is_still_retried() -> None:
    seen: list[str] = []
    generator, provider = guarded([ScriptedReply(error=broken()),
                                   ScriptedReply(content_chunks=("Good ", "answer."))])
    result = await generator.generate(REQUEST, ServerSpanTracker(), on_content=seen.append)
    assert result.error is None and result.attempts == 2 and result.retries == 1
    assert seen == ["Good ", "answer."] and len(provider.requests) == 2
