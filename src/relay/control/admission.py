"""Admission queue (application layer; ARCHITECTURE.md sections 9.2 and 15)."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from relay.errors import RelayError
from relay.schemas import ErrorCode
from relay.telemetry.spans import MonotonicClock, ns_to_ms

_NS: Final = 1_000_000_000
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


class CapacityInputs(BaseModel):
    """Measured or configured inputs. ``None`` means unknown and is left out."""

    model_config = _STRICT

    patience_quantile_s: Annotated[float, Field(gt=0.0)]
    rpm: Annotated[float, Field(gt=0.0)] | None = None
    tpm: Annotated[float, Field(gt=0.0)] | None = None
    tokens_per_answer: Annotated[float, Field(gt=0.0)] | None = None
    service_slots: Annotated[int, Field(ge=1)] | None = None
    service_time_s: Annotated[float, Field(gt=0.0)] | None = None


class CapacityDerivation(BaseModel):
    """The derived capacity with everything needed to recompute it."""

    model_config = _STRICT

    inputs: CapacityInputs
    rate_bounds_per_s: dict[str, float]
    binding_bound: str
    service_rate_per_s: float
    capacity: int


def derive_capacity(inputs: CapacityInputs) -> CapacityDerivation:
    bounds: dict[str, float] = {}
    if inputs.rpm is not None:
        bounds["rpm"] = inputs.rpm / 60.0
    if inputs.tpm is not None and inputs.tokens_per_answer is not None:
        bounds["tpm"] = inputs.tpm / (60.0 * inputs.tokens_per_answer)
    if inputs.service_slots is not None and inputs.service_time_s is not None:
        bounds["service_time"] = inputs.service_slots / inputs.service_time_s
    if not bounds:
        raise ValueError(
            "cannot derive a service rate: give rpm, or tpm with tokens_per_answer, or "
            "service_slots with service_time_s"
        )
    binding = min(bounds, key=lambda name: (bounds[name], name))
    rate = bounds[binding]
    return CapacityDerivation(
        inputs=inputs, rate_bounds_per_s={k: round(v, 6) for k, v in sorted(bounds.items())},
        binding_bound=binding, service_rate_per_s=round(rate, 6),
        capacity=max(1, math.floor(rate * inputs.patience_quantile_s)),
    )


class AdmissionRejectedError(RelayError):
    """The request was shed or dropped before any work was done for it."""

    def __init__(self, error_code: ErrorCode, message: str, *, queue_depth_at_entry: int,
                 waited_ms: float = 0.0) -> None:
        super().__init__(message)
        self.error_code: ErrorCode = error_code
        self.queue_depth_at_entry = queue_depth_at_entry
        self.waited_ms = waited_ms


class Ticket(BaseModel):
    model_config = _STRICT

    queue_depth_at_entry: int
    waited_ms: float


class AdmissionQueue:
    def __init__(self, *, capacity: int, service_rate_per_s: float, max_active: int = 1,
                 clock: MonotonicClock = time.perf_counter_ns) -> None:
        if capacity < 1 or max_active < 1 or service_rate_per_s <= 0:
            raise ValueError("capacity, max_active and service_rate_per_s must be positive")
        self.capacity = capacity
        self.service_rate_per_s = service_rate_per_s
        self.max_active = max_active
        self._clock = clock
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self.peak_depth = 0

    @classmethod
    def from_derivation(cls, derivation: CapacityDerivation, *, max_active: int = 1,
                        clock: MonotonicClock = time.perf_counter_ns) -> AdmissionQueue:
        return cls(capacity=derivation.capacity,
                   service_rate_per_s=derivation.service_rate_per_s, max_active=max_active,
                   clock=clock)

    @property
    def depth(self) -> int:
        """Requests waiting for a slot (not counting those being served)."""
        return len(self._waiters)

    @property
    def active(self) -> int:
        return self._active

    def _release(self) -> None:
        self._active -= 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():  # hand the slot straight to the next live waiter
                self._active += 1
                waiter.set_result(None)
                return

    @asynccontextmanager
    async def admit(self, *, deadline_ns: int | None = None,
                    is_connected: Callable[[], bool] | None = None) -> AsyncGenerator[Ticket]:
        """Hold a service slot for the ``async with`` body, or raise to shed the request."""
        entered = self._clock()
        depth_at_entry = self.depth
        if self._active < self.max_active and not self._waiters:
            self._active += 1
        else:
            if depth_at_entry >= self.capacity:
                raise AdmissionRejectedError(
                    "queue_full", f"queue full ({depth_at_entry}/{self.capacity})",
                    queue_depth_at_entry=depth_at_entry)
            predicted_s = (depth_at_entry + 1) / self.service_rate_per_s
            if deadline_ns is not None and entered + predicted_s * _NS > deadline_ns:
                raise AdmissionRejectedError(
                    "predicted_wait_exceeds_deadline",
                    f"predicted wait {predicted_s:.2f}s exceeds the remaining deadline",
                    queue_depth_at_entry=depth_at_entry)
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            self.peak_depth = max(self.peak_depth, self.depth)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter.done() and not waiter.cancelled():
                    self._release()  # a slot was handed over just as the client left
                else:
                    waiter.cancel()
                    if waiter in self._waiters:  # leave no ghost behind in the queue depth
                        self._waiters.remove(waiter)
                raise
        try:
            waited_ms = ns_to_ms(self._clock() - entered)
            if deadline_ns is not None and self._clock() >= deadline_ns:
                raise AdmissionRejectedError(
                    "deadline_expired", "deadline passed while queued",
                    queue_depth_at_entry=depth_at_entry, waited_ms=waited_ms)
            if is_connected is not None and not is_connected():
                raise AdmissionRejectedError(
                    "client_gave_up", "client disconnected while queued",
                    queue_depth_at_entry=depth_at_entry, waited_ms=waited_ms)
            yield Ticket(queue_depth_at_entry=depth_at_entry, waited_ms=waited_ms)
        finally:
            self._release()


__all__ = [
    "AdmissionQueue",
    "AdmissionRejectedError",
    "CapacityDerivation",
    "CapacityInputs",
    "Ticket",
    "derive_capacity",
]
