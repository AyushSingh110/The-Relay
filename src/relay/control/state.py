"""Shared state for multi-worker deployments (ARCHITECTURE_V2.md D7 and section 13; V2-13).

A ``StateStore`` holds what several workers must agree on:

- the exact answer cache;
- SingleFlight coordination: a lock with a TTL names the one leader of a question, and
  the leader's events reach followers on other workers;
- the provider quota counters: the sliding minute window (requests and tokens) and the
  daily counters of each lane.

``backend = "memory"`` (the default) needs no Redis: the service keeps its in-process
objects exactly as before (the cache's dict, the flight channel, the guard's windows).
``InMemoryStateStore`` is the protocol's reference implementation: the contract tests run
against it and against ``RedisStateStore``, and it lets two in-process workers share state.

Events of a flight are kept in a per-flight log and announced on Pub/Sub. A follower reads
the log, and each Pub/Sub message only wakes it to read the log again: Pub/Sub alone drops
whatever was published before the follower subscribed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from relay.control.cache import CachedAnswer

logger = logging.getLogger(__name__)

DEFAULT_PREFIX: Final = "relay"
DEFAULT_CACHE_TTL_S: Final = 7 * 24 * 3600.0
DEFAULT_FLIGHT_TTL_S: Final = 30.0
FLIGHT_LOG_GRACE_S: Final = 60.0  # a finished flight's log outlives its lock this long
QUOTA_WINDOW_S: Final = 60
DAY_KEY_TTL_S: Final = 2 * 24 * 3600
MAX_MEMORY_CACHE_ENTRIES: Final = 4096

FlightEvent = dict[str, Any]  # {"seq": int, "name": str, "data": dict}
QuotaRefusal = Literal["daily_budget_exhausted", "payload_too_large"]


class StateUnavailableError(RuntimeError):
    """The shared state backend could not be reached or answered unexpectedly."""


@dataclass(frozen=True)
class FlightLease:
    """The result of claiming a flight: ``owned`` (this worker leads) and the token of the
    leader that holds it. The token names the leader's event log and channel."""

    owned: bool
    token: str


@dataclass(frozen=True)
class QuotaLimits:
    """One lane's limits as the shared counters apply them. ``None``: no limit known.
    ``rpd_stop`` and ``tpd_stop`` are the daily safety stops (fraction x daily limit)."""

    rpm: int | None = None
    tpm: int | None = None
    rpd_stop: float | None = None
    tpd_stop: float | None = None


@dataclass(frozen=True)
class QuotaDecision:
    """``reservation`` when the tokens were reserved; otherwise ``wait_ms`` until they
    could be, or the ``refusal`` that no wait can fix."""

    reservation: str | None
    wait_ms: int = 0
    refusal: QuotaRefusal | None = None


@runtime_checkable
class StateStore(Protocol):
    """What the cache, SingleFlight and the provider guards share across workers."""

    name: str

    # cache
    async def get_cached_answer(self, key: str) -> CachedAnswer | None: ...
    async def set_cached_answer(self, key: str, answer: CachedAnswer, ttl_s: float) -> None: ...
    async def purge_cached(self, collection_id: str) -> int: ...

    # SingleFlight
    async def acquire_flight_lock(self, flight_key: str, ttl_ms: int) -> FlightLease: ...
    async def refresh_flight_lock(self, flight_key: str, token: str, ttl_ms: int) -> bool: ...
    async def release_flight_lock(self, flight_key: str, token: str) -> None: ...
    async def flight_holder(self, flight_key: str) -> str | None: ...
    async def publish_flight_event(self, flight_key: str, token: str,
                                   event: FlightEvent) -> None: ...
    def subscribe_flight(self, flight_key: str, token: str, *,
                         poll_s: float) -> AsyncIterator[FlightEvent | None]: ...
    async def flight_followers(self, flight_key: str, token: str) -> int: ...

    # quota
    async def reserve_tokens(self, lane: str, tokens: int, *, limits: QuotaLimits, day: date,
                             window_s: int = QUOTA_WINDOW_S) -> QuotaDecision: ...
    async def settle_tokens(self, lane: str, reservation: str, reserved: int, actual: int, *,
                            day: date) -> None: ...
    async def cancel_reservation(self, lane: str, reservation: str, reserved: int, *,
                                 day: date) -> None: ...

    async def ping(self) -> None: ...
    async def aclose(self) -> None: ...


