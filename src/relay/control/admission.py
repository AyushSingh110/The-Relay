"""Admission queue (application layer; ARCHITECTURE.md sections 9.2 and 15; V2-10 fairness,
ARCHITECTURE_V2 §7).

v1's queue was first come, first served. V2-10 adds, without changing v1's behaviour (a
request with no tenant and the default priority is ordered exactly as before):

- **A token bucket per tenant.** A request that finds its tenant's bucket empty is
  "over rate". It is not refused: it waits behind every request that is within its
  tenant's rate, so one tenant cannot take the whole service, yet a tenant alone still
  gets all of it (the queue never idles while work waits).
- **Two priority classes.** ``interactive`` (the UI, /v1/ask by default) goes before
  ``batch``. The rank of a waiting request is: interactive within rate (0), batch within
  rate (1), interactive over rate (2), batch over rate (3).
- **Fair order between tenants.** Within a rank, the tenant admitted least recently goes
  first (round robin), then arrival order.
- **Shedding the lowest rank first.** When the queue is full, an arriving request takes
  the place of the newest waiting request of a worse rank (that one is shed with
  ``queue_full``, reason ``shed_for_priority``); if none is worse, the arrival is shed.
- **Work-conserving (F-02c, owner 2026-11-22).** Being over rate ranks a request lower only
  against *other* tenants' requests: it counts while another tenant has a request waiting,
  and from a tenant's first over-rate request on it covers the rest of that tenant's
  backlog at that priority. So within one tenant and priority the order is arrival order, a
  request never evicts a waiting one of its own tenant and priority, and a lone tenant gets a
  plain FIFO queue. Priority (interactive before batch) counts as before.
- **Capacity from measured tokens per answer.** ``observe_tokens`` keeps an exponentially
  weighted moving average (EWMA) of the tokens each answer used and re-derives the
  capacity from it (v1 §15), when the queue was built from a token-bound derivation.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from relay.errors import RelayError
from relay.schemas import ErrorCode
from relay.telemetry.spans import MonotonicClock, ns_to_ms

_NS: Final = 1_000_000_000
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")
DEFAULT_TENANT_BURST: Final = 4.0
DEFAULT_EWMA_ALPHA: Final = 0.2


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


class Priority(StrEnum):
    INTERACTIVE = "interactive"
    BATCH = "batch"


class AdmissionRejectedError(RelayError):
    """The request was shed or dropped before any work was done for it."""

    def __init__(self, error_code: ErrorCode, message: str, *, queue_depth_at_entry: int,
                 waited_ms: float = 0.0, reason: str | None = None) -> None:
        super().__init__(message)
        self.error_code: ErrorCode = error_code
        self.queue_depth_at_entry = queue_depth_at_entry
        self.waited_ms = waited_ms
        # V2-10: why it was shed, beyond the code ("shed_for_priority", "queue_full")
        self.reason = reason


class Ticket(BaseModel):
    model_config = _STRICT

    queue_depth_at_entry: int
    waited_ms: float
    over_rate: bool = False  # V2-10: its tenant's bucket was empty when it arrived


@dataclass
class TenantBucket:
    """``rate_per_s`` requests per second, up to ``burst`` at once."""

    rate_per_s: float
    burst: float
    tokens: float
    updated_ns: int

    def take(self, now: int) -> bool:
        elapsed = max(0, now - self.updated_ns) / _NS
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate_per_s)
        self.updated_ns = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


@dataclass(eq=False)
class _Waiter:
    future: asyncio.Future[None]
    tenant: str | None
    rank: int
    seq: int
    over_rate: bool = False
    priority: str = Priority.INTERACTIVE


def _rank(priority: Priority, over_rate: bool) -> int:
    return (2 if over_rate else 0) + (1 if priority == Priority.BATCH else 0)


@dataclass
class _Fairness:
    """Per-tenant state: buckets and when each tenant was last admitted."""

    rate_per_s: float | None
    burst: float
    buckets: dict[str, TenantBucket] = field(default_factory=dict)
    last_admitted: dict[str | None, int] = field(default_factory=dict)

    def in_rate(self, tenant: str | None, now: int) -> bool:
        if tenant is None or self.rate_per_s is None:
            return True  # v1 /ask, or fairness off: no bucket
        bucket = self.buckets.get(tenant)
        if bucket is None:
            bucket = self.buckets[tenant] = TenantBucket(self.rate_per_s, self.burst,
                                                         self.burst, now)
        return bucket.take(now)


class AdmissionQueue:
    def __init__(self, *, capacity: int, service_rate_per_s: float, max_active: int = 1,
                 clock: MonotonicClock = time.perf_counter_ns,
                 tenant_rate_per_s: float | None = None,
                 tenant_burst: float = DEFAULT_TENANT_BURST,
                 adaptive: CapacityInputs | None = None,
                 ewma_alpha: float = DEFAULT_EWMA_ALPHA) -> None:
        if capacity < 1 or max_active < 1 or service_rate_per_s <= 0:
            raise ValueError("capacity, max_active and service_rate_per_s must be positive")
        if tenant_rate_per_s is not None and tenant_rate_per_s <= 0:
            raise ValueError("tenant_rate_per_s must be positive")
        if tenant_burst < 1 or not 0 < ewma_alpha <= 1:
            raise ValueError("tenant_burst must be >= 1 and ewma_alpha in (0, 1]")
        self.capacity = capacity
        self.service_rate_per_s = service_rate_per_s
        self.max_active = max_active
        self._clock = clock
        self._active = 0
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self._fair = _Fairness(tenant_rate_per_s, tenant_burst)
        self.peak_depth = 0
        self.shed_for_priority = 0
        # V2-10: re-derive the capacity from an EWMA of measured tokens per answer
        self._inputs = adaptive if adaptive is not None and adaptive.tpm is not None \
            and adaptive.tokens_per_answer is not None else None
        self._alpha = ewma_alpha
        self.tokens_per_answer: float | None = (None if self._inputs is None
                                                else self._inputs.tokens_per_answer)
        self.observed_answers = 0

    @classmethod
    def from_derivation(cls, derivation: CapacityDerivation, *, max_active: int = 1,
                        clock: MonotonicClock = time.perf_counter_ns,
                        tenant_rate_per_s: float | None = None,
                        tenant_burst: float = DEFAULT_TENANT_BURST,
                        adaptive: bool = False,
                        ewma_alpha: float = DEFAULT_EWMA_ALPHA) -> AdmissionQueue:
        return cls(capacity=derivation.capacity,
                   service_rate_per_s=derivation.service_rate_per_s, max_active=max_active,
                   clock=clock, tenant_rate_per_s=tenant_rate_per_s,
                   tenant_burst=tenant_burst,
                   adaptive=derivation.inputs if adaptive else None, ewma_alpha=ewma_alpha)

    @property
    def depth(self) -> int:
        """Requests waiting for a slot (not counting those being served)."""
        return len(self._waiters)

    @property
    def active(self) -> int:
        return self._active

    def depth_by_class(self) -> dict[tuple[str, str], int]:
        """Waiting requests by (priority, tenant) (V2-11: the queue-depth gauge)."""
        counts: dict[tuple[str, str], int] = {}
        for waiter in self._waiters:
            if not waiter.future.done():
                key = (str(waiter.priority), waiter.tenant or "none")
                counts[key] = counts.get(key, 0) + 1
        return counts

    # -- ordering --

    def _ranks(self, extra: _Waiter | None = None,
               without: _Waiter | None = None) -> dict[_Waiter, int]:
        """Each live waiter's rank (and ``extra``'s, an arrival not queued yet; ``without``:
        a waiter about to be shed, left out). Priority always counts; being over rate counts
        only when another tenant is waiting too, and it then covers a tenant's backlog (per
        priority) from its first over-rate request on, so one tenant's requests keep their
        arrival order."""
        live = [w for w in self._waiters if not w.future.done() and w is not without]
        if extra is not None:
            live.append(extra)
        contended = len({w.tenant for w in live}) > 1
        over: dict[tuple[str | None, str], bool] = {}
        ranks: dict[_Waiter, int] = {}
        for waiter in sorted(live, key=lambda w: w.seq):
            group = (waiter.tenant, waiter.priority)
            over[group] = over.get(group, False) or waiter.over_rate
            ranks[waiter] = _rank(Priority(waiter.priority), contended and over[group])
        return ranks

    def _key(self, waiter: _Waiter, ranks: dict[_Waiter, int]) -> tuple[int, int, int]:
        return (ranks.get(waiter, waiter.rank), self._fair.last_admitted.get(waiter.tenant, -1),
                waiter.seq)

    def _ordered(self) -> list[_Waiter]:
        ranks = self._ranks()
        return sorted(self._waiters, key=lambda w: self._key(w, ranks))

    def _position(self, waiter: _Waiter) -> int:
        if waiter not in self._waiters:
            return 0  # admitted, or gone
        return self._ordered().index(waiter) + 1

    def _admitted(self, tenant: str | None) -> None:
        self._fair.last_admitted[tenant] = next(self._seq)

    def _release(self) -> None:
        self._active -= 1
        for waiter in self._ordered():
            self._waiters.remove(waiter)
            if not waiter.future.done():  # hand the slot straight to the next live waiter
                self._active += 1
                self._admitted(waiter.tenant)
                waiter.future.set_result(None)
                return

    def _evictable(self, arrival: _Waiter) -> tuple[list[_Waiter], dict[_Waiter, int]]:
        """Waiting requests ``arrival`` may take the place of: ranked worse than it, and never
        one of its own tenant at its own priority (F-02c)."""
        ranks = self._ranks(arrival)
        mine = ranks[arrival]
        return [w for w in self._waiters if not w.future.done() and ranks[w] > mine
                and not (w.tenant == arrival.tenant and w.priority == arrival.priority)], ranks

    def _victim(self, arrival: _Waiter) -> _Waiter | None:
        """The queue is full: the newest waiting request of the worst rank among those
        ``arrival`` may evict, or None (the arrival itself is shed)."""
        candidates, ranks = self._evictable(arrival)
        return max(candidates, key=lambda w: (ranks[w], w.seq)) if candidates else None

    def _shed(self, victim: _Waiter, depth_at_entry: int) -> None:
        self._waiters.remove(victim)
        self.shed_for_priority += 1
        victim.future.set_exception(AdmissionRejectedError(
            "queue_full", "the queue is full: this lower-priority request was shed for a "
            "higher-priority one", queue_depth_at_entry=depth_at_entry,
            reason="shed_for_priority"))

    # -- capacity --

    def observe_tokens(self, tokens: int) -> None:
        """One answer's measured tokens (prompt + completion, as the provider reported).
        Re-derives capacity and service rate when the queue is token-bound."""
        if self._inputs is None or tokens <= 0:
            return
        previous = self.tokens_per_answer or float(tokens)
        self.tokens_per_answer = self._alpha * tokens + (1 - self._alpha) * previous
        self.observed_answers += 1
        derived = derive_capacity(self._inputs.model_copy(
            update={"tokens_per_answer": self.tokens_per_answer}))
        self.capacity = derived.capacity
        self.service_rate_per_s = derived.service_rate_per_s

    # -- admission --

    @asynccontextmanager
    async def admit(self, *, deadline_ns: int | None = None,
                    is_connected: Callable[[], bool] | None = None,
                    on_queued: Callable[[Callable[[], int]], None] | None = None,
                    tenant: str | None = None,
                    priority: Priority | str = Priority.INTERACTIVE,
                    ) -> AsyncGenerator[Ticket]:
        """Hold a service slot for the ``async with`` body, or raise to shed the request.

        ``on_queued`` (V2-05) is called once if the request has to wait, with a function that
        returns its live 1-based position in the queue (0 once it is no longer waiting).
        ``tenant`` and ``priority`` (V2-10) order it against other tenants' requests."""
        entered = self._clock()
        depth_at_entry = self.depth
        over_rate = not self._fair.in_rate(tenant, entered)
        rank = _rank(Priority(priority), over_rate)
        if self._active < self.max_active and not self._waiters:
            self._active += 1
            self._admitted(tenant)
        else:
            waiter = _Waiter(asyncio.get_running_loop().create_future(), tenant, rank,
                             next(self._seq), over_rate, str(Priority(priority)))
            full = depth_at_entry >= self.capacity
            victim = self._victim(waiter) if full else None
            if full and victim is None:  # v1's order: "full" before deadline
                raise AdmissionRejectedError(
                    "queue_full", f"queue full ({depth_at_entry}/{self.capacity})",
                    queue_depth_at_entry=depth_at_entry, reason="queue_full")
            # the position the arrival will have once ``victim`` (if any) is shed
            ranks = self._ranks(waiter, without=victim)
            mine = self._key(waiter, ranks)
            ahead = sum(1 for w in self._waiters
                        if w is not victim and w in ranks and self._key(w, ranks) < mine)
            predicted_s = (ahead + 1) / self.service_rate_per_s
            if deadline_ns is not None and entered + predicted_s * _NS > deadline_ns:
                raise AdmissionRejectedError(
                    "predicted_wait_exceeds_deadline",
                    f"predicted wait {predicted_s:.2f}s exceeds the remaining deadline",
                    queue_depth_at_entry=depth_at_entry)
            if victim is not None:
                self._shed(victim, depth_at_entry)
            self._waiters.append(waiter)
            self.peak_depth = max(self.peak_depth, self.depth)
            if on_queued is not None:
                try:
                    on_queued(lambda: self._position(waiter))
                except BaseException:
                    self._waiters.remove(waiter)  # leave no ghost behind
                    raise
            try:
                await waiter.future
            except asyncio.CancelledError:
                if waiter.future.done() and not waiter.future.cancelled() \
                        and waiter.future.exception() is None:
                    self._release()  # a slot was handed over just as the client left
                else:
                    waiter.future.cancel()
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
            yield Ticket(queue_depth_at_entry=depth_at_entry, waited_ms=waited_ms,
                         over_rate=over_rate)
        finally:
            self._release()


__all__ = [
    "AdmissionQueue",
    "AdmissionRejectedError",
    "CapacityDerivation",
    "CapacityInputs",
    "Priority",
    "TenantBucket",
    "Ticket",
    "derive_capacity",
]
