"""V2-13 tests: shared state and several workers (docs/ARCHITECTURE_V2.md D7 and §13).

A "worker" is one complete service (its own app, RelayService, provider, guard, queue and
/v1/ask context) as one ``relay serve`` process would run it. Two workers share one
``StateStore``: an in-process Redis (fakeredis, Lua scripts included), with two separate
clients as two processes would have, or the in-memory reference store. Everything runs
offline: no Redis daemon, no network, no API key.

1. Worker 1 warms the cache; worker 2 answers from it with no model call.
2. Ten identical concurrent requests across two workers make exactly one provider call,
   and all ten receive the same verified answer.
3. Two workers sharing a 10 RPM lane: the 11th request is refused, and the provider never
   sees more than 10 calls.
4. The leader's worker dies mid-stream: its follower on the other worker ends with an
   explicit error once the lock expires, and the next request takes the expired lock.
5. ``backend = "memory"`` (the default) needs no Redis and keeps today's objects.

Plus the ``StateStore`` contract, run against both implementations.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AsyncExitStack
from datetime import date
from typing import Any

import fakeredis
import pytest

from relay.api.app import create_app
from relay.api.routes.ask_v1 import AskV1Context, ask_v1_router
from relay.app.collections import StoreCollectionResolver
from relay.app.compose import open_state, share_state
from relay.app.settings import RelaySettings, StateSettings
from relay.control.admission import AdmissionQueue
from relay.control.cache import CachedAnswer
from relay.control.provider_guard import (
    GuardConfig,
    ProviderGuard,
    QuotaWaitExceedsDeadlineError,
)
from relay.control.state import (
    InMemoryStateStore,
    QuotaLimits,
    RedisStateStore,
    SharedQuota,
    StateStore,
)
from relay.errors import ConfigurationError
from relay.orchestrator.pipeline import PipelineConfig
from relay.providers.base import ChatMessage, ChatRequest, ScriptedReply, TokenUsage
from relay.retrieval.registry import IndexRegistry
from tests.test_ask_v1 import S1, S3, SlowProvider, answer, ask, env, geology, question
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever

__all__ = ["env"]  # the fixture, re-used from the /v1/ask tests

TODAY = date(2026, 11, 21)
BACKENDS = ("redis", "memory")


# --- Shared stores -----------------------------------------------------------------------


class SharedBackend:
    """One shared state, and a connection to it per worker."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.server = fakeredis.FakeServer()
        self.memory = InMemoryStateStore()
        self.opened: list[StateStore] = []

    def connect(self) -> StateStore:
        store: StateStore = (RedisStateStore(fakeredis.FakeAsyncRedis(server=self.server))
                             if self.kind == "redis" else self.memory)
        self.opened.append(store)
        return store

    async def aclose(self) -> None:
        for store in self.opened:
            await store.aclose()


@pytest.fixture(params=BACKENDS)
async def shared(request: pytest.FixtureRequest) -> AsyncGenerator[SharedBackend]:
    backend = SharedBackend(request.param)
    yield backend
    await backend.aclose()


class Severable:
    """A worker's connection to the shared state that can be cut, as when its process is
    killed: from then on nothing it does reaches the shared store (no renewal of its lock,
    no more events, no release)."""

    def __init__(self, inner: StateStore) -> None:
        self.inner = inner
        self.name = inner.name
        self.severed = False

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self.inner, attr)
        if attr == "subscribe_flight" or not callable(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            if self.severed:
                raise ConnectionError("the worker's process is gone")
            return await target(*args, **kwargs)

        return call


# --- Workers -----------------------------------------------------------------------------


class Worker:
    """One complete service over the shared store, as one ``relay serve`` would run it."""

    def __init__(self, env: Env, state: Any, replies: list[ScriptedReply], *,
                 delay_s: float = 0.0, guard: GuardConfig | None = None,
                 flight_ttl_s: float = 30.0, lease_poll_s: float = 0.05,
                 max_active: int = 4) -> None:
        self.provider = SlowProvider(replies, delay_s)
        resolver = StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                                    embedder=FakeEmbedder()))
        self.app = create_app(
            provider=self.provider, retrievers={"small_fast": StubRetriever(PASSAGES)},
            top_k={"small_fast": 4},
            pipeline_config=PipelineConfig(model_id="test-model", max_completion_tokens=300),
            guard=ProviderGuard(guard or GuardConfig(max_concurrency=4)),
            queue=AdmissionQueue(capacity=32, service_rate_per_s=10.0,
                                 max_active=max_active),
            corpus_sha256="c" * 64, cache_mode="exact", collections=resolver)
        self.service = self.app.state.relay
        if state is not None:
            share_state(state, StateSettings(backend="redis"), self.service,
                        self.service.router)
        self.context = AskV1Context(service=self.service, store=env.store, state=state,
                                    flight_ttl_s=flight_ttl_s, lease_poll_s=lease_poll_s)
        self.app.include_router(ask_v1_router(self.context))

    @property
    def calls(self) -> int:
        return len(self.provider.requests)

    async def aclose(self) -> None:
        await self.context.aclose()