def collection_marker(collection_id: str) -> str:
    """The part of a cache key that names its collection (``setting@<id>:v<version>``)."""
    return f"@{collection_id}:"


# --- In-memory ---------------------------------------------------------------------------


@dataclass
class _Lock:
    token: str
    expires_at: float


@dataclass
class _Log:
    events: list[FlightEvent] = field(default_factory=list)
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: int = 0
    expires_at: float | None = None  # set when the lock is released

    def wake(self) -> None:
        signal, self.signal = self.signal, asyncio.Event()
        signal.set()


@dataclass
class _Window:
    entries: deque[tuple[float, str]] = field(default_factory=deque)  # (at_s, reservation)
    amounts: dict[str, int] = field(default_factory=dict)


class InMemoryStateStore:
    """One process's shared state (one event loop): every method runs without awaiting
    inside its critical section, so it is atomic for the loop's other tasks."""

    name = "memory"

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 max_cache_entries: int = MAX_MEMORY_CACHE_ENTRIES) -> None:
        self._clock = clock
        self._max_cache_entries = max_cache_entries
        self._cache: OrderedDict[str, tuple[CachedAnswer, float]] = OrderedDict()
        self._locks: dict[str, _Lock] = {}
        self._logs: dict[tuple[str, str], _Log] = {}
        self._windows: dict[str, _Window] = {}
        self._days: dict[tuple[str, str], list[int]] = {}  # (lane, day) -> [requests, tokens]

    # cache

    async def get_cached_answer(self, key: str) -> CachedAnswer | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry[1] <= self._clock():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return entry[0]

    async def set_cached_answer(self, key: str, answer: CachedAnswer, ttl_s: float) -> None:
        self._cache[key] = (answer, self._clock() + ttl_s)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)

    async def purge_cached(self, collection_id: str) -> int:
        marker = collection_marker(collection_id)
        doomed = [key for key in self._cache if marker in key]
        for key in doomed:
            del self._cache[key]
        return len(doomed)

    # SingleFlight

    def _live_lock(self, flight_key: str) -> _Lock | None:
        lock = self._locks.get(flight_key)
        if lock is not None and lock.expires_at <= self._clock():
            del self._locks[flight_key]
            self._expire_log(flight_key, lock.token)
            return None
        return lock

    def _expire_log(self, flight_key: str, token: str) -> None:
        log = self._logs.get((flight_key, token))
        if log is not None and log.expires_at is None:
            log.expires_at = self._clock() + FLIGHT_LOG_GRACE_S

    def _prune_logs(self) -> None:
        now = self._clock()
        for key in [k for k, log in self._logs.items()
                    if log.expires_at is not None and log.expires_at <= now
                    and log.subscribers == 0]:
            del self._logs[key]

    async def acquire_flight_lock(self, flight_key: str, ttl_ms: int) -> FlightLease:
        self._prune_logs()
        lock = self._live_lock(flight_key)
        if lock is not None:
            return FlightLease(False, lock.token)
        token = uuid.uuid4().hex
        self._locks[flight_key] = _Lock(token, self._clock() + ttl_ms / 1000)
        self._logs[(flight_key, token)] = _Log()
        return FlightLease(True, token)

    async def refresh_flight_lock(self, flight_key: str, token: str, ttl_ms: int) -> bool:
        lock = self._live_lock(flight_key)
        if lock is None or lock.token != token:
            return False
        lock.expires_at = self._clock() + ttl_ms / 1000
        return True

    async def release_flight_lock(self, flight_key: str, token: str) -> None:
        lock = self._live_lock(flight_key)
        if lock is not None and lock.token == token:
            del self._locks[flight_key]
        self._expire_log(flight_key, token)

    async def flight_holder(self, flight_key: str) -> str | None:
        lock = self._live_lock(flight_key)
        return None if lock is None else lock.token

    async def publish_flight_event(self, flight_key: str, token: str,
                                   event: FlightEvent) -> None:
        log = self._logs.setdefault((flight_key, token), _Log())
        log.events.append(json.loads(json.dumps(event)))  # what Redis would carry
        log.wake()

    async def subscribe_flight(self, flight_key: str, token: str, *,
                               poll_s: float) -> AsyncIterator[FlightEvent | None]:
        log = self._logs.setdefault((flight_key, token), _Log())
        log.subscribers += 1
        seen = 0
        try:
            while True:
                while seen < len(log.events):
                    seen += 1
                    yield log.events[seen - 1]
                signal = log.signal
                try:
                    await asyncio.wait_for(signal.wait(), poll_s)
                except TimeoutError:
                    yield None
        finally:
            log.subscribers -= 1

    async def flight_followers(self, flight_key: str, token: str) -> int:
        log = self._logs.get((flight_key, token))
        return 0 if log is None else log.subscribers

    # quota

    async def reserve_tokens(self, lane: str, tokens: int, *, limits: QuotaLimits, day: date,
                             window_s: int = QUOTA_WINDOW_S) -> QuotaDecision:
        if limits.tpm is not None and tokens > limits.tpm:
            return QuotaDecision(None, refusal="payload_too_large")
        now = self._clock()
        window = self._windows.setdefault(lane, _Window())
        while window.entries and window.entries[0][0] <= now - window_s:
            _, old = window.entries.popleft()
            window.amounts.pop(old, None)
        counters = self._days.setdefault((lane, day.isoformat()), [0, 0])
        if ((limits.rpd_stop is not None and counters[0] >= limits.rpd_stop)
                or (limits.tpd_stop is not None and counters[1] >= limits.tpd_stop)):
            return QuotaDecision(None, refusal="daily_budget_exhausted")
        wait_s = 0.0
        entries = list(window.entries)
        if limits.rpm is not None and len(entries) >= limits.rpm:
            wait_s = max(wait_s, entries[len(entries) - limits.rpm][0] + window_s - now)
        if limits.tpm is not None:
            needed = tokens - (limits.tpm - sum(window.amounts.values()))
            for at, reservation in entries:
                if needed <= 0:
                    break
                needed -= window.amounts.get(reservation, 0)
                wait_s = max(wait_s, at + window_s - now)
        if wait_s > 0:
            return QuotaDecision(None, wait_ms=max(1, math.ceil(wait_s * 1000)))
        reservation = uuid.uuid4().hex
        window.entries.append((now, reservation))
        window.amounts[reservation] = tokens
        counters[0] += 1
        counters[1] += tokens
        return QuotaDecision(reservation)

    async def settle_tokens(self, lane: str, reservation: str, reserved: int, actual: int, *,
                            day: date) -> None:
        window = self._windows.get(lane)
        if window is not None and reservation in window.amounts:
            window.amounts[reservation] = actual
        counters = self._days.get((lane, day.isoformat()))
        if counters is not None:
            counters[1] += actual - reserved

    async def cancel_reservation(self, lane: str, reservation: str, reserved: int, *,
                                 day: date) -> None:
        window = self._windows.get(lane)
        if window is None or window.amounts.pop(reservation, None) is None:
            return  # already gone from the window (expired): its day counts stay
        window.entries = deque(e for e in window.entries if e[1] != reservation)
        counters = self._days.get((lane, day.isoformat()))
        if counters is not None:
            counters[0] -= 1
            counters[1] -= reserved

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


