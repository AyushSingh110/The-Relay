"""V2-10 admission units: v1 FIFO unchanged, buckets, priority ranks, shedding, EWMA
capacity; the dev-key policy; `relay keys` / `relay tenants`."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from relay.app.compose import admission_queue, capacity_derivation, dev_key_enabled
from relay.app.settings import RelaySettings
from relay.cli import main
from relay.control.admission import (
    AdmissionQueue,
    AdmissionRejectedError,
    CapacityInputs,
    TenantBucket,
    derive_capacity,
)
from relay.store import open_store

_S = 1_000_000_000


class Clock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


async def order(queue: AdmissionQueue,
                arrivals: Sequence[tuple[str, str | None, str]]) -> list[str]:
    """Admit one blocker, queue ``arrivals`` (tag, tenant, priority), then release them
    one by one; the order they were admitted in (``shed:tag`` for a shed one)."""
    admitted: list[str] = []
    gate = asyncio.Event()

    async def hold() -> None:
        async with queue.admit():
            await gate.wait()

    async def one(tag: str, tenant: str | None, priority: str) -> None:
        try:
            async with queue.admit(tenant=tenant, priority=priority):
                admitted.append(tag)
        except AdmissionRejectedError:
            admitted.append(f"shed:{tag}")

    blocker = asyncio.create_task(hold())
    await asyncio.sleep(0)
    tasks = []
    for arrival in arrivals:
        tasks.append(asyncio.create_task(one(*arrival)))
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(blocker, *tasks)
    return admitted


async def test_without_tenants_the_queue_is_v1_fifo() -> None:
    queue = AdmissionQueue(capacity=10, service_rate_per_s=1.0, tenant_rate_per_s=0.01)
    assert await order(queue, [(t, None, "interactive") for t in "abcde"]) == list("abcde")


async def test_without_tenants_a_burst_is_served_first_as_in_v1() -> None:
    """The baseline V2-10 fixes: no tenants (v1 /ask) is FIFO, so b waits behind a's
    whole burst; with tenants, b gets a turn after at most one of a's."""
    burst = [(f"a{i}", None, "interactive") for i in range(4)] + [("b1", None, "interactive")]
    fifo = await order(AdmissionQueue(capacity=10, service_rate_per_s=1.0), burst)
    assert fifo.index("b1") == 4
    tenants = [(tag, "a" if tag.startswith("a") else "b", p) for tag, _, p in burst]
    fair = await order(AdmissionQueue(capacity=10, service_rate_per_s=1.0), tenants)
    assert fair.index("b1") <= 1


async def test_interactive_goes_before_batch() -> None:
    queue = AdmissionQueue(capacity=10, service_rate_per_s=1.0)
    got = await order(queue, [("b1", "x", "batch"), ("b2", "x", "batch"),
                              ("i1", "y", "interactive")])
    assert got == ["i1", "b1", "b2"]


async def test_tenants_take_turns_within_a_rank() -> None:
    queue = AdmissionQueue(capacity=10, service_rate_per_s=1.0)
    got = await order(queue, [("a1", "a", "interactive"), ("a2", "a", "interactive"),
                              ("a3", "a", "interactive"), ("b1", "b", "interactive")])
    assert got.index("b1") <= 1  # b does not wait behind all of a's


async def test_over_rate_requests_wait_behind_in_rate_ones() -> None:
    clock = Clock()
    queue = AdmissionQueue(capacity=10, service_rate_per_s=1.0, clock=clock,
                           tenant_rate_per_s=0.001, tenant_burst=1.0)
    got = await order(queue, [("a1", "a", "interactive"), ("a2", "a", "interactive"),
                              ("b1", "b", "interactive")])
    assert got == ["a1", "b1", "a2"]  # a2 found a's bucket empty


async def test_a_full_queue_sheds_its_worst_waiter_for_a_better_arrival() -> None:
    queue = AdmissionQueue(capacity=2, service_rate_per_s=1.0)
    got = await order(queue, [("b1", "x", "batch"), ("b2", "x", "batch"),
                              ("i1", "y", "interactive"), ("b3", "x", "batch")])
    assert "shed:b2" in got and "shed:b3" in got  # newest batch waiter, then the arrival
    assert got.index("i1") < got.index("b1")
    assert queue.shed_for_priority == 1


