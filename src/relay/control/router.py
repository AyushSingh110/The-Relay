"""Provider router: several model lanes, each with its own ProviderGuard (plan V2-09;
docs/ARCHITECTURE_V2.md §7).

A lane is one (provider, model) pair with its own guard (sliding-window RPM/TPM, daily
budget, circuit breaker), completion budget, reasoning settings and price. Lanes share one
retry policy and its retry budget, so an escalation is a retry like any other.

Routing rules, in order (§7):

1. The primary lane, if its guard can admit the request within ``max_wait_ms``.
2. Otherwise the next lane, in configured order, that can.
3. Otherwise the lane that frees up first: its guard waits for quota up to the request's
   deadline, exactly as v1's single lane did (with one lane, routing changes nothing).
   No lane can admit at all (circuit open, daily budget spent, request too large), or
   every wait ends after the deadline: ``NoLaneAvailableError``, which the caller turns
   into the honest "unavailable" reply with passage pointers and a ``retry_after_ms``.

A lane that refuses locally at the moment of the call (its quota was taken in between) is
skipped for the next candidate. A provider failure is not failed over: the lane's own
retries and circuit breaker handle it, and once the circuit opens, rule 2 sends new
requests elsewhere.

Escalation: a buffered answer that is truncated (``finish_reason == "length"``) or empty
(e.g. every completion token spent on reasoning, v1 P07) is retried exactly once on the
lane's ``escalate_to`` lane, if that lane can admit it and the retry budget allows. A
streamed answer is escalated by ``Pipeline.escalate_streaming`` through ``escalation``, and
only before any sentence was delivered.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from relay.control.pricing import (
    ROUTE_NOTE,
    LanePricing,
    LaneUse,
    Purpose,
    RouteRecord,
    cost_usd,
)
from relay.control.provider_guard import (
    DeadlineExpiredError,
    GuardedGenerator,
    GuardError,
    ProviderGuard,
    Sleep,
)
from relay.control.retry import RetryPolicy
from relay.orchestrator.pipeline import (
    AnswerGenerator,
    ContentSink,
    GenerationResult,
    escalation_reason,
    merge_escalation,
)
from relay.providers.base import ChatProvider, ChatRequest
from relay.schemas import ErrorCode
from relay.telemetry.spans import MonotonicClock, ServerSpanTracker

_NS_PER_MS: Final = 1_000_000
DEFAULT_MAX_WAIT_MS: Final = 2000.0


class NoLaneAvailableError(GuardError):
    """No lane can serve this request before its deadline. No provider call was made."""

    def __init__(self, message: str, *, error_code: ErrorCode,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after_s = retry_after_s


@dataclass
class ProviderLane:
    """One (provider, model) pair and everything that is its own."""

    name: str
    provider: ChatProvider
    model: str
    guard: ProviderGuard
    pricing: LanePricing | None = None  # None: the price is unknown, so cost_usd is null
    max_completion_tokens: int = 512
    is_reasoning_model: bool = False
    reasoning_effort: str | None = None
    escalate_to: str | None = None  # the stronger lane a truncated answer is retried on
    generator: GuardedGenerator | None = field(default=None, repr=False)  # set by the router

    def adapt(self, request: ChatRequest, *, keep_budget: bool = False) -> ChatRequest:
        """The request as this lane sends it: its model, its reasoning settings and (for
        answers) its completion budget. ``keep_budget``: the caller's own small budget (the
        follow-up rewriter's 64 tokens)."""
        budget = (min(request.max_completion_tokens, self.max_completion_tokens) if keep_budget
                  else self.max_completion_tokens)
        return request.model_copy(update={"model": self.model,
                                          "max_completion_tokens": budget,
                                          "reasoning_effort": self.reasoning_effort})

    @property
    def source(self) -> str:
        return str(self.provider.source)

    def use(self, result: GenerationResult, purpose: Purpose) -> LaneUse | None:
        """The ledger entry for one call on this lane (None when no call was made)."""
        if result.attempts == 0 or result.tokens is None:
            return None
        totals = result.tokens
        return LaneUse(lane=self.name, provider=self.source, model=self.model,
                       purpose=purpose, tokens_in=totals.tokens_in,
                       tokens_out=totals.tokens_out, usage_source=totals.usage_source,
                       cost_usd=cost_usd(totals.tokens_in, totals.tokens_out, self.pricing))