@pytest.fixture
async def workers() -> AsyncGenerator[Callable[..., Worker]]:
    made: list[Worker] = []

    def make(*args: Any, **kwargs: Any) -> Worker:
        worker = Worker(*args, **kwargs)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        await worker.aclose()


def final_of(stream: Any) -> dict[str, Any]:
    finals = stream.of("final")
    assert len(finals) == 1, stream.names
    return finals[0]


def texts(stream: Any) -> list[str]:
    return [s["text"] for s in stream.of("sentence")]


# 1 --------------------------------------------------------------------------------------
async def test_one_workers_cached_answer_is_served_by_the_other(
        env: Env, shared: SharedBackend, workers: Callable[..., Worker]) -> None:
    cid = await geology(env)
    one = workers(env, shared.connect(), [answer(S1, S3)])
    two = workers(env, shared.connect(), [answer(S1, S3)])

    warmed = await ask(one.app, question(cid))
    assert final_of(warmed)["cache_status"] == "miss" and texts(warmed) == [S1, S3]
    served = await ask(two.app, question(cid))

    final = final_of(served)
    assert (final["status"], final["cache_status"], final["verdict"]) == (
        "ok", "exact_hit", "VERIFIED")
    assert texts(served) == [S1, S3]
    assert (one.calls, two.calls) == (1, 0)  # worker 2 made no model call
    assert final["tokens_in"] == final["tokens_out"] == 0


# 2 --------------------------------------------------------------------------------------
async def test_ten_identical_requests_across_two_workers_make_one_provider_call(
        env: Env, shared: SharedBackend, workers: Callable[..., Worker]) -> None:
    cid = await geology(env)
    one = workers(env, shared.connect(), [answer(S1, S3, pieces=12)], delay_s=0.03)
    two = workers(env, shared.connect(), [answer(S1, S3, pieces=12)], delay_s=0.03)

    streams = await asyncio.wait_for(asyncio.gather(*(
        ask((one if i % 2 == 0 else two).app, question(cid)) for i in range(10))), 30)

    assert one.calls + two.calls == 1  # exactly one provider call for all ten
    finals = [final_of(stream) for stream in streams]
    assert all((f["status"], f["verdict"], f["partial"]) == ("ok", "VERIFIED", False)
               for f in finals)
    assert all(texts(stream) == [S1, S3] for stream in streams)  # the identical answer
    statuses = sorted(f["cache_status"] for f in finals)
    assert statuses == ["coalesced"] * 9 + ["miss"]
    follower_worker = two if one.calls == 1 else one
    remote = [final_of(s) for i, s in enumerate(streams)
              if (one if i % 2 == 0 else two) is follower_worker]
    # every request on the worker that made no call followed the other worker's flight
    assert len(remote) == 5 and all(f["coalesced"] and f["tokens_in"] == 0 for f in remote)
    assert len({f["request_id"] for f in finals}) == 10


# 3 --------------------------------------------------------------------------------------
def chat(prompt_chars: int = 300, completion: int = 100) -> ChatRequest:
    return ChatRequest(model="test-model",
                       messages=(ChatMessage(role="user", content="x" * prompt_chars),),
                       max_completion_tokens=completion)


