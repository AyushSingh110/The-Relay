"""V2-14 fix A: a streamed answer cancelled before the provider's usage report arrives is
charged the prompt estimate plus the tokens of the text actually received, not the whole
completion allowance (which made every verification stop cost ~512 tokens it never used
and starved the TPM budget under load). Counting stays the guard's own chars-per-token rule.
Scripted provider and a fake clock; no network."""

from __future__ import annotations

import asyncio
import math

import pytest

from relay.control.provider_guard import GuardedGenerator
from relay.control.retry import RetryPolicy
from relay.providers.base import ScriptedProvider, ScriptedReply, TokenUsage
from relay.providers.headers import RateLimitSnapshot
from relay.telemetry.spans import ServerSpanTracker
from tests.test_control_plane import FakeTime, chat, make_guard

PROMPT_ESTIMATE = 100  # chat(300, ...) at the initial 3.0 chars per token


async def test_a_cancelled_stream_is_charged_the_prompt_and_the_received_tokens() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    permit = await guard.acquire(chat(300, 512))
    assert permit.estimate == PROMPT_ESTIMATE + 512  # the reservation while the call runs
    settled = guard.settle(permit, usage=None, stream_opened=True, received_chars=150)
    assert settled.charged == PROMPT_ESTIMATE + 50  # 150 chars = 50 tokens, not + 512
    assert (settled.tokens_in, settled.tokens_out, settled.usage_source) == (
        PROMPT_ESTIMATE, 50, "estimated")
    assert guard.snapshot().tokens_last_minute == PROMPT_ESTIMATE + 50


async def test_an_unknown_amount_received_keeps_the_whole_reservation() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    permit = await guard.acquire(chat(300, 512))
    settled = guard.settle(permit, usage=None, stream_opened=True, received_chars=None)
    assert settled.charged == PROMPT_ESTIMATE + 512  # nothing known: stay conservative


async def test_a_streaming_call_cancelled_mid_answer_charges_what_was_forwarded() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    provider = ScriptedProvider([ScriptedReply(content_chunks=("a" * 60, "b" * 90, "c" * 300))])
    generator = GuardedGenerator(provider, guard, RetryPolicy(), sleep=ft.sleep, clock=ft.clock)
    seen: list[str] = []

    def stop_after_first(text: str) -> None:  # the verifier stopping the answer
        seen.append(text)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await generator.generate(chat(300, 512), ServerSpanTracker(ft.clock),
                                 on_content=stop_after_first)
    received = sum(len(t) for t in seen)
    assert 0 < received < 450  # cancelled part-way
    assert guard.snapshot().tokens_last_minute == PROMPT_ESTIMATE + math.ceil(received / 3.0)


async def test_the_providers_own_headroom_is_not_given_back_for_a_cancelled_stream() -> None:
    """Review finding: a provider that is cut off mid-stream may keep its whole reservation
    (the stand-in does: it settles only when a stream completes), and Groq's behaviour is
    unobserved. So the local per-minute window is charged what was received, but the
    header-based headroom keeps the full reservation until the provider's next headers."""
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    guard.sync_headers(RateLimitSnapshot(limit_tokens=8000, remaining_tokens=2000))
    permit = await guard.acquire(chat(300, 512))
    guard.settle(permit, usage=None, stream_opened=True, received_chars=150)
    snapshot = guard.snapshot()
    assert snapshot.tokens_last_minute == PROMPT_ESTIMATE + 50  # local window: received
    assert snapshot.tpm_available == 2000 - (PROMPT_ESTIMATE + 512)  # provider: unchanged


async def test_reported_usage_still_gives_the_headroom_back() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    guard.sync_headers(RateLimitSnapshot(limit_tokens=8000, remaining_tokens=2000))
    permit = await guard.acquire(chat(300, 512))
    guard.settle(permit, usage=TokenUsage(prompt_tokens=90, completion_tokens=30),
                 stream_opened=True)
    assert guard.snapshot().tpm_available == 2000 - 120  # the provider settled to usage


async def test_reasoning_received_before_a_cancellation_is_charged_too() -> None:
    """Review finding: reasoning text is generated (and billed) like content, so a stream
    cancelled during or after reasoning is charged for it as well."""
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    provider = ScriptedProvider([ScriptedReply(reasoning_chunks=("r" * 90,),
                                               content_chunks=("a" * 60, "b" * 300))])
    generator = GuardedGenerator(provider, guard, RetryPolicy(), sleep=ft.sleep, clock=ft.clock)

    def stop_at_first_content(text: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await generator.generate(chat(300, 512), ServerSpanTracker(ft.clock),
                                 on_content=stop_at_first_content)
    # 90 reasoning chars + 60 content chars at 3.0 chars per token
    assert guard.snapshot().tokens_last_minute == PROMPT_ESTIMATE + math.ceil(150 / 3.0)