# --- Redis -------------------------------------------------------------------------------

# Sliding-window reservation, atomic on the server. KEYS: requests zset (member ->
# reserved-at ms), amounts hash (member -> tokens), day requests, day tokens.
# ARGV: window ms, tokens, rpm, tpm, rpd stop, tpd stop (0 = no limit), reservation id,
# day-key TTL s. Returns {status, wait_ms}: 1 reserved, 0 wait, -1 daily stop, -2 too large.
_RESERVE: Final = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = tonumber(ARGV[1])
local tokens = tonumber(ARGV[2])
local rpm = tonumber(ARGV[3])
local tpm = tonumber(ARGV[4])
local rpd_stop = tonumber(ARGV[5])
local tpd_stop = tonumber(ARGV[6])
if tpm > 0 and tokens > tpm then return {-2, 0} end
local old = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now - window)
if #old > 0 then
  redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
  for i = 1, #old, 500 do  -- unpack has a stack limit: delete in batches
    redis.call('HDEL', KEYS[2], unpack(old, i, math.min(i + 499, #old)))
  end
end
local day_requests = tonumber(redis.call('GET', KEYS[3]) or '0')
local day_tokens = tonumber(redis.call('GET', KEYS[4]) or '0')
if (rpd_stop > 0 and day_requests >= rpd_stop) or (tpd_stop > 0 and day_tokens >= tpd_stop) then
  return {-1, 0}
end
local live = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local count = #live / 2
local wait = 0
if rpm > 0 and count >= rpm then
  wait = math.max(wait, tonumber(live[(count - rpm) * 2 + 2]) + window - now)
end
if tpm > 0 then
  local amounts = {}
  local used = 0
  for i = 1, #live, 2 do
    local amount = tonumber(redis.call('HGET', KEYS[2], live[i]) or '0')
    amounts[#amounts + 1] = amount
    used = used + amount
  end
  local needed = tokens - (tpm - used)
  local j = 1
  for i = 1, #live, 2 do
    if needed <= 0 then break end
    needed = needed - amounts[j]
    wait = math.max(wait, tonumber(live[i + 1]) + window - now)
    j = j + 1
  end
end
if wait > 0 then return {0, math.max(1, math.ceil(wait))} end
redis.call('ZADD', KEYS[1], now, ARGV[7])
redis.call('HSET', KEYS[2], ARGV[7], tokens)
redis.call('PEXPIRE', KEYS[1], window * 2)
redis.call('PEXPIRE', KEYS[2], window * 2)
redis.call('INCR', KEYS[3])
redis.call('INCRBY', KEYS[4], tokens)
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[8]))
redis.call('EXPIRE', KEYS[4], tonumber(ARGV[8]))
return {1, 0}
"""

# KEYS: amounts hash, day tokens. ARGV: reservation, actual, actual - reserved.
_SETTLE: Final = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('INCRBY', KEYS[2], tonumber(ARGV[3]))
end
return 1
"""

# KEYS: requests zset, amounts hash, day requests, day tokens. ARGV: reservation, reserved.
# A reservation whose call never happened: its minute slot and daily counts are given back.
_CANCEL: Final = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 1 then
  redis.call('HDEL', KEYS[2], ARGV[1])
  if redis.call('EXISTS', KEYS[3]) == 1 then redis.call('DECR', KEYS[3]) end
  if redis.call('EXISTS', KEYS[4]) == 1 then redis.call('DECRBY', KEYS[4], tonumber(ARGV[2])) end
end
return 1
"""

# KEYS: lock. ARGV: token, ttl ms. Only the holder extends its lock.
_REFRESH: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# KEYS: lock, event log. ARGV: token, log grace ms. Only the holder deletes its lock; the
# log stays readable a little longer for a follower that subscribed late.
_RELEASE: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
redis.call('PEXPIRE', KEYS[2], ARGV[2])
return 1
"""

# KEYS: event log. ARGV: channel, payload, log TTL ms. One step: append, then announce.
_PUBLISH: Final = """
redis.call('RPUSH', KEYS[1], ARGV[2])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
redis.call('PUBLISH', ARGV[1], ARGV[2])
return 1
"""

_GLOB_SPECIAL: Final = str.maketrans({c: f"\\{c}" for c in "*?[]\\"})


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class RedisStateStore:
    """The shared state of several workers, in one Redis database."""

    name = "redis"

    def __init__(self, client: Any, *, prefix: str = DEFAULT_PREFIX,
                 log_ttl_s: float = DEFAULT_FLIGHT_TTL_S * 10) -> None:
        self._client = client
        self._prefix = prefix
        self._log_ttl_ms = int(log_ttl_s * 1000)
        self._reserve = client.register_script(_RESERVE)
        self._settle = client.register_script(_SETTLE)
        self._cancel = client.register_script(_CANCEL)
        self._refresh = client.register_script(_REFRESH)
        self._release = client.register_script(_RELEASE)
        self._publish = client.register_script(_PUBLISH)

    @classmethod
    def from_url(cls, url: str, *, socket_timeout_s: float = 5.0,
                 **kwargs: Any) -> RedisStateStore:
        """A store over ``redis.asyncio`` (imported here: Redis is an optional extra). The
        socket timeouts bound every call: a partitioned Redis fails a call, never hangs it."""
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise StateUnavailableError("the redis state backend needs the 'redis' package: "
                                        "pip install -e \".[redis]\"") from exc
        return cls(redis_asyncio.Redis.from_url(
            url, socket_timeout=socket_timeout_s, socket_connect_timeout=socket_timeout_s,
            health_check_interval=30), **kwargs)

    # keys

    def _cache_key(self, key: str) -> str:
        return f"{self._prefix}:cache:{key}"

    def _lock_key(self, flight_key: str) -> str:
        return f"{self._prefix}:flight:{flight_key}"

    def _channel(self, flight_key: str, token: str) -> str:
        return f"{self._prefix}:flight:events:{flight_key}:{token}"

    def _log_key(self, flight_key: str, token: str) -> str:
        return f"{self._prefix}:flight:log:{flight_key}:{token}"

    def _quota_keys(self, lane: str, day: date) -> list[str]:
        base = f"{self._prefix}:quota:{lane}"
        stamp = day.isoformat()
        return [f"{base}:requests", f"{base}:tokens", f"{base}:day:{stamp}:requests",
                f"{base}:day:{stamp}:tokens"]

    # cache

    async def get_cached_answer(self, key: str) -> CachedAnswer | None:
        from relay.control.cache import CachedAnswer

        raw = await self._client.get(self._cache_key(key))
        return None if raw is None else CachedAnswer.model_validate_json(raw)

    async def set_cached_answer(self, key: str, answer: CachedAnswer, ttl_s: float) -> None:
        await self._client.set(self._cache_key(key), answer.model_dump_json(),
                               px=max(1, int(ttl_s * 1000)))

    async def purge_cached(self, collection_id: str) -> int:
        marker = collection_marker(collection_id).translate(_GLOB_SPECIAL)
        doomed = [key async for key in self._client.scan_iter(
            match=f"{self._prefix}:cache:*{marker}*")]
        if not doomed:
            return 0
        return int(await self._client.delete(*doomed))

    # SingleFlight

    async def acquire_flight_lock(self, flight_key: str, ttl_ms: int) -> FlightLease:
        lock = self._lock_key(flight_key)
        for _ in range(3):  # the holder's lock can expire between SET NX and GET
            token = uuid.uuid4().hex
            if await self._client.set(lock, token, nx=True, px=ttl_ms):
                return FlightLease(True, token)
            holder = await self._client.get(lock)
            if holder is not None:
                return FlightLease(False, _text(holder))
        raise StateUnavailableError("flight lock changed hands three times in a row")

    async def refresh_flight_lock(self, flight_key: str, token: str, ttl_ms: int) -> bool:
        return bool(await self._refresh(keys=[self._lock_key(flight_key)], args=[token, ttl_ms]))

    async def release_flight_lock(self, flight_key: str, token: str) -> None:
        await self._release(keys=[self._lock_key(flight_key), self._log_key(flight_key, token)],
                            args=[token, int(FLIGHT_LOG_GRACE_S * 1000)])

    async def flight_holder(self, flight_key: str) -> str | None:
        holder = await self._client.get(self._lock_key(flight_key))
        return None if holder is None else _text(holder)

    async def publish_flight_event(self, flight_key: str, token: str,
                                   event: FlightEvent) -> None:
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        await self._publish(keys=[self._log_key(flight_key, token)],
                            args=[self._channel(flight_key, token), payload, self._log_ttl_ms])

    async def subscribe_flight(self, flight_key: str, token: str, *,
                               poll_s: float) -> AsyncIterator[FlightEvent | None]:
        log = self._log_key(flight_key, token)
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self._channel(flight_key, token))
        seen = 0
        try:
            while True:
                # subscribed first, then read the log: nothing published in between is lost
                for raw in await self._client.lrange(log, seen, -1):
                    seen += 1
                    yield json.loads(raw)
                silent = not await self._wake(pubsub, poll_s)
                if silent and seen == await self._client.llen(log):
                    yield None  # silence: the caller checks the leader's lease
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception as exc:  # the connection may already be gone
                logger.debug("flight unsubscribe failed: %s", type(exc).__name__)

    @staticmethod
    async def _wake(pubsub: Any, timeout_s: float) -> bool:
        """Whether a message arrived within ``timeout_s`` (the subscribe confirmation, which
        ``get_message`` swallows and answers with None, does not count)."""
        deadline = time.monotonic() + timeout_s
        while (left := deadline - time.monotonic()) > 0:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=left)
            if message is not None:
                return True
        return False

    async def flight_followers(self, flight_key: str, token: str) -> int:
        counts = await self._client.pubsub_numsub(self._channel(flight_key, token))
        return sum(int(count) for _, count in counts)

    # quota

    async def reserve_tokens(self, lane: str, tokens: int, *, limits: QuotaLimits, day: date,
                             window_s: int = QUOTA_WINDOW_S) -> QuotaDecision:
        reservation = uuid.uuid4().hex
        status, wait_ms = await self._reserve(keys=self._quota_keys(lane, day), args=[
            window_s * 1000, tokens, limits.rpm or 0, limits.tpm or 0, limits.rpd_stop or 0,
            limits.tpd_stop or 0, reservation, DAY_KEY_TTL_S])
        status = int(status)
        if status == 1:
            return QuotaDecision(reservation)
        if status == 0:
            return QuotaDecision(None, wait_ms=int(wait_ms))
        return QuotaDecision(None, refusal="daily_budget_exhausted" if status == -1
                             else "payload_too_large")

    async def settle_tokens(self, lane: str, reservation: str, reserved: int, actual: int, *,
                            day: date) -> None:
        keys = self._quota_keys(lane, day)
        await self._settle(keys=[keys[1], keys[3]], args=[reservation, actual, actual - reserved])

    async def cancel_reservation(self, lane: str, reservation: str, reserved: int, *,
                                 day: date) -> None:
        await self._cancel(keys=self._quota_keys(lane, day), args=[reservation, reserved])

    async def ping(self) -> None:
        await self._client.ping()

    async def aclose(self) -> None:
        await self._client.aclose()