async def timed_order(queue: AdmissionQueue, clock: Clock,
                      arrivals: Sequence[tuple[str, str | None, str]],
                      step_s: float) -> list[str]:
    """``order`` with the clock moved ``step_s`` before each arrival (buckets refill)."""
    admitted: list[str] = []
    gate = asyncio.Event()

    async def hold() -> None:
        async with queue.admit():
            await gate.wait()

    async def one(tag: str, tenant: str | None, priority: str) -> None:
        try:
            async with queue.admit(tenant=tenant, priority=priority):
                admitted.append(tag)
        except AdmissionRejectedError as exc:
            admitted.append(f"shed:{tag}:{exc.reason}")

    blocker = asyncio.create_task(hold())
    await asyncio.sleep(0)
    tasks = []
    for arrival in arrivals:
        clock.now += int(step_s * _S)
        tasks.append(asyncio.create_task(one(*arrival)))
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(blocker, *tasks)
    return admitted


# F-02c: work-conserving admission (a tenant never evicts or overtakes its own requests) ---
async def test_a_lone_tenant_is_fifo_even_when_its_bucket_refills() -> None:
    """a1 finds the bucket empty (over rate), a2 arrives after a refill (within rate): a2
    must not overtake a1, nor evict it when the queue is full."""
    clock = Clock()
    queue = AdmissionQueue(capacity=2, service_rate_per_s=1.0, clock=clock,
                           tenant_rate_per_s=0.5, tenant_burst=1.0)
    got = await timed_order(queue, clock, [(f"a{i}", "a", "interactive") for i in range(4)],
                            step_s=1.0)
    admitted = [t for t in got if not t.startswith("shed:")]
    assert admitted == sorted(admitted, key=lambda t: int(t[1:]))  # arrival order
    assert not any(t.endswith(":shed_for_priority") for t in got)
    assert queue.shed_for_priority == 0


async def test_a_burst_of_30_from_one_tenant_never_evicts_its_own_earlier_requests() -> None:
    clock = Clock()
    queue = AdmissionQueue(capacity=5, service_rate_per_s=1.0, clock=clock,
                           tenant_rate_per_s=0.4, tenant_burst=2.0)
    got = await timed_order(queue, clock, [(f"a{i:02d}", "a", "interactive")
                                           for i in range(30)], step_s=0.7)
    assert queue.shed_for_priority == 0
    shed = [t for t in got if t.startswith("shed:")]
    assert all(t.endswith(":queue_full") for t in shed)  # only arrivals at a full queue
    admitted = [t for t in got if not t.startswith("shed:")]
    assert admitted == sorted(admitted)  # FIFO among the admitted


async def test_a_within_rate_tenant_still_evicts_another_tenants_over_rate_backlog() -> None:
    """V2-10's noisy-neighbour protection is kept: B (within rate) takes the place of A's
    over-rate waiter when the queue is full."""
    clock = Clock()
    queue = AdmissionQueue(capacity=2, service_rate_per_s=1.0, clock=clock,
                           tenant_rate_per_s=0.001, tenant_burst=1.0)
    got = await timed_order(queue, clock, [("a1", "a", "interactive"),
                                           ("a2", "a", "interactive"),
                                           ("b1", "b", "interactive")], step_s=0.01)
    assert "shed:a2:shed_for_priority" in got  # a2 was over rate; b1 was not
    assert [t for t in got if not t.startswith("shed:")] == ["a1", "b1"]
    assert queue.shed_for_priority == 1


async def test_the_predicted_wait_is_counted_after_the_eviction_it_causes() -> None:
    """Review F-02c: evicting B's only waiter ends the contention, so A's arrival ranks
    first; its deadline check must use that position, not the pre-eviction one."""
    clock = Clock()
    queue = AdmissionQueue(capacity=2, service_rate_per_s=1.0, clock=clock,
                           tenant_rate_per_s=0.001, tenant_burst=1.0)
    results: list[str] = []
    gate = asyncio.Event()

    async def hold() -> None:
        async with queue.admit(tenant="b", priority="batch"):  # takes B's only token
            await gate.wait()

    async def one(tag: str, tenant: str, priority: str, deadline_s: float | None) -> None:
        deadline = None if deadline_s is None else clock.now + int(deadline_s * _S)
        try:
            async with queue.admit(tenant=tenant, priority=priority, deadline_ns=deadline):
                results.append(tag)
        except AdmissionRejectedError as exc:
            results.append(f"shed:{tag}:{exc.error_code}")

    blocker = asyncio.create_task(hold())
    await asyncio.sleep(0)
    tasks = [asyncio.create_task(one("a1", "a", "batch", None))]   # in rate
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(one("b1", "b", "batch", None)))   # over rate
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(one("a2", "a", "interactive", 1.5)))  # over rate
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(blocker, *tasks)
    assert "shed:a2:predicted_wait_exceeds_deadline" not in results, results
    assert "a2" in results


