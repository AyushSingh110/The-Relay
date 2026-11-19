"""V2-02 tests: the store wired into the service and into ``relay doctor``.

Every store here is ``:memory:``. The embedded stand-in runs on 127.0.0.1; retrieval is a
scripted stub; no API key is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay import cli
from relay.app import compose
from relay.app.request_log import RequestLogSink
from relay.app.settings import LaneSettings, RelaySettings
from relay.config import Settings
from relay.store import StoreOpenError, open_store
from relay.store.migrations import LATEST_VERSION
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever
from tests.test_standin import MODEL, profile

RETRIEVERS = {"small_fast": StubRetriever(PASSAGES)}
HASH = "e" * 64


def env() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def settings_for(tmp_path: Path, *, mode: str, store_path: str = ":memory:") -> RelaySettings:
    path = tmp_path / "profile.json"
    path.write_text(profile().model_dump_json(), encoding="utf-8")
    return RelaySettings(
        lanes=(LaneSettings(name="s", kind="standin", model=MODEL, embedded=True,
                            profile=path),),
        retrieval={"settings": ("small_fast",)},  # type: ignore[arg-type]
        cache={"mode": mode},  # type: ignore[arg-type]
        # V2-03: sqlite mode also opens collections; keep their files out of data/
        paths={"store_path": store_path, "uploads_dir": tmp_path / "uploads",  # type: ignore[arg-type]
               "collections_dir": tmp_path / "collections"})


def request_record(**final: Any) -> dict[str, Any]:
    base = {"status": "ok", "cache_status": "miss", "verification_verdict": "VERIFIED",
            "provider_first_token_ms": 412.5, "server_total_ms": 700.25,
            "tokens_in": 900, "tokens_out": 40}
    return {"record": "request", "request_id": "r1", "role": "leader",
            "question_sha256": HASH, "final": {**base, **final}}


# --- Sink ------------------------------------------------------------------------------


async def test_sink_turns_request_records_into_request_log_rows() -> None:
    async with open_store(":memory:") as store:
        sink = RequestLogSink(store.request_log)
        await sink.start()
        sink.append_trace(request_record())
        sink.append_trace(request_record(cache_status="exact_hit", tokens_in=0, tokens_out=0))
        sink.append_trace(request_record(cache_status="coalesced"))
        sink.append_trace({"record": "flight", "final": {"status": "ok"}})  # not a request
        sink.append_trace(request_record(status="timeout", verification_verdict="NOT_RUN",
                                         tokens_in=None, tokens_out=None))
        await sink.aclose()
        rows = list(reversed(await store.request_log.list_recent()))
    assert len(rows) == 4 and sink.written == 4 and sink.failed == sink.dropped == 0
    first = rows[0]
    assert (first.status, first.question_hash, first.ttft_ms, first.total_ms) == (
        "ok", HASH, 412.5, 700.25)
    assert (first.tokens_in, first.tokens_out, first.verdict) == (900, 40, "VERIFIED")
    assert [(r.cache_hit, r.coalesced) for r in rows] == [
        (False, False), (True, False), (False, True), (False, False)]
    assert (rows[3].status, rows[3].tokens_in) == ("timeout", None)


async def test_sink_counts_failed_writes_and_keeps_going() -> None:
    async with open_store(":memory:") as store:
        sink = RequestLogSink(store.request_log)
        await sink.start()
        sink.append_trace(request_record(status=None))  # NOT NULL column: the write fails
        sink.append_trace(request_record())
        await sink.aclose()
        assert (sink.failed, sink.written) == (1, 1)
        assert len(await store.request_log.list_recent()) == 1


async def test_sink_drops_and_counts_when_its_queue_is_full() -> None:
    async with open_store(":memory:") as store:
        sink = RequestLogSink(store.request_log, max_pending=1)
        sink.append_trace(request_record())  # not started: the queue fills up
        sink.append_trace(request_record())
        assert sink.dropped == 1
        await sink.start()
        await sink.aclose()
        assert sink.written == 1


# --- Composition -----------------------------------------------------------------------


async def ask(app: Any, n: int) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        reply = await client.post("/ask", json={
            "request_id": f"req-{n}", "question": "What is d_model in the encoder?",
            "retrieval_setting": "small_fast"})
    assert reply.status_code == 200
    return json.loads(reply.text.split("event: final\ndata: ")[1])


async def test_sqlite_mode_opens_the_store_and_logs_every_request(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, mode="sqlite")
    async with compose.open_runtime(settings, env(), retrievers=RETRIEVERS) as runtime:
        assert runtime.store is not None and runtime.store.schema_version == LATEST_VERSION
        assert runtime.service.cache_mode == "exact"  # the answer cache stays in memory
        first, second = await ask(runtime.app, 1), await ask(runtime.app, 2)
        assert (first["cache_status"], second["cache_status"]) == ("miss", "exact_hit")
        assert runtime.request_log is not None
        await runtime.request_log.flush()
        rows = list(reversed(await runtime.store.request_log.list_recent()))
        assert any(f"store: :memory: (schema v{LATEST_VERSION}" in line
                   for line in runtime.describe())
    assert [(r.status, r.cache_hit, r.verdict) for r in rows] == [
        ("ok", False, "VERIFIED"), ("ok", True, "VERIFIED")]
    assert rows[0].tokens_in and rows[0].question_hash and len(rows[0].question_hash) == 64
    assert rows[1].tokens_in == 0  # a cache hit spends nothing


async def test_memory_mode_has_no_store_and_unchanged_behaviour(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, mode="memory")
    async with compose.open_runtime(settings, env(), retrievers=RETRIEVERS) as runtime:
        assert runtime.store is None and runtime.request_log is None
        assert (await ask(runtime.app, 1))["status"] == "ok"
        assert runtime.service.run_id is None


async def test_an_invalid_store_path_fails_before_the_app_starts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(compose, "serve_on_loopback",
                        lambda app: started.append("standin"))  # must never be reached
    settings = settings_for(tmp_path, mode="sqlite",
                            store_path=str(tmp_path / "missing" / "relay.sqlite3"))
    with pytest.raises(StoreOpenError, match="does not exist"):
        async with compose.open_runtime(settings, env(), retrievers=RETRIEVERS):
            pytest.fail("the runtime must not start")
    assert started == []


# --- doctor ----------------------------------------------------------------------------


def doctor_text(settings: RelaySettings) -> tuple[int, str]:
    lines: list[str] = []
    return cli.doctor(settings, env(), "test", out=lines.append), "\n".join(lines)


def test_doctor_reports_the_store_in_each_mode(tmp_path: Path) -> None:
    code, text = doctor_text(settings_for(tmp_path, mode="memory"))
    assert code == cli.EXIT_OK and "[INFO] store: not used (cache mode memory)" in text
    code, text = doctor_text(settings_for(tmp_path, mode="sqlite"))
    assert code == cli.EXIT_OK
    assert f"[  OK] store: :memory: opened, migrations applied (schema v{LATEST_VERSION}" in text
    bad = settings_for(tmp_path, mode="sqlite",
                       store_path=str(tmp_path / "missing" / "relay.sqlite3"))
    code, text = doctor_text(bad)
    assert code == cli.EXIT_FAILED and "[FAIL] store: cannot open store" in text
    assert "does not exist" in text
