"""V2-02 tests: Store and open_store.

All tests use ``:memory:`` except the two that need a real file by nature: the restart
test (close and reopen) and the WAL check. Those use pytest's ``tmp_path``, which is
deleted automatically and is never ``data/relay.sqlite3`` (owner decision 2026-11-18).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from relay.errors import ConfigurationError
from relay.store import Store, StoreOpenError, open_store
from relay.store.migrations import LATEST_VERSION


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as fresh:
        yield fresh


async def test_open_store_migrates_and_exposes_every_repository(store: Store) -> None:
    assert store.schema_version == LATEST_VERSION
    assert store.migration.applied == (1,)
    assert store.journal_mode == "memory"  # WAL does not apply to :memory:
    tenant = await store.tenants.create("acme")
    collection = await store.collections.create(tenant.id, "papers")
    document = await store.documents.create(collection.id, source="upload", sha256="a")
    job = await store.ingest_jobs.create(document.id)
    conversation = await store.conversations.create(collection.id)
    entry = await store.request_log.insert(status="ok")
    assert (await store.ingest_jobs.get_by_id(job.id)) == job
    assert (await store.conversations.get_by_id(conversation.id)) == conversation
    assert (await store.request_log.get_by_id(entry.id)) == entry


async def test_each_test_gets_a_fresh_store(store: Store) -> None:
    assert await store.request_log.list_recent() == []


@pytest.mark.parametrize("kind", ["missing-directory", "is-a-directory"])
async def test_open_store_with_an_invalid_path_fails_clearly_before_anything_starts(
        tmp_path: Path, kind: str) -> None:
    path = tmp_path / "no-such-dir" / "relay.sqlite3" if kind == "missing-directory" \
        else tmp_path
    with pytest.raises(StoreOpenError, match="store") as caught:
        async with open_store(path):
            pytest.fail("the store must not open")
    assert isinstance(caught.value, ConfigurationError)  # reported cleanly by the CLI
    assert str(path) in str(caught.value)
    assert not (tmp_path / "no-such-dir").exists()  # nothing was created


async def test_a_restart_keeps_the_request_log(tmp_path: Path) -> None:
    path = tmp_path / "relay.sqlite3"
    async with open_store(path) as first:
        assert first.journal_mode == "wal"
        written = await first.request_log.insert(
            status="ok", question_hash="9" * 64, ttft_ms=812.5, total_ms=901.25,
            tokens_in=1513, tokens_out=106, cache_hit=False, coalesced=True,
            verdict="VERIFIED")
        tenant = await first.tenants.create("acme")
        collection = await first.collections.create(tenant.id, "papers")
        await first.collections.bump_version(collection.id)
    async with open_store(path) as second:  # a new connection: a real restart
        assert second.migration.applied == ()  # already migrated, nothing re-applied
        assert await second.request_log.list_recent() == [written]
        reread = await second.collections.get_by_id(collection.id)
        assert reread is not None and reread.version == 1
