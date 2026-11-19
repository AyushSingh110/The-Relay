"""V2-03 tests: migration 002 (request_log.request_id, collection.rerank) and the repository
support added with it. In-memory SQLite only."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from relay.store import Store, open_store
from relay.store.migrations import LATEST_VERSION, MIGRATIONS


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as fresh:
        yield fresh


def test_migration_002_is_the_next_numbered_migration() -> None:
    assert LATEST_VERSION == 2 and MIGRATIONS[1].version == 2
    assert "ALTER TABLE request_log ADD COLUMN request_id TEXT" in MIGRATIONS[1].sql
    assert "ALTER TABLE collection ADD COLUMN rerank INT DEFAULT 0" in MIGRATIONS[1].sql


async def test_request_id_is_stored_with_the_log_entry(store: Store) -> None:
    entry = await store.request_log.insert(status="ok", request_id="req-42")
    stored = await store.request_log.get_by_id(entry.id)
    assert stored is not None and stored.request_id == "req-42"
    assert (await store.request_log.insert(status="ok")).request_id is None


async def test_collections_have_a_rerank_flag_defaulting_to_off(store: Store) -> None:
    tenant = await store.tenants.create("acme")
    plain = await store.collections.create(tenant.id, "a")
    reranked = await store.collections.create(tenant.id, "b", rerank=True)
    assert plain.rerank is False and reranked.rerank is True
    assert (await store.collections.get_by_id(reranked.id)).rerank is True  # type: ignore[union-attr]
    assert await store.collections.find_by_name(tenant.id, "b") == reranked
    assert await store.collections.find_by_name(tenant.id, "zzz") is None


async def test_documents_are_found_by_hash_within_one_collection(store: Store) -> None:
    tenant = await store.tenants.create("acme")
    first = await store.collections.create(tenant.id, "a")
    other = await store.collections.create(tenant.id, "b")
    doc = await store.documents.create(first.id, source="upload", sha256="h" * 64,
                                       status="queued")
    assert doc.status == "queued"
    assert await store.documents.find_by_sha256(first.id, "h" * 64) == doc
    assert await store.documents.find_by_sha256(other.id, "h" * 64) is None


async def test_runnable_jobs_include_pending_and_retryable_failures(store: Store) -> None:
    tenant = await store.tenants.create("acme")
    collection = await store.collections.create(tenant.id, "a")
    doc = await store.documents.create(collection.id, source="upload", sha256="x")
    queued = await store.ingest_jobs.create(doc.id)
    retry = await store.ingest_jobs.create(doc.id)
    await store.ingest_jobs.update_state(retry.id, "failed", error_class="E",
                                         increment_attempts=True)
    exhausted = await store.ingest_jobs.create(doc.id)
    for _ in range(3):
        await store.ingest_jobs.update_state(exhausted.id, "failed", error_class="E",
                                             increment_attempts=True)
    done = await store.ingest_jobs.create(doc.id)
    await store.ingest_jobs.update_state(done.id, "ready")
    runnable = [j.id for j in await store.ingest_jobs.list_runnable(max_attempts=3)]
    assert runnable == [queued.id, retry.id]
    latest = await store.ingest_jobs.latest_for_document(doc.id)
    assert latest is not None and latest.id == done.id