# --- One lane's view of the shared quota -------------------------------------------------


@dataclass
class SharedQuota:
    """The shared minute window and daily counters of one lane (``ProviderGuard.share``)."""

    store: StateStore
    lane: str
    _pending: set[asyncio.Task[None]] = field(default_factory=set)

    async def reserve(self, tokens: int, limits: QuotaLimits, day: date) -> QuotaDecision:
        """The reservation runs to completion even if the caller is cancelled meanwhile; a
        reservation granted to a caller that has gone is then given back."""
        call = asyncio.ensure_future(
            self.store.reserve_tokens(self.lane, tokens, limits=limits, day=day))
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            call.add_done_callback(lambda done: self._give_back(done, tokens, day))
            raise
        except StateUnavailableError:
            raise
        except Exception as exc:  # fail closed: the global limits cannot be checked
            raise StateUnavailableError(f"quota reservation failed: {type(exc).__name__}") \
                from exc

    def _give_back(self, done: asyncio.Future[QuotaDecision], tokens: int, day: date) -> None:
        if done.cancelled() or done.exception() is not None:
            return
        reservation = done.result().reservation
        if reservation is not None:
            self.cancel(reservation, tokens, day)

    def cancel(self, reservation: str, reserved: int, day: date) -> None:
        """Give back a reservation whose provider call never happened (background)."""
        self._background(self._cancel(reservation, reserved, day))

    async def _cancel(self, reservation: str, reserved: int, day: date) -> None:
        try:
            await self.store.cancel_reservation(self.lane, reservation, reserved, day=day)
        except Exception as exc:
            logger.warning("lane %s: unused reservation not given back (%s); it expires "
                           "with the window", self.lane, type(exc).__name__)

    def _background(self, work: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - always called on the event loop
            work.close()
            return
        task = loop.create_task(work)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def settle(self, reservation: str, reserved: int, actual: int, day: date) -> None:
        """Correct the reservation to what was spent, in the background (``settle`` is
        synchronous). If it never lands, the window keeps the larger reservation: the
        error is on the safe side."""
        self._background(self._settle(reservation, reserved, actual, day))

    async def _settle(self, reservation: str, reserved: int, actual: int, day: date) -> None:
        try:
            await self.store.settle_tokens(self.lane, reservation, reserved, actual, day=day)
        except Exception as exc:
            logger.warning("lane %s: shared quota not settled (%s); the reservation stays",
                           self.lane, type(exc).__name__)

    async def drain(self) -> None:
        """Wait for the settlements still in flight (tests and shutdown)."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)


__all__ = [
    "DEFAULT_CACHE_TTL_S",
    "DEFAULT_FLIGHT_TTL_S",
    "FlightEvent",
    "FlightLease",
    "InMemoryStateStore",
    "QuotaDecision",
    "QuotaLimits",
    "RedisStateStore",
    "SharedQuota",
    "StateStore",
    "StateUnavailableError",
    "collection_marker",
]
