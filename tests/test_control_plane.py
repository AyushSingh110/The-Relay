"""P06 tests: retry policy and budget, ProviderGuard, GuardedGenerator, admission queue.

Time is faked (a clock plus a sleep that advances it), randomness is seeded, and the
model is a ``ScriptedProvider``. Every limit below is an explicit test input, not a
measured Groq value.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Collection, Sequence
from datetime import date

import pytest

from relay.control.admission import (
    AdmissionQueue,
    AdmissionRejectedError,
    CapacityInputs,
    derive_capacity,
)
from relay.control.provider_guard import (
    CircuitOpenError,
    DailyBudgetExhaustedError,
    DeadlineExpiredError,
    GuardConfig,
    GuardedGenerator,
    ProviderGuard,
    QuotaWaitExceedsDeadlineError,
    RequestExceedsTokenLimitError,
)
from relay.control.retry import RetryBudget, RetryBudgetConfig, RetryConfig, RetryPolicy
from relay.orchestrator.pipeline import Pipeline, PipelineConfig, PipelineRequest
from relay.orchestrator.tools import ToolGateway
from relay.providers.base import (
    ChatMessage,
    ChatRequest,
    ScriptedProvider,
    ScriptedReply,
    TokenUsage,
)
from relay.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from relay.providers.headers import RateLimitSnapshot, ResponseMeta
from relay.retrieval.hybrid_index import RetrievalResult, RetrievedPassage
from relay.telemetry.spans import ServerSpanTracker

NS = 1_000_000_000


class FakeTime:
    """A monotonic clock, a sleep that advances it, and a settable calendar day."""

    def __init__(self) -> None:
        self.now = 1_000 * NS
        self.sleeps: list[float] = []
        self.day = date(2026, 11, 13)

    def clock(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += int(seconds * NS)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * NS)

    def today(self) -> date:
        return self.day

    def in_s(self, seconds: float) -> int:
        return self.now + int(seconds * NS)


def chat(prompt_chars: int = 300, max_tokens: int = 100) -> ChatRequest:
    return ChatRequest(model="test-model",
                       messages=(ChatMessage(role="user", content="x" * prompt_chars),),
                       max_completion_tokens=max_tokens)


def make_guard(ft: FakeTime, **config: object) -> ProviderGuard:
    config.setdefault("max_concurrency", 8)  # tests often hold several permits at once
    return ProviderGuard(GuardConfig(**config), clock=ft.clock, sleep=ft.sleep,  # type: ignore[arg-type]
                         today=ft.today)


def usage(prompt: int, completion: int) -> TokenUsage:
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion,
                      total_tokens=prompt + completion)


def ok_reply(text: str = "INSUFFICIENT_EVIDENCE", prompt: int = 80,
             completion: int = 20) -> ScriptedReply:
    return ScriptedReply(content_chunks=(text,), usage=usage(prompt, completion))


SERVER_ERROR = ProviderServerError("boom")


# --- Retry policy and budget --------------------------------------------------------


def test_full_jitter_backoff_stays_within_the_exponential_cap() -> None:
    policy = RetryPolicy(RetryConfig(base_delay_s=0.5, max_delay_s=4.0), rng=random.Random(7))
    for retry_number, cap in ((1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (6, 4.0)):
        delays = [policy.backoff_s(retry_number) for _ in range(200)]
        assert min(delays) >= 0.0 and max(delays) <= cap
        assert max(delays) > 0.5 * cap  # it really is spread over the whole range
    assert policy.backoff_s(1, retry_after_s=3.0) >= 3.0  # retry-after is a floor


@pytest.mark.parametrize(
    ("error", "attempt", "remaining_s", "reason"),
    [
        (ProviderAuthError("no"), 1, None, "not_retryable"),
        (ProviderRateLimitError("day", scope="daily", basis="body"), 1, None, "daily_scope"),
        (SERVER_ERROR, 3, None, "max_attempts"),
        (ProviderRateLimitError("min", scope="minute", basis="body", retry_after_s=5.0), 1,
         6.0, "deadline"),
        (SERVER_ERROR, 1, 30.0, "retry"),
        (ProviderTimeoutError("stall", phase="stall"), 2, None, "retry"),
    ],
)
def test_retry_decisions(error: Exception, attempt: int, remaining_s: float | None,
                         reason: str) -> None:
    policy = RetryPolicy(RetryConfig(max_attempts=3, deadline_share=0.5), rng=random.Random(1))
    decision = policy.decide(error, attempt=attempt, remaining_s=remaining_s)  # type: ignore[arg-type]
    assert (decision.reason, decision.retry) == (reason, reason == "retry")


def test_retry_budget_throttles_and_can_be_switched_off() -> None:
    ft = FakeTime()
    budget = RetryBudget(RetryBudgetConfig(ratio=0.5, min_retries=1, window_s=60.0), ft.clock)
    policy = RetryPolicy(RetryConfig(max_attempts=10), budget, rng=random.Random(3))
    for _ in range(4):
        budget.record_first_attempt()  # allowance = 0.5 * 4 + 1 = 3 retries per window
    reasons = [policy.decide(SERVER_ERROR, attempt=1, remaining_s=None).reason for _ in range(5)]
    assert reasons == ["retry", "retry", "retry", "budget_exhausted", "budget_exhausted"]
    assert budget.retries_in_window == 3
    ft.advance(61)  # the window rolls over: first attempts and retries both expire
    assert budget.retries_in_window == 0 and budget.allowance() == 1
    unlimited = RetryPolicy(RetryConfig(max_attempts=10),
                            RetryBudget(RetryBudgetConfig(enabled=False, ratio=0.0,
                                                          min_retries=0), ft.clock),
                            rng=random.Random(3))
    decisions = [unlimited.decide(SERVER_ERROR, attempt=1, remaining_s=None) for _ in range(20)]
    assert all(d.retry and not d.budget_enforced for d in decisions)


# --- ProviderGuard: reservation and settlement --------------------------------------


async def test_settlement_replaces_the_estimate_with_provider_usage() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=1000)
    permit = await guard.acquire(chat(300, 100))
    assert permit.estimate == 200  # ceil(300 / 3.0) + 100
    assert guard.snapshot().tokens_last_minute == 200
    settled = guard.settle(permit, usage=usage(80, 20), stream_opened=True)
    assert (settled.tokens_in, settled.tokens_out, settled.usage_source) == (80, 20, "provider")
    assert (settled.reserved, settled.charged) == (200, 100)
    snapshot = guard.snapshot()
    assert (snapshot.tokens_last_minute, snapshot.day_tokens, snapshot.tpm_available) == (
        100, 100, 900)


async def test_missing_usage_keeps_the_reservation_and_is_marked_estimated() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=1000)
    permit = await guard.acquire(chat(300, 100))
    settled = guard.settle(permit, usage=None, stream_opened=True, received_chars=30)
    assert (settled.usage_source, settled.charged) == ("estimated", 200)
    assert (settled.tokens_in, settled.tokens_out) == (100, 10)
    assert guard.snapshot().tokens_last_minute == 200  # stays charged
    rejected = await guard.acquire(chat(300, 100))
    released = guard.settle(rejected, usage=None, stream_opened=False)
    assert (released.usage_source, released.charged) == ("none", 0)
    assert guard.snapshot().tokens_last_minute == 200  # the rejected call cost nothing


async def test_chars_per_token_adapts_conservatively_and_stays_bounded() -> None:
    ft = FakeTime()
    guard = make_guard(ft)
    assert guard.chars_per_token == 3.0
    permit = await guard.acquire(chat(300, 100))
    guard.settle(permit, usage=usage(150, 10), stream_opened=True)  # 2.0 chars per token seen
    assert guard.chars_per_token == pytest.approx(0.8 * 3.0 + 0.2 * (0.9 * 2.0))
    assert guard.estimate(chat(300, 100)) > 200  # later reservations are larger
    for _ in range(200):  # an extreme ratio cannot push the estimate below the floor
        permit = await guard.acquire(chat(30, 1))
        guard.settle(permit, usage=usage(3000, 1), stream_opened=True)
    assert guard.chars_per_token == 1.0


# --- ProviderGuard: headers ---------------------------------------------------------


async def test_header_sync_never_treats_request_headers_as_rpm() -> None:
    ft = FakeTime()
    guard = make_guard(ft)  # no configured limits at all
    guard.sync_headers(RateLimitSnapshot(limit_requests=1000, remaining_requests=990,
                                         limit_tokens=8000, remaining_tokens=7000,
                                         reset_tokens_s=5.0))
    snapshot = guard.snapshot()
    assert snapshot.rpm_limit is None  # *-requests headers are daily, never per minute
    assert (snapshot.rpd_limit, snapshot.rpd_remaining_header) == (1000, 990)
    assert (snapshot.tpm_limit, snapshot.tpm_available) == (8000, 7000)
    # 40 requests inside one minute are not throttled by the "1000 requests" header.
    for _ in range(40):
        permit = await guard.acquire(chat(3, 1))
        guard.settle(permit, usage=usage(1, 1), stream_opened=True)
    assert ft.sleeps == [] and guard.snapshot().requests_last_minute == 40

    configured = make_guard(ft, rpm_limit=30)
    configured.sync_headers(RateLimitSnapshot(limit_requests=1000, remaining_requests=999))
    assert configured.snapshot().rpm_limit == 30  # configuration is the only RPM source


async def test_header_token_headroom_overrides_the_local_view_until_it_resets() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=8000)
    guard.sync_headers(RateLimitSnapshot(limit_tokens=8000, remaining_tokens=250,
                                         reset_tokens_s=5.0))
    assert guard.snapshot().tpm_available == 250 and guard.fits(chat(300, 100))
    permit = await guard.acquire(chat(300, 100))  # reserves 200 of the 250
    assert guard.snapshot().tpm_available == 50 and not guard.fits(chat(300, 100))
    guard.settle(permit, usage=usage(80, 20), stream_opened=True)
    assert guard.snapshot().tpm_available == 150  # settled down to 100 used
    ft.advance(6)  # the header's window has reset: back to the local view
    assert guard.snapshot().tpm_available == 7900


# --- ProviderGuard: minute windows, deadline, size ----------------------------------


async def test_token_window_waits_then_proceeds_and_the_wait_is_a_waiting_span() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=500, max_concurrency=3)
    spans = ServerSpanTracker(clock=ft.clock)
    spans.switch("working")
    for _ in range(2):
        await guard.acquire(chat(300, 100), spans=spans)  # 2 x 200 reserved
    ft.advance(10)
    await guard.acquire(chat(300, 100), spans=spans)  # needs the first to leave the window
    assert ft.sleeps == [pytest.approx(50.0)]
    summary = spans.finish()
    assert summary.waiting_ms == pytest.approx(50_000.0)
    assert summary.working_ms == pytest.approx(10_000.0)


async def test_quota_wait_longer_than_the_deadline_fails_fast() -> None:
    ft = FakeTime()
    guard = make_guard(ft, rpm_limit=2)
    for _ in range(2):
        await guard.acquire(chat(3, 1))
    with pytest.raises(QuotaWaitExceedsDeadlineError) as info:
        await guard.acquire(chat(3, 1), deadline_ns=ft.in_s(10))
    assert info.value.error_code == "predicted_wait_exceeds_deadline" and ft.sleeps == []
    await guard.acquire(chat(3, 1), deadline_ns=ft.in_s(120))  # enough time: it waits
    assert ft.sleeps == [pytest.approx(60.0)]


async def test_expired_deadline_and_oversized_request_are_refused() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=150)
    with pytest.raises(DeadlineExpiredError):
        await guard.acquire(chat(3, 1), deadline_ns=ft.now)
    with pytest.raises(RequestExceedsTokenLimitError) as info:
        await guard.acquire(chat(300, 100))  # 200 > 150 can never fit
    assert info.value.error_code == "payload_too_large"
    permit = await guard.acquire(chat(3, 1))  # refusals released the slot
    assert permit.estimate == 2


async def test_minute_rate_limit_blocks_new_calls_for_retry_after() -> None:
    ft = FakeTime()
    guard = make_guard(ft)
    guard.record_result(ProviderRateLimitError("min", scope="minute", basis="body",
                                               retry_after_s=4.0))
    await guard.acquire(chat(3, 1))
    assert ft.sleeps == [pytest.approx(4.0)]
    assert guard.circuit_state() == "closed"  # a 429 is not a health failure


async def test_semaphore_limits_concurrent_provider_calls() -> None:
    ft = FakeTime()
    guard = make_guard(ft, max_concurrency=1)
    first = await guard.acquire(chat(3, 1))
    second = asyncio.create_task(guard.acquire(chat(3, 1)))
    await asyncio.sleep(0.01)
    assert not second.done()
    guard.settle(first, usage=usage(1, 1), stream_opened=True)
    permit = await asyncio.wait_for(second, 1.0)  # proceeds as soon as the slot is free
    assert guard.snapshot().requests_last_minute == 2 and permit.estimate >= 2


# --- ProviderGuard: daily stop and circuit breaker ----------------------------------


async def test_daily_safety_stop_from_local_counts_headers_and_rollover() -> None:
    ft = FakeTime()
    guard = make_guard(ft, rpd_limit=4, daily_safety_fraction=0.75)
    for _ in range(3):
        guard.settle(await guard.acquire(chat(3, 1)), usage=usage(1, 1), stream_opened=True)
    with pytest.raises(DailyBudgetExhaustedError) as info:
        await guard.acquire(chat(3, 1))  # 3 >= 0.75 x 4
    assert info.value.error_code == "daily_budget_exhausted"
    ft.day = date(2026, 11, 14)  # a new quota day starts the local counters again
    assert guard.daily_stop_reason() is None
    await guard.acquire(chat(3, 1))

    by_header = make_guard(ft)
    by_header.sync_headers(RateLimitSnapshot(limit_requests=1000, remaining_requests=250))
    assert "750/1000" in (by_header.daily_stop_reason() or "")
    by_tokens = make_guard(ft, tpd_limit=1000)
    by_tokens.settle(await by_tokens.acquire(chat(3, 1)), usage=usage(700, 60),
                     stream_opened=True)
    assert "daily tokens 760" in (by_tokens.daily_stop_reason() or "")


async def test_circuit_opens_after_consecutive_failures_and_recovers_with_one_trial() -> None:
    ft = FakeTime()
    guard = make_guard(ft, circuit_failure_threshold=2, circuit_cooldown_s=30.0,
                       max_concurrency=3)
    guard.record_result(SERVER_ERROR)
    guard.record_result(None)  # a success resets the count
    guard.record_result(SERVER_ERROR)
    assert guard.circuit_state() == "closed"
    guard.record_result(ProviderTimeoutError("stall", phase="stall"))
    assert guard.circuit_state() == "open"
    with pytest.raises(CircuitOpenError) as info:
        await guard.acquire(chat(3, 1))
    assert info.value.error_code == "circuit_open"
    ft.advance(31)
    assert guard.circuit_state() == "half_open"
    trial = await guard.acquire(chat(3, 1))
    with pytest.raises(CircuitOpenError):
        await guard.acquire(chat(3, 1))  # only one trial at a time
    guard.settle(trial, usage=usage(1, 1), stream_opened=True)
    guard.record_result(None)
    assert guard.circuit_state() == "closed"
    await guard.acquire(chat(3, 1))


# --- GuardedGenerator ---------------------------------------------------------------


def generator(ft: FakeTime, replies: Sequence[ScriptedReply], guard: ProviderGuard,
              **retry: object) -> tuple[GuardedGenerator, ScriptedProvider]:
    provider = ScriptedProvider(replies, clock=ft.clock)
    policy = RetryPolicy(RetryConfig(**retry), RetryBudget(clock=ft.clock),  # type: ignore[arg-type]
                         rng=random.Random(5))
    return GuardedGenerator(provider, guard, policy, sleep=ft.sleep, clock=ft.clock), provider


def working_spans(ft: FakeTime) -> ServerSpanTracker:
    spans = ServerSpanTracker(clock=ft.clock)
    spans.switch("working")
    return spans


async def test_retry_then_success_is_counted_timed_and_summed() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=10_000)
    partial = ScriptedReply(content_chunks=("abcdef", "gh"), error=SERVER_ERROR,
                            fail_after_chunks=1)  # opens, streams 6 chars, then fails
    gen, provider = generator(ft, [partial, ok_reply(prompt=80, completion=20)], guard)
    spans = working_spans(ft)
    result = await gen.generate(chat(300, 100), spans)
    assert result.completion is not None and (result.attempts, result.retries) == (2, 1)
    assert len(provider.requests) == 2 and len(ft.sleeps) == 1
    assert result.tokens is not None
    # attempt 1 had no usage: estimated 100 in + 2 out; attempt 2 reported 80 + 20.
    assert (result.tokens.tokens_in, result.tokens.tokens_out) == (180, 22)
    assert result.tokens.usage_source == "estimated"
    summary = spans.finish()
    assert summary.retrying_ms == pytest.approx(ft.sleeps[0] * 1000, abs=0.01)
    total = summary.waiting_ms + summary.working_ms + summary.retrying_ms
    assert abs(total - summary.server_total_ms) <= 1.0
    assert guard.snapshot().tokens_last_minute == 200 + 100  # reservation kept + usage


async def test_non_retryable_and_exhausted_retries_stop_with_the_last_error() -> None:
    ft = FakeTime()
    gen, provider = generator(ft, [ScriptedReply(error=ProviderAuthError("no"))],
                              make_guard(ft))
    result = await gen.generate(chat(), working_spans(ft))
    assert isinstance(result.error, ProviderAuthError)
    assert (result.attempts, result.retries, len(provider.requests)) == (1, 0, 1)
    assert result.tokens is not None and result.tokens.usage_source == "none"

    gen, provider = generator(ft, [ScriptedReply(error=SERVER_ERROR)] * 5, make_guard(ft),
                              max_attempts=3)
    result = await gen.generate(chat(), working_spans(ft))
    assert result.error is SERVER_ERROR
    assert (result.attempts, result.retries, len(provider.requests)) == (3, 2, 3)


async def test_daily_exhaustion_fails_fast_with_zero_provider_calls() -> None:
    ft = FakeTime()
    guard = make_guard(ft, rpd_limit=4)
    gen, provider = generator(ft, [ok_reply()] * 6, guard)
    for _ in range(3):
        assert (await gen.generate(chat(), working_spans(ft))).completion is not None
    result = await gen.generate(chat(), working_spans(ft))
    assert isinstance(result.error, DailyBudgetExhaustedError)
    assert (result.attempts, result.retries) == (0, 0)
    assert len(provider.requests) == 3  # the fourth request never reached the provider
    assert ft.sleeps == []


async def test_upstream_daily_limit_is_not_retried_and_stops_later_calls() -> None:
    ft = FakeTime()
    daily = ProviderRateLimitError("day", scope="daily", basis="body")
    gen, provider = generator(ft, [ScriptedReply(error=daily), ok_reply()], make_guard(ft))
    first = await gen.generate(chat(), working_spans(ft))
    assert first.error is daily and (first.attempts, first.retries) == (1, 0)
    second = await gen.generate(chat(), working_spans(ft))
    assert isinstance(second.error, DailyBudgetExhaustedError) and second.attempts == 0
    assert len(provider.requests) == 1


async def test_response_headers_reach_the_guard() -> None:
    ft = FakeTime()
    guard = make_guard(ft)
    meta = ResponseMeta(status_code=200, headers={}, rate_limits=RateLimitSnapshot(
        limit_requests=1000, remaining_requests=998, limit_tokens=8000, remaining_tokens=7500,
        reset_tokens_s=2.0))
    reply = ScriptedReply(content_chunks=("ok",), usage=usage(80, 20), meta=meta)
    gen, _ = generator(ft, [reply], guard)
    await gen.generate(chat(), working_spans(ft))
    snapshot = guard.snapshot()
    assert (snapshot.rpd_limit, snapshot.rpd_remaining_header, snapshot.tpm_limit) == (
        1000, 998, 8000)


async def test_cancellation_mid_call_keeps_the_reservation_and_frees_the_slot() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=1000, max_concurrency=1)

    class Hanging(ScriptedProvider):
        async def stream_chat(self, request):  # type: ignore[no-untyped-def,override]
            self.requests.append(request)
            await asyncio.Event().wait()
            yield  # pragma: no cover

    gen = GuardedGenerator(Hanging([]), guard, sleep=ft.sleep, clock=ft.clock)
    task = asyncio.create_task(gen.generate(chat(300, 100), working_spans(ft)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert guard.snapshot().tokens_last_minute == 200  # tokens may have been spent
    await asyncio.wait_for(guard.acquire(chat(3, 1)), 1.0)  # the slot was released


# --- Pipeline integration -----------------------------------------------------------


def passage(index: int, words: int = 40) -> RetrievedPassage:
    return RetrievedPassage(
        rank=index, chunk_id=f"A_p{index}_c0", paper_id="1706.03762v7", arxiv_id="1706.03762",
        version=7, short_name="attention", section_number="3", section_title="Model",
        section_path=("3 Model",), section_kind="body", page_start=index, page_end=index,
        is_reference=False, text=" ".join(f"word{index}x{i}" for i in range(words)),
        bm25_rank=index, dense_rank=index, rrf_score=0.03, rerank_score=None,
        final_score=0.03, reference_downweighted=False,
    )


class Retriever:
    def __init__(self, passages: Sequence[RetrievedPassage]) -> None:
        self._passages = tuple(passages)

    def retrieve(self, query: str, *, paper_filter: Collection[str] | None = None,
                 include_references: bool = False) -> RetrievalResult:
        return RetrievalResult(query=query, setting="unit", paper_filter=None,
                               include_references=include_references, passages=self._passages)


def pipeline_with_guard(ft: FakeTime, replies: Sequence[ScriptedReply], guard: ProviderGuard,
                        passages: Sequence[RetrievedPassage], *, budget: bool = False,
                        ) -> tuple[Pipeline, ScriptedProvider]:
    gen, provider = generator(ft, replies, guard)
    pipeline = Pipeline(
        gateway=ToolGateway({"unit": Retriever(passages)}, clock=ft.clock), generator=gen,
        config=PipelineConfig(model_id="test-model", max_completion_tokens=100),
        top_k={"unit": 4}, budget=guard if budget else None,
    )
    return pipeline, provider


REQUEST = PipelineRequest(request_id="exp-20261113T080000Z-abc1234-000001",
                          question="How many layers are used?", retrieval_setting="unit")


async def test_pipeline_reports_retries_and_fast_fail_without_a_provider_call() -> None:
    ft = FakeTime()
    passages = [passage(i) for i in range(1, 5)]
    guard = make_guard(ft)
    pipeline, provider = pipeline_with_guard(
        ft, [ScriptedReply(error=SERVER_ERROR), ok_reply()], guard, passages)
    spans = ServerSpanTracker(clock=ft.clock)
    outcome = await pipeline.run(REQUEST, spans)
    summary = spans.finish()
    assert (outcome.kind, outcome.retries, outcome.provider_called) == ("llm_refusal", 1, True)
    assert (outcome.tokens_in, outcome.tokens_out, outcome.usage_source) == (80, 20, "provider")
    assert len(provider.requests) == 2 and summary.retrying_ms > 0
    assert abs(summary.waiting_ms + summary.working_ms + summary.retrying_ms
               - summary.server_total_ms) <= 1.0

    exhausted = make_guard(ft, rpd_limit=1)
    exhausted.settle(await exhausted.acquire(chat(3, 1)), usage=usage(1, 1), stream_opened=True)
    pipeline, provider = pipeline_with_guard(ft, [ok_reply()], exhausted, passages)
    outcome = await pipeline.run(REQUEST, ServerSpanTracker(clock=ft.clock))
    assert (outcome.status, outcome.error_code) == ("rate_limited", "daily_budget_exhausted")
    assert not outcome.provider_called and provider.requests == []
    assert (outcome.tokens_in, outcome.tokens_out, outcome.usage_source, outcome.retries) == (
        0, 0, "none", 0)
    assert outcome.delivered_text is None and len(outcome.passages) == 4  # pointers remain


async def test_tight_token_headroom_degrades_top_k() -> None:
    ft = FakeTime()
    passages = [passage(i, words=120) for i in range(1, 5)]
    roomy = make_guard(ft, tpm_limit=100_000)
    pipeline, provider = pipeline_with_guard(ft, [ok_reply()], roomy, passages, budget=True)
    outcome = await pipeline.run(REQUEST, ServerSpanTracker(clock=ft.clock))
    assert not outcome.degraded and len(outcome.passages) == 4
    full_estimate = roomy.estimate(provider.requests[0])

    tight = make_guard(ft, tpm_limit=full_estimate - 200)  # the full prompt cannot fit
    pipeline, provider = pipeline_with_guard(ft, [ok_reply()], tight, passages, budget=True)
    outcome = await pipeline.run(REQUEST, ServerSpanTracker(clock=ft.clock))
    assert outcome.degraded and 1 <= len(outcome.passages) < 4
    assert "[P4]" not in provider.requests[0].messages[1].content
    assert tight.estimate(provider.requests[0]) <= full_estimate - 200
    assert outcome.kind == "llm_refusal" and ft.sleeps == []


# --- Admission: capacity derivation -------------------------------------------------


def test_capacity_is_derived_from_explicit_inputs() -> None:
    derived = derive_capacity(CapacityInputs(rpm=30, tpm=8000, tokens_per_answer=2000,
                                             patience_quantile_s=20.0))
    assert derived.rate_bounds_per_s == {"rpm": 0.5, "tpm": pytest.approx(0.066667)}
    assert (derived.binding_bound, derived.capacity) == ("tpm", 1)  # floor(0.0667 x 20)
    by_requests = derive_capacity(CapacityInputs(rpm=60, patience_quantile_s=30.0))
    assert (by_requests.service_rate_per_s, by_requests.capacity) == (1.0, 30)
    stand_in = derive_capacity(CapacityInputs(service_slots=4, service_time_s=2.0, rpm=600,
                                              patience_quantile_s=10.0))
    assert (stand_in.binding_bound, stand_in.capacity) == ("service_time", 20)
    floor = derive_capacity(CapacityInputs(rpm=1, patience_quantile_s=5.0))
    assert floor.capacity == 1  # never zero
    assert derived.model_dump()["inputs"]["tokens_per_answer"] == 2000  # recomputable
    with pytest.raises(ValueError, match="cannot derive"):
        derive_capacity(CapacityInputs(patience_quantile_s=20.0, tpm=8000))


# --- Admission: queue behaviour -----------------------------------------------------


async def hold(queue: AdmissionQueue, release: asyncio.Event, order: list[str], name: str,
               **kwargs: object) -> None:
    async with queue.admit(**kwargs) as ticket:  # type: ignore[arg-type]
        order.append(f"{name}:{ticket.queue_depth_at_entry}")
        await release.wait()


async def test_queue_is_fifo_and_sheds_when_full() -> None:
    ft = FakeTime()
    queue = AdmissionQueue(capacity=2, service_rate_per_s=1.0, max_active=1, clock=ft.clock)
    release = asyncio.Event()
    order: list[str] = []
    tasks = [asyncio.create_task(hold(queue, release, order, name)) for name in "abc"]
    await asyncio.sleep(0.01)
    assert (queue.active, queue.depth, order) == (1, 2, ["a:0"])
    with pytest.raises(AdmissionRejectedError) as info:
        async with queue.admit():
            pass
    assert (info.value.error_code, info.value.queue_depth_at_entry) == ("queue_full", 2)
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["a:0", "b:0", "c:1"] and queue.peak_depth == 2
    assert (queue.active, queue.depth) == (0, 0)


async def test_predicted_wait_beyond_the_deadline_is_shed_on_entry() -> None:
    ft = FakeTime()
    queue = AdmissionQueue(capacity=10, service_rate_per_s=0.5, max_active=1, clock=ft.clock)
    release = asyncio.Event()
    order: list[str] = []
    running = asyncio.create_task(hold(queue, release, order, "a"))
    await asyncio.sleep(0.01)
    with pytest.raises(AdmissionRejectedError) as info:  # position 1 / 0.5 per s = 2 s
        async with queue.admit(deadline_ns=ft.in_s(1.5)):
            pass
    assert info.value.error_code == "predicted_wait_exceeds_deadline" and queue.depth == 0
    waiting = asyncio.create_task(hold(queue, release, order, "b", deadline_ns=ft.in_s(5)))
    await asyncio.sleep(0.01)
    assert queue.depth == 1
    release.set()
    await asyncio.gather(running, waiting)


async def test_deadline_and_disconnect_are_rechecked_when_a_slot_is_reached() -> None:
    ft = FakeTime()
    queue = AdmissionQueue(capacity=5, service_rate_per_s=10.0, max_active=1, clock=ft.clock)
    release = asyncio.Event()
    order: list[str] = []
    running = asyncio.create_task(hold(queue, release, order, "a"))
    await asyncio.sleep(0.01)
    connected = {"c": True}

    async def attempt(**kwargs: object) -> str:
        try:
            async with queue.admit(**kwargs):  # type: ignore[arg-type]
                return "served"
        except AdmissionRejectedError as exc:
            return f"{exc.error_code}:{exc.waited_ms:.0f}"

    late = asyncio.create_task(attempt(deadline_ns=ft.in_s(2)))
    gone = asyncio.create_task(attempt(is_connected=lambda: connected["c"]))
    await asyncio.sleep(0.01)
    ft.advance(3)  # the first waiter's deadline passes while it is queued
    connected["c"] = False  # the second waiter's client disconnects
    release.set()
    await running
    assert await late == "deadline_expired:3000"
    assert await gone == "client_gave_up:3000"
    assert (queue.active, queue.depth) == (0, 0)


async def test_disconnect_before_the_provider_call_spends_zero_tokens() -> None:
    ft = FakeTime()
    guard = make_guard(ft, tpm_limit=10_000)
    passages = [passage(i) for i in range(1, 5)]
    pipeline, provider = pipeline_with_guard(ft, [ok_reply(), ok_reply()], guard, passages)
    queue = AdmissionQueue(capacity=5, service_rate_per_s=10.0, max_active=1, clock=ft.clock)
    release = asyncio.Event()

    async def serve(hold_slot: bool) -> str:
        spans = ServerSpanTracker(clock=ft.clock)
        async with queue.admit():
            if hold_slot:
                await release.wait()
            return (await pipeline.run(REQUEST, spans)).kind

    first = asyncio.create_task(serve(True))
    await asyncio.sleep(0.01)
    queued = asyncio.create_task(serve(False))  # waits behind the first request
    await asyncio.sleep(0.01)
    assert queue.depth == 1
    queued.cancel()  # the client disconnects while still queued
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert queue.depth == 0 and provider.requests == []  # nothing was sent for it
    assert guard.snapshot().tokens_last_minute == 0
    release.set()
    assert await first == "llm_refusal"
    assert len(provider.requests) == 1  # only the request that was actually served
    assert await serve(False) == "llm_refusal" and queue.active == 0


def test_queue_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        AdmissionQueue(capacity=0, service_rate_per_s=1.0)
    with pytest.raises(ValueError):
        AdmissionQueue(capacity=1, service_rate_per_s=0.0)
