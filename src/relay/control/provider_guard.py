"""ProviderGuard: the sole owner of provider quota state (ARCHITECTURE.md section 9)."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from relay.control.retry import RetryPolicy
from relay.control.state import QuotaLimits, SharedQuota, StateUnavailableError
from relay.orchestrator.pipeline import (
    ContentSink,
    GenerationResult,
    SingleCallGenerator,
    TokenTotals,
)
from relay.providers.base import ChatProvider, ChatRequest, TokenUsage
from relay.providers.errors import (
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderStreamIncompleteError,
    ProviderTimeoutError,
)
from relay.providers.headers import RateLimitSnapshot
from relay.schemas import ErrorCode, UsageSource
from relay.telemetry.spans import MonotonicClock, ServerSpanTracker

_NS: Final = 1_000_000_000
WINDOW_NS: Final = 60 * _NS
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")

Sleep = Callable[[float], Awaitable[Any]]
CircuitState = Literal["closed", "open", "half_open"]


class GuardConfig(BaseModel):
    """Limits come from the provider console/docs or measured headers; ``None`` = unknown."""

    model_config = _STRICT

    max_concurrency: Annotated[int, Field(ge=1)] = 2
    rpm_limit: Annotated[int, Field(ge=1)] | None = None
    tpm_limit: Annotated[int, Field(ge=1)] | None = None
    rpd_limit: Annotated[int, Field(ge=1)] | None = None
    tpd_limit: Annotated[int, Field(ge=1)] | None = None
    daily_safety_fraction: Annotated[float, Field(gt=0.0, le=1.0)] = 0.75
    # Owner-approved fallback (P02): 3.0 characters per token until usage is observed.
    initial_chars_per_token: Annotated[float, Field(gt=0.0)] = 3.0
    min_chars_per_token: Annotated[float, Field(gt=0.0)] = 1.0
    max_chars_per_token: Annotated[float, Field(gt=0.0)] = 6.0
    adapt_weight: Annotated[float, Field(gt=0.0, le=1.0)] = 0.2
    adapt_safety_margin: Annotated[float, Field(gt=0.0, le=1.0)] = 0.9
    circuit_failure_threshold: Annotated[int, Field(ge=1)] = 5
    circuit_cooldown_s: Annotated[float, Field(gt=0.0)] = 30.0
    # F-04 (owner 2026-11-22): the least time between two call starts on this lane, per
    # process (0 = no spacing). For a provider's request-rate limit that its headers do not
    # show (F-03: a 429 after 10 calls in ~2 s, with token and daily headroom left).
    min_interval_ms: Annotated[int, Field(ge=0)] = 0


class GuardError(ProviderError):
    """The guard refused locally. No provider call was made for this decision."""


class DailyBudgetExhaustedError(GuardError):
    error_code: ErrorCode = "daily_budget_exhausted"


class CircuitOpenError(GuardError):
    error_code: ErrorCode = "circuit_open"


class QuotaWaitExceedsDeadlineError(GuardError):
    error_code: ErrorCode = "predicted_wait_exceeds_deadline"


class DeadlineExpiredError(GuardError):
    error_code: ErrorCode = "deadline_expired"


class RequestExceedsTokenLimitError(GuardError):
    """The request alone is larger than the whole per-minute token limit."""

    error_code: ErrorCode = "payload_too_large"


class SharedQuotaUnavailableError(GuardError):
    """V2-13: the shared quota counters could not be reached. The guard fails closed: with
    several workers, the provider's limits cannot be kept without them."""

    error_code: ErrorCode = "internal_error"


@dataclass
class _TokenEntry:
    at_ns: int
    tokens: int


@dataclass(frozen=True)
class Admission:
    """A forecast of ``acquire``: the quota wait in ns, or why it would refuse."""

    wait_ns: int | None  # None: it would refuse (see ``refusal``)
    refusal: ErrorCode | None = None

    @property
    def admits(self) -> bool:
        return self.wait_ns is not None


@dataclass(frozen=True)
class Permit:
    """A reservation for one provider attempt. Settle it exactly once."""

    entry: _TokenEntry
    estimate: int
    prompt_chars: int
    day: date
    shared: str | None = None  # V2-13: the reservation in the shared window, if any


