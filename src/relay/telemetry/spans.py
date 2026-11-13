"""Monotonic latency accounting."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

SpanKind = Literal["waiting", "working", "retrying"]
MonotonicClock = Callable[[], int]
WallClock = Callable[[], datetime]

_NS_PER_MS = 1_000_000


def ns_to_ms(ns: int) -> float:
    return round(ns / _NS_PER_MS, 3)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SpanSummary(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    server_total_ms: float
    waiting_ms: float
    working_ms: float
    retrying_ms: float
    provider_first_token_ms: float | None


class ClientTiming(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    start_time: datetime
    first_token_ms: float | None
    total_ms: float


class ServerSpanTracker:
    """Server-side span accounting for one request. Starts in the ``waiting`` state."""

    def __init__(self, clock: MonotonicClock = time.perf_counter_ns) -> None:
        self._clock = clock
        self._start_ns = clock()
        self._last_ns = self._start_ns
        self._current: SpanKind = "waiting"
        self._totals_ns: dict[SpanKind, int] = {"waiting": 0, "working": 0, "retrying": 0}
        self._provider_first_token_ns: int | None = None
        self._summary: SpanSummary | None = None

    @property
    def current(self) -> SpanKind:
        return self._current

    def switch(self, kind: SpanKind) -> None:
        """Close the current span and attribute subsequent time to ``kind``."""
        self._ensure_open()
        self._accumulate()
        self._current = kind

    def mark_provider_first_token(self) -> None:
        """Record the first visible content delta from the provider (first call wins)."""
        self._ensure_open()
        if self._provider_first_token_ns is None:
            self._provider_first_token_ns = self._clock() - self._start_ns

    def snapshot(self) -> SpanSummary:
        """Totals as of now, without closing the tracker (the final summary if finished)."""
        if self._summary is not None:
            return self._summary
        now = self._clock()
        totals = dict(self._totals_ns)
        totals[self._current] += now - self._last_ns
        first = self._provider_first_token_ns
        return SpanSummary(
            server_total_ms=ns_to_ms(now - self._start_ns),
            waiting_ms=ns_to_ms(totals["waiting"]),
            working_ms=ns_to_ms(totals["working"]),
            retrying_ms=ns_to_ms(totals["retrying"]),
            provider_first_token_ms=None if first is None else ns_to_ms(first),
        )

    def finish(self) -> SpanSummary:
        self._ensure_open()
        self._accumulate()
        total_ns = self._last_ns - self._start_ns
        first = self._provider_first_token_ns
        self._summary = SpanSummary(
            server_total_ms=ns_to_ms(total_ns),
            waiting_ms=ns_to_ms(self._totals_ns["waiting"]),
            working_ms=ns_to_ms(self._totals_ns["working"]),
            retrying_ms=ns_to_ms(self._totals_ns["retrying"]),
            provider_first_token_ms=None if first is None else ns_to_ms(first),
        )
        return self._summary

    def _accumulate(self) -> None:
        now = self._clock()
        if now < self._last_ns:
            raise RuntimeError("monotonic clock went backwards")
        self._totals_ns[self._current] += now - self._last_ns
        self._last_ns = now

    def _ensure_open(self) -> None:
        if self._summary is not None:
            raise RuntimeError("span tracker already finished")


class ClientTimer:
    """Client-observed timing: wall-clock start, first delivered byte, total."""

    def __init__(
        self,
        clock: MonotonicClock = time.perf_counter_ns,
        wall_clock: WallClock = _utc_now,
    ) -> None:
        self._clock = clock
        self._start_time = wall_clock()
        self._start_ns = clock()
        self._first_ns: int | None = None
        self._result: ClientTiming | None = None

    def mark_first_byte(self) -> None:
        if self._result is not None:
            raise RuntimeError("client timer already finished")
        if self._first_ns is None:
            self._first_ns = self._clock() - self._start_ns

    def finish(self) -> ClientTiming:
        if self._result is not None:
            raise RuntimeError("client timer already finished")
        total_ns = self._clock() - self._start_ns
        first = self._first_ns
        self._result = ClientTiming(
            start_time=self._start_time,
            first_token_ms=None if first is None else ns_to_ms(first),
            total_ms=ns_to_ms(total_ns),
        )
        return self._result