async def test_a_tenants_interactive_request_may_still_evict_its_own_batch_one() -> None:
    queue = AdmissionQueue(capacity=2, service_rate_per_s=1.0)
    got = await order(queue, [("b1", "x", "batch"), ("b2", "x", "batch"),
                              ("i1", "x", "interactive")])
    assert "shed:b2" in got and got.index("i1") < got.index("b1")


def test_a_bucket_refills_at_its_rate() -> None:
    bucket = TenantBucket(rate_per_s=2.0, burst=2.0, tokens=2.0, updated_ns=0)
    assert [bucket.take(0) for _ in range(3)] == [True, True, False]
    assert bucket.take(_S // 2) is True  # half a second: one more
    assert bucket.take(_S // 2) is False


def test_capacity_follows_the_ewma_of_tokens_per_answer() -> None:
    inputs = CapacityInputs(patience_quantile_s=20.0, tpm=8000.0, tokens_per_answer=1500.0)
    queue = AdmissionQueue.from_derivation(derive_capacity(inputs), adaptive=True,
                                           ewma_alpha=0.5)
    assert queue.capacity == derive_capacity(inputs).capacity
    queue.observe_tokens(500)  # EWMA: 0.5 * 500 + 0.5 * 1500 = 1000
    assert queue.tokens_per_answer == 1000.0
    expected = derive_capacity(inputs.model_copy(update={"tokens_per_answer": 1000.0}))
    assert (queue.capacity, queue.service_rate_per_s) == (expected.capacity,
                                                           expected.service_rate_per_s)
    queue.observe_tokens(100)  # 0.5 * 100 + 0.5 * 1000 = 550
    assert queue.tokens_per_answer == 550.0
    assert queue.capacity == derive_capacity(
        inputs.model_copy(update={"tokens_per_answer": 550.0})).capacity


def test_a_fixed_capacity_is_not_adapted() -> None:
    settings = RelaySettings.model_validate({"queue": {"capacity": 7}})
    queue = admission_queue(settings, capacity_derivation(settings))
    queue.observe_tokens(10)
    assert queue.capacity == 7


@pytest.mark.parametrize(("host", "setting", "expected"), [
    ("127.0.0.1", None, True), ("localhost", None, True), ("::1", None, True),
    ("0.0.0.0", None, False), ("relay.example.org", None, False),
    ("0.0.0.0", True, True), ("127.0.0.1", False, False)])
def test_the_dev_key_is_accepted_only_on_loopback_unless_set(
        host: str, setting: bool | None, expected: bool) -> None:
    settings = RelaySettings.model_validate({"server": {"host": host},
                                             "auth": {"dev_key_enabled": setting}})
    assert dev_key_enabled(settings) is expected


# --- relay keys / relay tenants -------------------------------------------------------------
def run(capsys: Any, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_relay_keys_create_list_and_revoke(tmp_path: Path, monkeypatch: Any,
                                           capsys: Any) -> None:
    monkeypatch.setenv("RELAY_PATHS__STORE_PATH", str(tmp_path / "relay.sqlite3"))
    monkeypatch.chdir(tmp_path)  # no relay.toml here: defaults and the environment
    code, out = run(capsys, "tenants", "create", "--name", "Acme")
    assert code == 0
    tenant = out.split()[1]
    code, out = run(capsys, "keys", "create", "--tenant", tenant, "--role", "admin",
                    "--name", "ci")
    assert code == 0
    raw = out.splitlines()[0]
    assert raw.startswith("rel_live_") and "shown only once" in out
    key_id = out.splitlines()[1].split()[2].rstrip(",")
    code, listing = run(capsys, "keys", "list", "--tenant", tenant)
    assert code == 0 and key_id in listing and "ci" in listing and "active" in listing
    assert raw not in listing and raw[len("rel_live_"):] not in listing  # never the key
    code, out = run(capsys, "keys", "revoke", "--id", key_id)
    assert code == 0 and "revoked" in out
    code, listing = run(capsys, "keys", "list", "--tenant", tenant)
    assert "revoked" in listing

    async def stored() -> list[Any]:
        async with open_store(tmp_path / "relay.sqlite3") as store:
            return await store.api_keys.list_for_tenant(tenant)

    (record,) = asyncio.run(stored())
    assert record.revoked and record.key_hash != raw


def test_relay_keys_create_for_an_unknown_tenant_fails(tmp_path: Path, monkeypatch: Any,
                                                       capsys: Any) -> None:
    monkeypatch.setenv("RELAY_PATHS__STORE_PATH", str(tmp_path / "relay.sqlite3"))
    monkeypatch.chdir(tmp_path)
    code, out = run(capsys, "keys", "create", "--tenant", "nobody")
    assert code == 1 and "no tenant" in out
