"""V2-02 tests: repositories. Every test gets a fresh, migrated in-memory database."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator
from dataclasses import dataclass, is_dataclass

import aiosqlite
import pytest

from relay.store import open_store
from relay.store.repositories import (
    CollectionRepo,
    ConversationRepo,
    DocumentRepo,
    ForeignKeyViolationError,
    IngestJobRepo,
    InvalidValueError,
    NotFoundError,
    RequestLogRepo,
    StoreError,
    TenantRepo,
)


@dataclass
class Repos:
    conn: aiosqlite.Connection
    tenants: TenantRepo
    collections: CollectionRepo
    documents: DocumentRepo
    jobs: IngestJobRepo
    log: RequestLogRepo
    conversations: ConversationRepo


@pytest.fixture
async def repos() -> AsyncGenerator[Repos]:
    """A fresh in-memory Store per test."""
    async with open_store(":memory:") as store:
        yield Repos(store.conn, store.tenants, store.collections, store.documents,
                    store.ingest_jobs, store.request_log, store.conversations)


async def tenant_and_collection(r: Repos) -> tuple[str, str]:
    tenant = await r.tenants.create("acme")
    collection = await r.collections.create(tenant.id, "papers")
    return tenant.id, collection.id


async def test_tenants_are_created_and_read_back_as_dataclasses(repos: Repos) -> None:
    tenant = await repos.tenants.create("acme")
    assert is_dataclass(tenant) and tenant.name == "acme" and len(tenant.id) == 32
    assert tenant.created_at.endswith("+00:00")
    assert await repos.tenants.get_by_id(tenant.id) == tenant
    assert await repos.tenants.get_by_id("missing") is None


async def test_collections_list_per_tenant_and_bump_their_version(repos: Repos) -> None:
    tenant_id, collection_id = await tenant_and_collection(repos)
    other = await repos.tenants.create("other")
    await repos.collections.create(other.id, "theirs")
    second = await repos.collections.create(tenant_id, "notes", retrieval_preset="large_wide")
    listed = await repos.collections.list_for_tenant(tenant_id)
    assert [c.id for c in listed] == [collection_id, second.id]
    assert listed[0].version == 0 and listed[0].retrieval_preset == "small_fast"
    assert await repos.collections.bump_version(collection_id) == 1
    assert await repos.collections.bump_version(collection_id) == 2
    fetched = await repos.collections.get_by_id(collection_id)
    assert fetched is not None and fetched.version == 2
    with pytest.raises(NotFoundError):
        await repos.collections.bump_version("missing")


async def test_document_status_persists_across_repository_instances(repos: Repos) -> None:
    _, collection_id = await tenant_and_collection(repos)
    doc = await repos.documents.create(collection_id, source="upload", sha256="a" * 64,
                                       title="Paper", pages=12)
    assert doc.status == "pending"
    updated = await repos.documents.update_status(doc.id, "ready")
    assert updated.status == "ready"
    fresh = DocumentRepo(repos.conn)  # a new repository on the same connection
    reread = await fresh.get_by_id(doc.id)
    assert reread is not None and reread.status == "ready" and reread.pages == 12
    assert [d.id for d in await fresh.list_for_collection(collection_id)] == [doc.id]
    with pytest.raises(InvalidValueError):
        await repos.documents.update_status(doc.id, "exploded")
    with pytest.raises(NotFoundError):
        await repos.documents.update_status("missing", "ready")


async def test_ingest_jobs_track_state_attempts_and_pending(repos: Repos) -> None:
    _, collection_id = await tenant_and_collection(repos)
    doc = await repos.documents.create(collection_id, source="arxiv", sha256="b" * 64)
    job = await repos.jobs.create(doc.id)
    done = await repos.jobs.create(doc.id)
    assert (job.state, job.attempts, job.error_class) == ("queued", 0, None)
    parsing = await repos.jobs.update_state(job.id, "parsing", increment_attempts=True)
    assert (parsing.state, parsing.attempts) == ("parsing", 1)
    await repos.jobs.update_state(done.id, "failed", error_class="ParseError")
    failed = await repos.jobs.get_by_id(done.id)
    assert failed is not None and failed.error_class == "ParseError"
    assert [j.id for j in await repos.jobs.list_pending()] == [job.id]
    with pytest.raises(InvalidValueError):
        await repos.jobs.update_state(job.id, "teleported")


async def test_request_log_insert_writes_every_column_without_truncation(repos: Repos) -> None:
    entry = await repos.log.insert(
        status="ok", question_hash="f" * 64, ttft_ms=1234.567891, total_ms=98765.4321,
        tokens_in=2**40, tokens_out=1_234_567, cache_hit=True, coalesced=False,
        verdict="REFUSAL_VERIFIED", collection_id="c" * 40, tenant_id="t" * 40)
    stored = await repos.log.get_by_id(entry.id)
    assert stored == entry
    assert stored is not None
    assert (stored.question_hash, stored.ttft_ms, stored.total_ms) == ("f" * 64, 1234.567891,
                                                                      98765.4321)
    assert (stored.tokens_in, stored.tokens_out) == (2**40, 1_234_567)
    assert (stored.cache_hit, stored.coalesced, stored.verdict) == (True, False,
                                                                    "REFUSAL_VERIFIED")
    assert (stored.collection_id, stored.tenant_id) == ("c" * 40, "t" * 40)
    minimal = await repos.log.insert(status="timeout")
    assert (minimal.tokens_in, minimal.ttft_ms, minimal.cache_hit) == (None, None, False)
    assert [e.id for e in await repos.log.list_recent(10)] == [minimal.id, entry.id]


@pytest.mark.parametrize("case", ["collection", "document", "job", "message"])
async def test_foreign_key_violations_raise_the_application_exception(
        repos: Repos, case: str) -> None:
    with pytest.raises(ForeignKeyViolationError) as caught:
        if case == "collection":
            await repos.collections.create("no-such-tenant", "x")
        elif case == "document":
            await repos.documents.create("no-such-collection", source="upload", sha256="c")
        elif case == "job":
            await repos.jobs.create("no-such-document")
        else:
            await repos.conversations.append_message("no-such-conversation", "user", "hi")
    assert isinstance(caught.value, StoreError)
    assert not isinstance(caught.value, sqlite3.Error)
    assert caught.value.__cause__ is None or not isinstance(caught.value.__cause__, StoreError)
    assert "does not exist" in str(caught.value)


async def test_conversations_keep_messages_in_order(repos: Repos) -> None:
    _, collection_id = await tenant_and_collection(repos)
    conversation = await repos.conversations.create(collection_id)
    assert await repos.conversations.get_by_id(conversation.id) == conversation
    for role, text in (("user", "first"), ("assistant", "second"), ("user", "third")):
        await repos.conversations.append_message(conversation.id, role, text)
    messages = await repos.conversations.list_messages(conversation.id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "first"), ("assistant", "second"), ("user", "third")]
    with pytest.raises(InvalidValueError):
        await repos.conversations.append_message(conversation.id, "robot", "x")


async def test_a_failed_write_leaves_the_connection_usable(repos: Repos) -> None:
    with pytest.raises(ForeignKeyViolationError):
        await repos.collections.create("no-such-tenant", "x")
    tenant = await repos.tenants.create("after-failure")  # no transaction left open
    assert await repos.tenants.get_by_id(tenant.id) == tenant
