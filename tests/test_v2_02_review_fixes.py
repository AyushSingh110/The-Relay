"""V2-02 regression tests for the review findings (each failed before its fix).

All stores are ``:memory:``; failures are injected, no real file is opened.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest

import relay.store as store_module
from relay import cli
from relay.app import compose
from relay.app.request_log import RequestLogSink
from relay.app.settings import LaneSettings, RelaySettings
from relay.config import Settings
from relay.store import Store, StoreOpenError, open_store
from relay.store.repositories import ForeignKeyViolationError
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever
from tests.test_standin import MODEL, profile
from tests.test_store_integration import request_record


@pytest.fixture
async def store() -> Any:
    async with open_store(":memory:") as fresh:
        yield fresh


def slow_commit(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the gap between a write's INSERT and its COMMIT, as real I/O does."""
    original = store.conn.commit

    async def commit() -> None:
        await asyncio.sleep(0.02)
        await original()

    monkeypatch.setattr(store.conn, "commit", commit)


# 1 --------------------------------------------------------------------------------------
async def test_a_failed_write_never_rolls_back_a_concurrent_write(
        store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    slow_commit(store, monkeypatch)
    good = asyncio.create_task(store.tenants.create("survivor"))
    await asyncio.sleep(0.005)  # its INSERT has run; its COMMIT is in flight
    with pytest.raises(ForeignKeyViolationError):
        await store.collections.create("no-such-tenant", "x")
    tenant = await good
    assert await store.tenants.get_by_id(tenant.id) == tenant


# 2 --------------------------------------------------------------------------------------
async def test_request_log_counters_are_reported_by_healthz(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(profile().model_dump_json(), encoding="utf-8")
    settings = RelaySettings(
        lanes=(LaneSettings(name="s", kind="standin", model=MODEL, embedded=True,
                            profile=path),),
        retrieval={"settings": ("small_fast",)}, cache={"mode": "sqlite"},  # type: ignore[arg-type]
        paths={"store_path": ":memory:", "uploads_dir": tmp_path / "uploads",  # type: ignore[arg-type]
               "collections_dir": tmp_path / "collections"})
    async with compose.open_runtime(settings, Settings(_env_file=None),  # type: ignore[call-arg]
                                    retrievers={"small_fast": StubRetriever(PASSAGES)}) as rt:
        transport = httpx.ASGITransport(app=rt.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/ask", json={"request_id": "r1", "question": "What is d_model?",
                                            "retrieval_setting": "small_fast"})
            assert rt.request_log is not None
            await rt.request_log.flush()
            health = (await client.get("/healthz")).json()
    assert health["trace_sink"] == {"written": 1, "failed": 0, "dropped": 0, "pending": 0}


# 3 --------------------------------------------------------------------------------------
async def test_aclose_returns_when_the_writer_task_has_died(store: Store) -> None:
    sink = RequestLogSink(store.request_log)
    await sink.start()
    assert sink._task is not None
    sink._task.cancel()  # the writer dies
    await asyncio.sleep(0)
    sink.append_trace(request_record())
    await asyncio.wait_for(sink.aclose(), timeout=2)  # must not hang
    assert sink.dropped == 1 and sink.written == 0


async def test_aclose_is_bounded_and_counts_what_it_could_not_write(store: Store) -> None:
    never = asyncio.Event()

    class StuckRepo:
        async def insert(self, **_: Any) -> None:
            await never.wait()

    sink = RequestLogSink(StuckRepo(), flush_timeout_s=0.1)  # type: ignore[arg-type]
    await sink.start()
    for _ in range(3):
        sink.append_trace(request_record())
    await asyncio.wait_for(sink.aclose(), timeout=2)
    assert sink.dropped == 3 and sink.written == 0


async def test_records_after_close_are_counted_as_dropped(store: Store) -> None:
    sink = RequestLogSink(store.request_log)
    await sink.start()
    await sink.aclose()
    sink.append_trace(request_record())
    assert sink.dropped == 1


# 4 --------------------------------------------------------------------------------------
async def test_append_trace_never_raises_on_a_malformed_record(store: Store) -> None:
    sink = RequestLogSink(store.request_log)
    sink.append_trace({"record": "request", "final": 5})  # not a mapping
    assert sink.failed == 1


# 5 --------------------------------------------------------------------------------------
async def test_a_failing_rollback_does_not_mask_the_original_error(
        store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_rollback() -> None:
        raise aiosqlite.OperationalError("disk I/O error")

    monkeypatch.setattr(store.conn, "rollback", broken_rollback)
    with pytest.raises(ForeignKeyViolationError):
        await store.collections.create("no-such-tenant", "x")


# 6 --------------------------------------------------------------------------------------
async def test_sqlite_errors_during_migration_become_store_open_errors(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def locked(conn: Any) -> Any:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(store_module, "migrate", locked)
    with pytest.raises(StoreOpenError, match="database is locked"):
        async with open_store(":memory:"):
            pytest.fail("must not open")


def test_doctor_reports_a_migration_failure_instead_of_crashing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def locked(conn: Any) -> Any:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(store_module, "migrate", locked)
    settings = RelaySettings(cache={"mode": "sqlite"},  # type: ignore[arg-type]
                             paths={"store_path": ":memory:"})  # type: ignore[arg-type]
    lines: list[str] = []
    code = cli.doctor(settings, Settings(_env_file=None), "t", out=lines.append)  # type: ignore[call-arg]
    text = "\n".join(lines)
    assert code == cli.EXIT_FAILED and "[FAIL] store:" in text and "database is locked" in text


# 7/8 ------------------------------------------------------------------------------------
def test_doctor_warns_when_it_creates_a_new_store_and_explains_sqlite_mode(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_probe(path: Path) -> tuple[int, str, bool]:
        return 1, "wal", True  # the file did not exist before the check

    monkeypatch.setattr(cli, "_probe_store", fake_probe)
    settings = RelaySettings(cache={"mode": "sqlite"})  # type: ignore[arg-type]
    lines: list[str] = []
    cli.doctor(settings, Settings(_env_file=None), "t", out=lines.append)  # type: ignore[call-arg]
    text = "\n".join(lines)
    assert "[WARN] store:" in text and "created a new, empty store" in text
    assert "answers cached in memory; request log in the store" in text


# Found in the end-to-end run: Ctrl+Break killed `relay serve` on Windows (exit 3), so the
# request log's shutdown flush never ran.
def test_serve_turns_ctrl_break_into_a_graceful_shutdown() -> None:
    import signal

    if not hasattr(signal, "SIGBREAK"):
        pytest.skip("SIGBREAK exists only on Windows")

    class FakeServer:
        should_exit = False

    server = FakeServer()
    previous = cli._install_break_handler(server)
    try:
        handler = signal.getsignal(signal.SIGBREAK)
        assert callable(handler)
        handler(signal.SIGBREAK, None)  # call it directly; never raise the real signal
        assert server.should_exit is True
    finally:
        signal.signal(signal.SIGBREAK, previous)