class ProviderRouter:
    """An ordered list of lanes; the first is the primary."""

    def __init__(self, lanes: Sequence[ProviderLane], *, retry: RetryPolicy | None = None,
                 max_wait_ms: float = DEFAULT_MAX_WAIT_MS, sleep: Sleep = asyncio.sleep,
                 clock: MonotonicClock = time.perf_counter_ns) -> None:
        if not lanes:
            raise ValueError("a router needs at least one lane")
        names = [lane.name for lane in lanes]
        if len(set(names)) != len(names):
            raise ValueError(f"lane names must be unique: {names}")
        for lane in lanes:
            if lane.escalate_to is not None and (lane.escalate_to not in names
                                                 or lane.escalate_to == lane.name):
                raise ValueError(f"lane {lane.name!r}: escalate_to must name another lane")
        if max_wait_ms < 0:
            raise ValueError("max_wait_ms cannot be negative")
        self.retry = retry or RetryPolicy()
        self.max_wait_ms = max_wait_ms
        self._clock = clock
        self.lanes: tuple[ProviderLane, ...] = tuple(lanes)
        self._by_name = {lane.name: lane for lane in self.lanes}
        for lane in self.lanes:  # each lane's guard is its own; the retry budget is shared
            lane.generator = GuardedGenerator(lane.provider, lane.guard, self.retry,
                                              sleep=sleep, clock=clock, record_first=False)

    @property
    def primary(self) -> ProviderLane:
        return self.lanes[0]

    def lane(self, name: str) -> ProviderLane:
        return self._by_name[name]

    def candidates(self, request: ChatRequest, *, deadline_ns: int | None = None,
                   max_wait_ms: float | None = None, keep_budget: bool = False,
                   ) -> tuple[list[ProviderLane], list[str], float | None]:
        """The lanes to try, in order, the lanes skipped (``"lane:reason"``), and the
        shortest predicted wait in ms when no lane is usable at all."""
        limit_ns = (self.max_wait_ms if max_wait_ms is None else max_wait_ms) * _NS_PER_MS
        now = self._clock()
        remaining = None if deadline_ns is None else deadline_ns - now
        if remaining is not None and remaining <= 0:
            return [self.primary], [], None  # its guard reports the expired deadline (v1)
        prompt: list[ProviderLane] = []
        waiting: list[tuple[int, ProviderLane]] = []
        skipped: list[str] = []
        for lane in self.lanes:
            forecast = lane.guard.predict(lane.adapt(request, keep_budget=keep_budget))
            if forecast.wait_ns is None:
                skipped.append(f"{lane.name}:{forecast.refusal}")
            elif forecast.wait_ns <= limit_ns:
                prompt.append(lane)
            else:
                waiting.append((forecast.wait_ns, lane))
                skipped.append(f"{lane.name}:wait_{round(forecast.wait_ns / _NS_PER_MS)}ms")
        waiting.sort(key=lambda item: item[0])  # stable: configured order on ties
        late = [lane for wait, lane in waiting if remaining is None or wait < remaining]
        shortest = None if not waiting else round(waiting[0][0] / _NS_PER_MS, 1)
        return prompt + late, skipped, shortest

    def select_lane(self, request: ChatRequest, max_wait_ms: float | None = None, *,
                    deadline_ns: int | None = None) -> ProviderLane:
        """The lane rules 1-3 choose; ``NoLaneAvailableError`` when none can serve."""
        lanes, skipped, shortest = self.candidates(request, deadline_ns=deadline_ns,
                                                   max_wait_ms=max_wait_ms)
        if not lanes:
            raise self.unavailable(skipped, shortest)
        return lanes[0]

    def unavailable(self, skipped: Sequence[str], shortest_ms: float | None
                    ) -> NoLaneAvailableError:
        """The error for "no lane can serve", with the primary lane's reason as its code
        (a busy primary beyond the deadline is ``predicted_wait_exceeds_deadline``)."""
        reason = skipped[0].split(":", 1)[1] if skipped else ""
        code: ErrorCode = ("circuit_open" if reason == "circuit_open"
                           else "daily_budget_exhausted" if reason == "daily_budget_exhausted"
                           else "payload_too_large" if reason == "payload_too_large"
                           else "predicted_wait_exceeds_deadline")
        return NoLaneAvailableError(
            f"no lane can serve this request ({', '.join(skipped)})", error_code=code,
            retry_after_s=None if shortest_ms is None else shortest_ms / 1000)

    def escalate(self, failed_lane: ProviderLane, reason: str,
                 request: ChatRequest) -> ProviderLane | None:
        """The lane for the single escalation retry, or None: no ``escalate_to`` lane, it
        cannot admit the request now, or the shared retry budget is spent. A granted
        escalation spends one retry from the budget."""
        if failed_lane.escalate_to is None:
            return None
        target = self._by_name[failed_lane.escalate_to]
        if not target.guard.predict(target.adapt(request)).admits:
            return None
        if not self.retry.budget.try_spend():
            return None
        return target

    def fits(self, request: ChatRequest) -> bool:
        """Whether some lane has the token headroom for this prompt now (the pipeline drops
        passages only when none has: degraded top-k)."""
        return any(lane.guard.fits(lane.adapt(request)) for lane in self.lanes)