class Settlement(BaseModel):
    model_config = _STRICT

    tokens_in: int
    tokens_out: int
    usage_source: UsageSource
    reserved: int
    charged: int


class GuardSnapshot(BaseModel):
    """Read-only view of the guard's state, for traces and tests."""

    model_config = _STRICT

    requests_last_minute: int
    tokens_last_minute: int
    tpm_available: int | None
    day_requests: int
    day_tokens: int
    rpd_limit: int | None
    rpd_remaining_header: int | None
    tpm_limit: int | None
    rpm_limit: int | None
    tpd_limit: int | None
    chars_per_token: float
    circuit: CircuitState
    daily_stop: str | None


def _utc_today() -> date:
    return datetime.now(UTC).date()


class ProviderGuard:
    def __init__(
        self,
        config: GuardConfig | None = None,
        *,
        clock: MonotonicClock = time.perf_counter_ns,
        sleep: Sleep = asyncio.sleep,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self.config = config or GuardConfig()
        self._clock = clock
        self._sleep = sleep
        self._today = today
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._interval_ns = self.config.min_interval_ms * 1_000_000
        self._last_start_ns: int | None = None  # F-04: the last claimed call start
        self._requests: deque[int] = deque()
        self._tokens: deque[_TokenEntry] = deque()
        self._day = today()
        self._day_requests = 0
        self._day_tokens = 0
        self._daily_exhausted_upstream = False
        self._rpd_limit_header: int | None = None
        self._rpd_remaining_header: int | None = None
        self._tpm_limit_header: int | None = None
        self._tpm_header: tuple[int, int, int] | None = None  # (remaining, at_ns, until_ns)
        self._reserved_since_header = 0
        self._blocked_until_ns = 0
        self._chars_per_token = self.config.initial_chars_per_token
        self._failures = 0
        self._open_until_ns = 0
        self._trial_in_flight = False
        self._shared: SharedQuota | None = None

    def share(self, quota: SharedQuota) -> None:
        """V2-13: also reserve every call in ``quota``, the minute window and daily counters
        that all workers share. This guard's own windows still apply (they see only this
        worker's calls). ``predict`` stays local: the router's forecast is this worker's."""
        self._shared = quota

    async def drain_shared(self) -> None:
        """Shutdown: wait for the shared settlements still in flight."""
        if self._shared is not None:
            await self._shared.drain()

    def _shared_limits(self) -> QuotaLimits:
        fraction = self.config.daily_safety_fraction
        rpd, tpd = self.rpd_limit, self.config.tpd_limit
        return QuotaLimits(rpm=self.config.rpm_limit, tpm=self.tpm_limit,
                           rpd_stop=None if rpd is None else fraction * rpd,
                           tpd_stop=None if tpd is None else fraction * tpd)

    # -- limits and windows --

    @property
    def chars_per_token(self) -> float:
        return self._chars_per_token

    @property
    def tpm_limit(self) -> int | None:
        return self.config.tpm_limit or self._tpm_limit_header

    @property
    def rpd_limit(self) -> int | None:
        return self.config.rpd_limit or self._rpd_limit_header

    def _trim(self, now: int) -> None:
        horizon = now - WINDOW_NS
        while self._requests and self._requests[0] <= horizon:
            self._requests.popleft()
        while self._tokens and self._tokens[0].at_ns <= horizon:
            self._tokens.popleft()
        if self._tpm_header is not None and now >= self._tpm_header[2]:
            self._tpm_header = None
        today = self._today()
        if today != self._day:  # new quota day: local daily state starts over
            self._day, self._day_requests, self._day_tokens = today, 0, 0
            self._daily_exhausted_upstream = False
            self._rpd_remaining_header = None

    def estimate(self, request: ChatRequest) -> int:
        """Conservative reservation: prompt estimate plus the full completion allowance."""
        prompt = math.ceil(request.prompt_chars / self._chars_per_token)
        return prompt + request.max_completion_tokens

    def _tpm_available(self) -> int | None:
        limit = self.tpm_limit
        if limit is None:
            return None
        available = limit - sum(entry.tokens for entry in self._tokens)
        if self._tpm_header is not None:  # provider-reported headroom overrides the local view
            available = min(available, self._tpm_header[0] - self._reserved_since_header)
        return available

    def fits(self, request: ChatRequest) -> bool:
        """Whether the request fits the token headroom right now (drives degraded top-k)."""
        now = self._clock()
        self._trim(now)
        available = self._tpm_available()
        return available is None or self.estimate(request) <= available

    def daily_stop_reason(self) -> str | None:
        self._trim(self._clock())
        fraction = self.config.daily_safety_fraction
        if self._daily_exhausted_upstream:
            return "provider reported a daily limit (429)"
        rpd = self.rpd_limit
        if rpd is not None:
            if self._day_requests >= fraction * rpd:
                return f"local daily requests {self._day_requests} >= {fraction} x {rpd}"
            remaining = self._rpd_remaining_header
            if remaining is not None and rpd - remaining >= fraction * rpd:
                return f"header shows {rpd - remaining}/{rpd} daily requests used"
        tpd = self.config.tpd_limit
        if tpd is not None and self._day_tokens >= fraction * tpd:
            return f"local daily tokens {self._day_tokens} >= {fraction} x {tpd}"
        return None

    def _quota_wait_ns(self, now: int, estimate: int) -> int:
        """Nanoseconds until this request fits the minute windows (0 = now)."""
        wait = max(0, self._blocked_until_ns - now)
        rpm = self.config.rpm_limit
        if rpm is not None and len(self._requests) >= rpm:
            wait = max(wait, self._requests[len(self._requests) - rpm] + WINDOW_NS - now)
        limit = self.tpm_limit
        if limit is None:
            return wait
        if self._tpm_header is not None and (
            estimate > self._tpm_header[0] - self._reserved_since_header
        ):
            wait = max(wait, self._tpm_header[2] - now)
        needed = estimate - (limit - sum(entry.tokens for entry in self._tokens))
        for entry in self._tokens:
            if needed <= 0:
                break
            needed -= entry.tokens
            wait = max(wait, entry.at_ns + WINDOW_NS - now)
        return wait

    def predict(self, request: ChatRequest) -> Admission:
        """What ``acquire`` would decide now, without reserving anything (V2-09: the router
        chooses a lane with it). Concurrency slots are not part of the forecast."""
        now = self._clock()
        self._trim(now)
        state = self.circuit_state()
        if state == "open" or (state == "half_open" and self._trial_in_flight):
            return Admission(None, "circuit_open")
        if self.daily_stop_reason() is not None:
            return Admission(None, "daily_budget_exhausted")
        estimate = self.estimate(request)
        limit = self.tpm_limit
        if limit is not None and estimate > limit:
            return Admission(None, "payload_too_large")
        return Admission(self._quota_wait_ns(now, estimate), None)

    # -- circuit breaker --

    def circuit_state(self) -> CircuitState:
        if self._failures < self.config.circuit_failure_threshold:
            return "closed"
        return "open" if self._clock() < self._open_until_ns else "half_open"

    def _check_circuit(self) -> None:
        state = self.circuit_state()
        if state == "open" or (state == "half_open" and self._trial_in_flight):
            raise CircuitOpenError("circuit open: provider calls suspended")
        if state == "half_open":
            self._trial_in_flight = True

    def _spacing_wait_ns(self, now: int) -> int:
        """F-04: how long until this lane may start another call (0: now)."""
        if not self._interval_ns or self._last_start_ns is None:
            return 0
        return self._last_start_ns + self._interval_ns - now

    # -- reservation and settlement --

    async def acquire(self, request: ChatRequest, *, deadline_ns: int | None = None,
                      spans: ServerSpanTracker | None = None) -> Permit:
        """Wait for a slot and quota, then reserve. Raises ``GuardError`` to refuse."""
        await self._wait(self._semaphore.acquire(), spans)
        shared: str | None = None
        shared_day = self._day
        try:
            while True:
                now = self._clock()
                self._trim(now)
                if deadline_ns is not None and now >= deadline_ns:
                    raise DeadlineExpiredError("deadline expired before the provider call")
                if self.circuit_state() == "open":  # fail fast: do not wait on quota first
                    raise CircuitOpenError("circuit open: provider calls suspended")
                reason = self.daily_stop_reason()
                if reason is not None:
                    raise DailyBudgetExhaustedError(f"daily safety stop: {reason}")
                estimate = self.estimate(request)
                limit = self.tpm_limit
                if limit is not None and estimate > limit:
                    raise RequestExceedsTokenLimitError(
                        f"request needs ~{estimate} tokens; the per-minute limit is {limit}"
                    )
                wait_ns = max(self._quota_wait_ns(now, estimate), self._spacing_wait_ns(now))
                if wait_ns <= 0 and self._interval_ns:
                    self._last_start_ns = now  # claimed before any await (F-04 spacing)
                if wait_ns <= 0 and self._shared is not None:
                    shared_day = self._day
                    wait_ns, shared = await self._reserve_shared(estimate, spans)
                    now = self._clock()
                if wait_ns <= 0:
                    break
                if deadline_ns is not None and now + wait_ns >= deadline_ns:
                    raise QuotaWaitExceedsDeadlineError(
                        f"quota frees in {wait_ns / _NS:.2f}s, after the deadline"
                    )
                await self._wait(self._sleep(wait_ns / _NS), spans)
            self._check_circuit()
        except BaseException:
            self._semaphore.release()
            if shared is not None and self._shared is not None:  # no call: give it back
                self._shared.cancel(shared, estimate, shared_day)
            raise
        entry = _TokenEntry(now, estimate)
        self._requests.append(now)
        self._tokens.append(entry)
        self._day_requests += 1
        self._day_tokens += estimate
        if self._tpm_header is not None:
            self._reserved_since_header += estimate
        return Permit(entry=entry, estimate=estimate, prompt_chars=request.prompt_chars,
                      day=self._day if shared is None else shared_day, shared=shared)

    async def _reserve_shared(self, estimate: int,
                              spans: ServerSpanTracker | None) -> tuple[int, str | None]:
        """(wait ns, None) or (0, reservation) from the shared window."""
        if self._shared is None:
            return 0, None
        try:
            decision = await self._shared.reserve(estimate, self._shared_limits(), self._day)
        except StateUnavailableError as exc:
            raise SharedQuotaUnavailableError(str(exc)) from exc
        if decision.refusal == "daily_budget_exhausted":
            raise DailyBudgetExhaustedError("daily safety stop: shared daily counters")
        if decision.refusal == "payload_too_large":
            raise RequestExceedsTokenLimitError(
                f"request needs ~{estimate} tokens; over the shared per-minute limit")
        if decision.reservation is None:
            return decision.wait_ms * 1_000_000, None
        return 0, decision.reservation

    async def _wait(self, awaitable: Awaitable[Any], spans: ServerSpanTracker | None) -> None:
        """Time spent waiting for a slot or quota is ``waiting``, not ``working``."""
        if spans is None:
            await awaitable
            return
        previous = spans.current
        spans.switch("waiting")
        try:
            await awaitable
        finally:
            spans.switch(previous)

    def settle(self, permit: Permit, *, usage: TokenUsage | None, stream_opened: bool,
               received_chars: int | None = 0) -> Settlement:
        """Replace the reservation with what the provider reported, and free the slot.

        Without a usage report (the stream broke, or the caller cancelled it), the charge is
        the prompt estimate plus the tokens of the text actually received, counted with the
        same chars-per-token rule as the prompt (V2-14: a verification stop used to keep the
        whole completion allowance, starving the TPM budget). ``received_chars=None`` means
        the amount received is unknown, and the whole reservation stays charged."""
        self._semaphore.release()
        self._trial_in_flight = False  # a half-open trial ends when its call is settled
        if usage is not None:
            charged = usage.prompt_tokens + usage.completion_tokens
            result = (usage.prompt_tokens, usage.completion_tokens, "provider")
            self._adapt(permit.prompt_chars, usage.prompt_tokens)
        elif stream_opened:
            prompt = math.ceil(permit.prompt_chars / self._chars_per_token)
            if received_chars is None:
                charged = permit.estimate
                result = (prompt, permit.estimate - prompt, "estimated")
            else:
                received = math.ceil(received_chars / self._chars_per_token)
                charged = prompt + received
                result = (prompt, received, "estimated")
        else:
            charged = 0
            result = (0, 0, "none")
        delta = charged - permit.estimate
        if permit.shared is not None and self._shared is not None:
            self._shared.settle(permit.shared, permit.estimate, charged, permit.day)
        permit.entry.tokens = charged
        if permit.day == self._day:
            self._day_tokens += delta
        # The provider's own headroom is given back only when it settled the call (it
        # reported usage) or never opened it. A stream cut off mid-way may keep its whole
        # reservation at the provider (the stand-in does), so header headroom stays reduced
        # until the provider's next headers say otherwise.
        provider_settled = usage is not None or not stream_opened
        if self._tpm_header is not None and (provider_settled or delta > 0):
            self._reserved_since_header += delta
        return Settlement(tokens_in=result[0], tokens_out=result[1], usage_source=result[2],
                          reserved=permit.estimate, charged=charged)

    def _adapt(self, prompt_chars: int, prompt_tokens: int) -> None:
        """Move chars-per-token towards what the provider counted, staying conservative."""
        if prompt_chars <= 0 or prompt_tokens <= 0:
            return
        observed = self.config.adapt_safety_margin * prompt_chars / prompt_tokens
        weight = self.config.adapt_weight
        blended = (1 - weight) * self._chars_per_token + weight * observed
        self._chars_per_token = min(self.config.max_chars_per_token,
                                    max(self.config.min_chars_per_token, blended))

    # -- feedback from the provider --

    def sync_headers(self, snapshot: RateLimitSnapshot) -> None:
        """Apply response headers. ``*-requests`` = per day; ``*-tokens`` = per minute."""
        now = self._clock()
        self._trim(now)
        if snapshot.limit_requests is not None:
            self._rpd_limit_header = snapshot.limit_requests
        if snapshot.remaining_requests is not None:
            self._rpd_remaining_header = snapshot.remaining_requests
        if snapshot.limit_tokens is not None:
            self._tpm_limit_header = snapshot.limit_tokens
        if snapshot.remaining_tokens is not None:
            reset_s = snapshot.reset_tokens_s if snapshot.reset_tokens_s is not None else 60.0
            self._tpm_header = (snapshot.remaining_tokens, now, now + int(reset_s * _NS))
            self._reserved_since_header = 0

    def record_result(self, error: ProviderError | None) -> None:
        """Feed one attempt's outcome to the circuit breaker and rate-limit state."""
        now = self._clock()
        if error is None:
            self._failures = 0
            return
        if isinstance(error, ProviderRateLimitError):
            if error.scope == "daily":
                self._daily_exhausted_upstream = True
            elif error.retry_after_s is not None:
                self._blocked_until_ns = max(self._blocked_until_ns,
                                             now + int(error.retry_after_s * _NS))
            return  # a 429 is quota feedback, not a provider health failure
        if isinstance(error, ProviderServerError | ProviderTimeoutError
                      | ProviderConnectionError | ProviderStreamIncompleteError):
            self._failures += 1
            if self._failures >= self.config.circuit_failure_threshold:
                self._open_until_ns = now + int(self.config.circuit_cooldown_s * _NS)

    def snapshot(self) -> GuardSnapshot:
        now = self._clock()
        self._trim(now)
        return GuardSnapshot(
            requests_last_minute=len(self._requests),
            tokens_last_minute=sum(entry.tokens for entry in self._tokens),
            tpm_available=self._tpm_available(), day_requests=self._day_requests,
            day_tokens=self._day_tokens, rpd_limit=self.rpd_limit,
            rpd_remaining_header=self._rpd_remaining_header, tpm_limit=self.tpm_limit,
            rpm_limit=self.config.rpm_limit, tpd_limit=self.config.tpd_limit,
            chars_per_token=round(self._chars_per_token, 4), circuit=self.circuit_state(),
            daily_stop=self.daily_stop_reason(),
        )


class GuardedGenerator:
    """The pipeline's ``AnswerGenerator`` with quota control and visible retries."""

    def __init__(self, provider: ChatProvider, guard: ProviderGuard,
                 retry: RetryPolicy | None = None, *, sleep: Sleep = asyncio.sleep,
                 clock: MonotonicClock = time.perf_counter_ns,
                 record_first: bool = True) -> None:
        self._call = SingleCallGenerator(provider)
        # V2-09: under the router, the RoutedGenerator records one first attempt per
        # request (an escalation or a skipped lane is not a new request)
        self._record_first = record_first
        self._guard = guard
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock

    async def generate(self, request: ChatRequest, spans: ServerSpanTracker, *,
                       deadline_ns: int | None = None,
                       on_content: ContentSink | None = None) -> GenerationResult:
        """With ``on_content`` (streaming), content is forwarded as it arrives, and an
        attempt that already forwarded content is never retried: the client has seen it,
        and a retry would produce a different answer."""
        if self._record_first:
            self._retry.budget.record_first_attempt()
        forwarded = False
        attempt_chars = 0  # content and reasoning characters received by the current attempt

        def count(chars: int) -> None:
            nonlocal attempt_chars
            attempt_chars += chars

        def forward(text: str) -> None:
            nonlocal forwarded
            forwarded = True
            if on_content is not None:
                on_content(text)

        attempts = 0
        tokens_in = tokens_out = 0
        sources: set[UsageSource] = set()

        def totals() -> TokenTotals:
            source: UsageSource = ("estimated" if "estimated" in sources
                                   else "provider" if "provider" in sources else "none")
            return TokenTotals(tokens_in, tokens_out, source)

        def finish(result: GenerationResult) -> GenerationResult:
            return GenerationResult(
                result.completion, result.error, result.stream_opened, result.received_chars,
                retries=max(0, attempts - 1), attempts=attempts, tokens=totals(),
            )

        while True:
            try:
                permit = await self._guard.acquire(request, deadline_ns=deadline_ns, spans=spans)
            except GuardError as refusal:
                return finish(GenerationResult(None, refusal, False, 0, attempts=0))
            attempts += 1
            attempt_chars = 0
            try:
                result = await self._call.generate(
                    request, spans, on_content=None if on_content is None else forward,
                    on_received=count)
            except BaseException:
                # Cancelled mid-call. Streaming: charge the text received so far. Buffered:
                # what the provider produced is unknown, so the reservation stays charged.
                self._guard.settle(permit, usage=None, stream_opened=True,
                                   received_chars=attempt_chars if on_content is not None
                                   else None)
                raise
            usage = None if result.completion is None else result.completion.usage
            settled = self._guard.settle(permit, usage=usage, stream_opened=result.stream_opened,
                                         received_chars=result.received_chars)
            tokens_in += settled.tokens_in
            tokens_out += settled.tokens_out
            sources.add(settled.usage_source)
            meta = result.completion.meta if result.completion else (
                result.error.meta if result.error else None)
            if meta is not None:
                self._guard.sync_headers(meta.rate_limits)
            self._guard.record_result(result.error)
            if result.error is None or forwarded:
                return finish(result)
            remaining = None if deadline_ns is None else (deadline_ns - self._clock()) / _NS
            decision = self._retry.decide(result.error, attempt=attempts, remaining_s=remaining)
            if not decision.retry:
                return finish(result)
            spans.switch("retrying")
            try:
                await self._sleep(decision.delay_s)
            finally:
                spans.switch("working")


__all__ = [
    "Admission",
    "CircuitOpenError",
    "DailyBudgetExhaustedError",
    "DeadlineExpiredError",
    "GuardConfig",
    "GuardError",
    "GuardSnapshot",
    "GuardedGenerator",
    "Permit",
    "ProviderGuard",
    "QuotaWaitExceedsDeadlineError",
    "RequestExceedsTokenLimitError",
    "Settlement",
    "SharedQuotaUnavailableError",
]