async def test_two_guards_share_one_rpm_window(shared: SharedBackend) -> None:
    config = GuardConfig(max_concurrency=16, rpm_limit=10)
    guards = [ProviderGuard(config) for _ in range(2)]
    quotas = [SharedQuota(shared.connect(), "groq-qwen") for _ in guards]
    for guard, quota in zip(guards, quotas, strict=True):
        guard.share(quota)

    permits = [await guards[i % 2].acquire(chat()) for i in range(10)]
    # each guard alone has seen only 5 calls and would admit an 11th at once
    assert all(g.predict(chat()).wait_ns == 0 for g in guards)
    for guard in guards:
        with pytest.raises(QuotaWaitExceedsDeadlineError):
            await guard.acquire(chat(), deadline_ns=time.perf_counter_ns() + 1_000_000_000)

    for i, permit in enumerate(permits):
        guards[i % 2].settle(permit, usage=TokenUsage(prompt_tokens=90, completion_tokens=10,
                                                      total_tokens=100),
                             stream_opened=True)
    for quota in quotas:
        await quota.drain()


async def test_two_workers_on_a_10_rpm_lane_never_exceed_it(
        env: Env, shared: SharedBackend, workers: Callable[..., Worker]) -> None:
    cid = await geology(env)
    lane = GuardConfig(max_concurrency=4, rpm_limit=10)
    one = workers(env, shared.connect(), [answer(S1) for _ in range(11)], guard=lane)
    two = workers(env, shared.connect(), [answer(S1) for _ in range(11)], guard=lane)

    finals = []
    for i in range(11):  # eleven different questions: nothing is cached or coalesced
        stream = await ask((one if i % 2 == 0 else two).app,
                           {**question(cid, f"What is basalt? ({i})"), "deadline_ms": 1500})
        finals.append(final_of(stream))

    assert one.calls + two.calls == 10  # the provider never saw an 11th call this minute
    assert all(f["status"] == "ok" and f["provider_called"] for f in finals[:10])
    eleventh = finals[10]
    assert eleventh["status"] != "ok" and not eleventh["provider_called"]
    assert eleventh["error_code"] == "predicted_wait_exceeds_deadline"


async def test_the_shared_quota_fails_closed_when_the_store_is_gone(
        shared: SharedBackend) -> None:
    store = Severable(shared.connect())
    guard = ProviderGuard(GuardConfig(rpm_limit=10))
    guard.share(SharedQuota(store, "groq-qwen"))  # type: ignore[arg-type]
    store.severed = True
    with pytest.raises(Exception) as refused:
        await guard.acquire(chat())
    assert getattr(refused.value, "error_code", None) == "internal_error"
    assert guard.snapshot().requests_last_minute == 0  # nothing reserved locally either