class RoutedGenerator:
    """The pipeline's ``AnswerGenerator``: picks a lane per call, records the route and
    cost, and escalates a buffered truncated answer once. ``purpose="rewrite"`` (the
    follow-up rewriter) keeps the caller's own completion budget and never escalates."""

    def __init__(self, router: ProviderRouter, *, purpose: Purpose = "answer",
                 escalate: bool = True, pinned: ProviderLane | None = None) -> None:
        self.router = router
        self._purpose: Purpose = purpose
        self._escalate = escalate
        self._pinned = pinned  # a single lane (an escalation retry, a pinned rewriter)

    def pinned(self, lane: ProviderLane, purpose: Purpose) -> RoutedGenerator:
        return RoutedGenerator(self.router, purpose=purpose, escalate=False, pinned=lane)

    async def generate(self, request: ChatRequest, spans: ServerSpanTracker, *,
                       deadline_ns: int | None = None,
                       on_content: ContentSink | None = None) -> GenerationResult:
        keep = self._purpose == "rewrite"
        if self._purpose != "escalation":  # one first attempt per request (retry budget)
            self.router.retry.budget.record_first_attempt()
        if self._pinned is not None:
            lanes, skipped, shortest = [self._pinned], [], None
        else:
            lanes, skipped, shortest = self.router.candidates(request, deadline_ns=deadline_ns,
                                                              keep_budget=keep)
        note = ROUTE_NOTE.get()
        if not lanes:
            error = self.router.unavailable(skipped, shortest)
            return GenerationResult(None, error, False, 0, attempts=0, route=RouteRecord(
                lane=None, provider=None, model=None, skipped=tuple(skipped),
                retry_after_ms=shortest))
        result: GenerationResult | None = None
        lane = lanes[0]
        for lane in lanes:
            if note is not None:  # a stream cancelled mid-call still knows its lane
                note.lane, note.provider, note.model = lane.name, lane.source, lane.model
                note.pricing, note.purpose = lane.pricing, self._purpose
                note.fallback, note.skipped = lane is not self.router.primary, list(skipped)
            generator = lane.generator
            if generator is None:  # set in ProviderRouter.__init__
                raise RuntimeError(f"lane {lane.name!r} has no generator")
            result = await generator.generate(lane.adapt(request, keep_budget=keep), spans,
                                              deadline_ns=deadline_ns, on_content=on_content)
            local_refusal = (result.attempts == 0 and isinstance(result.error, GuardError)
                             and not isinstance(result.error, DeadlineExpiredError))
            if not local_refusal:
                break
            skipped.append(f"{lane.name}:{result.error.error_code}")  # type: ignore[union-attr]
        if result is None:  # lanes is not empty
            raise RuntimeError("no lane was tried")
        if result.attempts == 0 and result.error is not None:  # every lane refused
            return replace(result, route=RouteRecord(
                lane=None, provider=None, model=None, skipped=tuple(skipped),
                retry_after_ms=shortest))
        use = lane.use(result, self._purpose)
        result = replace(result, route=RouteRecord(
            lane=lane.name, provider=lane.source, model=lane.model,
            fallback=lane is not self.router.primary, uses=() if use is None else (use,),
            skipped=tuple(skipped)))
        if not self._escalate or on_content is not None:
            return result  # streaming: Pipeline.escalate_streaming decides
        second = self.escalation(result, request)
        if second is None:
            return result
        reason = escalation_reason(result) or "escalated"
        retry = await second.generate(request, spans, deadline_ns=deadline_ns)
        return merge_escalation(result, retry, reason)

    def escalation(self, result: GenerationResult,
                   request: ChatRequest) -> AnswerGenerator | None:
        """``Escalating``: the generator for the one escalation retry of ``result``."""
        if not self._escalate or result.route is None or result.route.lane is None:
            return None
        reason = escalation_reason(result)
        if reason is None:
            return None
        target = self.router.escalate(self.router.lane(result.route.lane), reason, request)
        return None if target is None else self.pinned(target, "escalation")


__all__ = ["DEFAULT_MAX_WAIT_MS", "NoLaneAvailableError", "ProviderLane", "ProviderRouter",
           "RoutedGenerator"]