# 4 --------------------------------------------------------------------------------------
async def test_a_dead_leader_ends_its_remote_follower_and_frees_the_lock(
        env: Env, shared: SharedBackend, workers: Callable[..., Worker]) -> None:
    cid = await geology(env)
    cut = Severable(shared.connect())
    leader = workers(env, cut, [answer(S1, S3, pieces=12)], delay_s=0.08,
                     flight_ttl_s=0.3, lease_poll_s=0.05)
    other = workers(env, shared.connect(), [answer(S1, S3)], flight_ttl_s=0.3,
                    lease_poll_s=0.05)

    leading = asyncio.create_task(ask(leader.app, question(cid)))
    while not leader.context.flights:  # the leader's flight exists and holds the lock
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    killed_at: list[float] = []

    def kill_on_first_sentence(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "sentence" and not cut.severed:
            cut.severed = True  # the leader's process dies mid-stream
            killed_at.append(time.perf_counter())

    followed = await asyncio.wait_for(ask(other.app, question(cid),
                                          on_event=kill_on_first_sentence), 10)
    ended_at = time.perf_counter()

    final = final_of(followed)
    assert killed_at and ended_at - killed_at[0] < 3.0  # bounded: it never hangs
    assert (final["status"], final["error_code"], final["shed_reason"]) == (
        "error", "internal_error", "leader_lost")
    assert final["partial"] is True and final["coalesced"] is True
    assert followed.of("stopped") == [{"reason_class": "leader_lost", "partial": True}]
    assert texts(followed) == [S1]  # only what the leader verified before it died
    assert other.calls == 0

    # the dead leader's lock has expired: the next request on the other worker takes it
    retried = await asyncio.wait_for(ask(other.app, question(cid)), 10)
    assert (final_of(retried)["status"], final_of(retried)["cache_status"]) == ("ok", "miss")
    assert texts(retried) == [S1, S3] and other.calls == 1
    await leading  # the leader's own client was still served by its (local) flight


async def test_a_follower_of_a_leader_that_finishes_normally_is_never_cut_off(
        env: Env, shared: SharedBackend, workers: Callable[..., Worker]) -> None:
    """The lease check must not mistake a leader that released its lock after its last
    event for a dead one."""
    cid = await geology(env)
    one = workers(env, shared.connect(), [answer(S1, S3, pieces=12)], delay_s=0.04,
                  flight_ttl_s=0.3, lease_poll_s=0.02)
    two = workers(env, shared.connect(), [], flight_ttl_s=0.3, lease_poll_s=0.02)
    leading = asyncio.create_task(ask(one.app, question(cid)))
    while not one.context.flights:
        await asyncio.sleep(0.01)
    followed = await asyncio.wait_for(ask(two.app, question(cid)), 10)
    await leading
    assert (final_of(followed)["status"], final_of(followed)["cache_status"]) == (
        "ok", "coalesced")
    assert texts(followed) == [S1, S3] and (one.calls, two.calls) == (1, 0)


async def test_a_queued_leader_whose_client_left_still_serves_its_remote_followers(
        env: Env, shared: SharedBackend, workers: Callable[..., Worker]) -> None:
    """Review finding: the leader's own client leaves while it waits in the queue; the
    flight is kept for the other worker's follower, so the queue must not shed it as
    ``client_gave_up``."""
    cid = await geology(env)
    one = workers(env, shared.connect(), [answer(S1, pieces=12), answer(S1, S3)],
                  delay_s=0.05, max_active=1)
    two = workers(env, shared.connect(), [])
    busy = asyncio.create_task(ask(one.app, question(cid, "What is lava?")))
    while one.service.queue.active == 0:  # the one slot of worker one is taken
        await asyncio.sleep(0.01)
    gone = asyncio.Event()
    leading = asyncio.create_task(ask(one.app, question(cid), gone=gone))
    while one.service.queue.depth == 0:  # the leader waits in the queue
        await asyncio.sleep(0.01)
    following = asyncio.create_task(ask(two.app, question(cid)))
    await asyncio.sleep(0.2)  # the follower is subscribed on worker two
    gone.set()  # the leader's own client leaves
    followed = await asyncio.wait_for(following, 10)
    await busy
    await leading
    final = final_of(followed)
    assert (final["status"], final["error_code"], final["coalesced"]) == ("ok", None, True)
    assert texts(followed) == [S1, S3] and (one.calls, two.calls) == (2, 0)


class SlowStore:
    """A shared store whose quota calls take a while (a cancel can land mid-call)."""

    def __init__(self, inner: StateStore, delay_s: float) -> None:
        self.inner, self.delay_s, self.name = inner, delay_s, inner.name

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.inner, attr)

    async def reserve_tokens(self, *args: Any, **kwargs: Any) -> Any:
        decision = await self.inner.reserve_tokens(*args, **kwargs)
        await asyncio.sleep(self.delay_s)  # the script ran; its reply is slow to arrive
        return decision


async def test_a_reservation_cancelled_in_flight_is_given_back(shared: SharedBackend) -> None:
    store = shared.connect()
    quota = SharedQuota(SlowStore(store, 0.2), "lane")  # type: ignore[arg-type]
    guard = ProviderGuard(GuardConfig(rpm_limit=1))
    guard.share(quota)
    acquiring = asyncio.create_task(guard.acquire(chat()))
    await asyncio.sleep(0.05)  # the reservation is made, its reply not yet received
    acquiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    await asyncio.sleep(0.3)
    await quota.drain()
    granted = await store.reserve_tokens("lane", 10, limits=QuotaLimits(rpm=1), day=TODAY)
    assert granted.reservation is not None  # the one RPM slot was given back


# 5 --------------------------------------------------------------------------------------
def test_memory_is_the_default_backend() -> None:
    assert RelaySettings().state.backend == "memory"
    with pytest.raises(ValueError, match="lease_poll_s"):
        StateSettings(flight_lock_ttl_s=1.0, lease_poll_s=1.0)


def test_the_default_path_never_imports_redis() -> None:
    probe = ("import sys, relay.app.compose, relay.api.routes.ask_v1, relay.control.state; "
             "print('redis' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True, timeout=120)
    assert out.stdout.strip() == "False"


async def test_memory_backend_keeps_the_in_process_objects(
        env: Env, workers: Callable[..., Worker]) -> None:
    async with AsyncExitStack() as stack:
        assert await open_state(StateSettings(), stack) is None
    cid = await geology(env)
    alone = workers(env, None, [answer(S1, S3, pieces=12)], delay_s=0.02)
    assert alone.context.state is None and alone.service.cache.shared is None
    assert alone.service.router.primary.guard._shared is None
    streams = await asyncio.gather(*(ask(alone.app, question(cid)) for _ in range(3)))
    assert alone.calls == 1 and all(texts(s) == [S1, S3] for s in streams)
    assert len(alone.service.cache) == 1  # the in-process cache, as before V2-13


async def test_an_unreachable_redis_fails_start_up_without_its_credentials() -> None:
    settings = StateSettings(backend="redis", redis_url="redis://relay:s3cret@127.0.0.1:1/0")
    async with AsyncExitStack() as stack:
        with pytest.raises(ConfigurationError) as failed:
            await open_state(settings, stack)
    assert "127.0.0.1:1/0" in str(failed.value) and "s3cret" not in str(failed.value)


# --- The StateStore contract (both implementations) --------------------------------------


def cached(text: str = "Basalt is volcanic [P1].") -> CachedAnswer:
    return CachedAnswer(delivered_text=text, refused=False, citations=(),
                        verification_verdict="VERIFIED", model_id="m", source_request_id="r")


async def test_contract_cache_round_trip_expiry_and_purge(shared: SharedBackend) -> None:
    one, two = shared.connect(), shared.connect()
    await one.set_cached_answer("c|small_fast@geo:v1:x|p|m|q1", cached(), 60)
    await one.set_cached_answer("c|small_fast@art:v1:x|p|m|q1", cached("Other [P1]."), 60)
    await one.set_cached_answer("c|small_fast@geo:v1:x|p|m|short", cached(), 0.05)
    assert await two.get_cached_answer("c|small_fast@geo:v1:x|p|m|q1") == cached()
    assert await two.get_cached_answer("absent") is None
    await asyncio.sleep(0.12)
    assert await two.get_cached_answer("c|small_fast@geo:v1:x|p|m|short") is None
    assert await two.purge_cached("geo") == 1
    assert await one.get_cached_answer("c|small_fast@geo:v1:x|p|m|q1") is None
    assert await one.get_cached_answer("c|small_fast@art:v1:x|p|m|q1") is not None


async def test_contract_the_flight_lock_has_one_holder_and_expires(
        shared: SharedBackend) -> None:
    one, two = shared.connect(), shared.connect()
    first = await one.acquire_flight_lock("f", 150)
    second = await two.acquire_flight_lock("f", 150)
    assert first.owned and not second.owned and second.token == first.token
    assert not await two.refresh_flight_lock("f", "not-the-holder", 150)
    await two.release_flight_lock("f", "not-the-holder")  # only the holder can release
    assert await one.flight_holder("f") == first.token
    assert await one.refresh_flight_lock("f", first.token, 150)
    await asyncio.sleep(0.25)  # not renewed: the holder is presumed dead
    assert await two.flight_holder("f") is None
    taken = await two.acquire_flight_lock("f", 150)
    assert taken.owned and taken.token != first.token
    await one.release_flight_lock("f", first.token)  # a late release changes nothing
    assert await two.flight_holder("f") == taken.token
    await two.release_flight_lock("f", taken.token)
    assert await one.flight_holder("f") is None


async def test_contract_a_late_subscriber_gets_every_event_in_order(
        shared: SharedBackend) -> None:
    leader, follower = shared.connect(), shared.connect()
    lease = await leader.acquire_flight_lock("f", 5000)
    for seq in range(3):  # published before anyone subscribed
        await leader.publish_flight_event("f", lease.token, {"seq": seq, "name": "status",
                                                             "data": {"stage": str(seq)}})
    got: list[Any] = []

    async def follow() -> None:
        events: AsyncIterator[Any] = follower.subscribe_flight("f", lease.token, poll_s=0.05)
        async for event in events:
            got.append(event)
            if event is not None and event["name"] == "_summary":
                await events.aclose()  # type: ignore[attr-defined]
                return

    following = asyncio.create_task(follow())
    await asyncio.sleep(0.15)  # silence: the follower is told (None), and keeps waiting
    assert await leader.flight_followers("f", lease.token) == 1
    await leader.publish_flight_event("f", lease.token, {"seq": 3, "name": "_summary",
                                                         "data": {"status": "ok"}})
    await asyncio.wait_for(following, 5)
    events = [e for e in got if e is not None]
    assert [e["seq"] for e in events] == [0, 1, 2, 3]
    assert events[0]["data"] == {"stage": "0"} and None in got


async def test_contract_quota_reservations_are_atomic_across_workers(
        shared: SharedBackend) -> None:
    one, two = shared.connect(), shared.connect()
    limits = QuotaLimits(rpm=10)
    decisions = await asyncio.gather(*(
        (one if i % 2 else two).reserve_tokens("lane", 100, limits=limits, day=TODAY)
        for i in range(40)))
    granted = [d for d in decisions if d.reservation is not None]
    assert len(granted) == 10  # forty concurrent attempts, never more than the limit
    assert all(0 < d.wait_ms <= 60_000 for d in decisions if d.reservation is None)


async def test_contract_a_cancelled_reservation_frees_its_slot_and_daily_counts(
        shared: SharedBackend) -> None:
    one, two = shared.connect(), shared.connect()
    limits = QuotaLimits(rpm=1, rpd_stop=1)
    first = await one.reserve_tokens("lane", 50, limits=limits, day=TODAY)
    assert first.reservation is not None
    assert (await two.reserve_tokens("lane", 50, limits=limits, day=TODAY)).refusal == \
        "daily_budget_exhausted"
    await one.cancel_reservation("lane", first.reservation, 50, day=TODAY)  # never called
    again = await two.reserve_tokens("lane", 50, limits=limits, day=TODAY)
    assert again.reservation is not None  # the RPM slot and the daily request are back


async def test_contract_tokens_are_settled_and_daily_stops_hold(shared: SharedBackend) -> None:
    one, two = shared.connect(), shared.connect()
    limits = QuotaLimits(tpm=1000, tpd_stop=1500)
    big = await one.reserve_tokens("lane", 800, limits=limits, day=TODAY)
    assert big.reservation is not None
    assert (await two.reserve_tokens("lane", 400, limits=limits, day=TODAY)).wait_ms > 0
    await one.settle_tokens("lane", big.reservation, 800, 300, day=TODAY)  # actual usage
    fits = await two.reserve_tokens("lane", 400, limits=limits, day=TODAY)
    assert fits.reservation is not None  # 300 + 400 <= 1000 after settling
    assert (await one.reserve_tokens("lane", 5000, limits=limits, day=TODAY)).refusal == \
        "payload_too_large"
    await two.settle_tokens("lane", fits.reservation, 400, 1300, day=TODAY)  # 1600 today
    stopped = await one.reserve_tokens("lane", 10, limits=limits, day=TODAY)
    assert stopped.refusal == "daily_budget_exhausted"
    tomorrow = await one.reserve_tokens("other", 10, limits=limits, day=date(2026, 11, 22))
    assert tomorrow.reservation is not None
    stops = QuotaLimits(rpd_stop=2)
    for _ in range(2):
        assert (await one.reserve_tokens("rpd", 1, limits=stops, day=TODAY)).reservation
    assert (await two.reserve_tokens("rpd", 1, limits=stops, day=TODAY)).refusal == \
        "daily_budget_exhausted"


async def test_redis_calls_are_bounded_by_socket_timeouts() -> None:
    store = RedisStateStore.from_url("redis://127.0.0.1:1/0", socket_timeout_s=2.5)
    pool = store._client.connection_pool.connection_kwargs  # no connection is opened
    assert (pool["socket_timeout"], pool["socket_connect_timeout"]) == (2.5, 2.5)
    await store.aclose()
