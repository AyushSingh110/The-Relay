"""Repositories over the metadata store (plan V2-02; docs/ARCHITECTURE_V2.md §5).

Rules:

- every method is async; SQLite runs on ``aiosqlite``'s own thread, off the event loop;
- raw SQL lives only here and in ``migrations.py``;
- methods return frozen dataclasses, never ``sqlite3.Row`` objects;
- database integrity failures become application exceptions
  (``ForeignKeyViolationError``, ``IntegrityViolationError``); a missing row on update is
  ``NotFoundError``. Each write is committed on success and rolled back on failure, so a
  failed write never leaves a transaction open;
- all repositories on one connection share **one write lock**, held from a write's
  statement to its commit or rollback. Without it, one coroutine's rollback could undo
  another coroutine's statement that had run but not yet committed (found in review);
- if a rollback itself fails, that is logged and noted on the original error, which is
  the one raised.

Ids are 32-character hex UUIDs unless the caller supplies one. Timestamps are ISO-8601 UTC
with milliseconds.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import aiosqlite

from relay.errors import RelayError, sanitize_error

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
Params = Sequence[Any]

DOCUMENT_STATUSES: Final = frozenset({"pending", "queued", "ingesting", "ready", "failed"})
INGEST_STATES: Final = ("queued", "parsing", "embedding", "indexing", "ready", "failed")
PENDING_STATES: Final = ("queued", "parsing", "embedding", "indexing")
MESSAGE_ROLES: Final = frozenset({"user", "assistant", "system"})


# --- Errors ----------------------------------------------------------------------------


class StoreError(RelayError):
    """Base class for metadata-store failures."""


class NotFoundError(StoreError):
    """The row to update does not exist."""


class ForeignKeyViolationError(StoreError):
    """A write referenced a row that does not exist."""


class IntegrityViolationError(StoreError):
    """A uniqueness or other integrity constraint was violated."""


class InvalidValueError(StoreError):
    """A value outside the allowed set (status, state, role) was given."""


# --- Records ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tenant:
    id: str
    name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Collection:
    id: str
    tenant_id: str
    name: str
    retrieval_preset: str
    version: int
    created_at: str
    rerank: bool = False
    buffered: bool = False  # V2-05: /v1/ask answers in one piece (migration 004)


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    collection_id: str
    source: str
    sha256: str
    title: str | None
    pages: int | None
    status: str
    created_at: str
    cues: str | None = None  # JSON of relay.retrieval.cues.DocumentCues (migration 003)


@dataclass(frozen=True, slots=True)
class IngestJob:
    id: str
    document_id: str
    state: str
    attempts: int
    error_class: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    collection_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RequestLogEntry:
    id: str
    request_id: str | None
    collection_id: str | None
    tenant_id: str | None
    question_hash: str | None
    status: str
    ttft_ms: float | None
    total_ms: float | None
    tokens_in: int | None
    tokens_out: int | None
    cache_hit: bool
    coalesced: bool
    verdict: str | None
    created_at: str


# --- Base ------------------------------------------------------------------------------


_WRITE_LOCKS: weakref.WeakKeyDictionary[aiosqlite.Connection, asyncio.Lock] = (
    weakref.WeakKeyDictionary())


def write_lock(conn: aiosqlite.Connection) -> asyncio.Lock:
    """The single write lock shared by every repository on ``conn``."""
    lock = _WRITE_LOCKS.get(conn)
    if lock is None:
        lock = _WRITE_LOCKS[conn] = asyncio.Lock()
    return lock


async def safe_rollback(conn: aiosqlite.Connection, original: BaseException) -> None:
    """Roll back; if that fails too, log it and note it on ``original`` (never mask it)."""
    try:
        await conn.rollback()
    except aiosqlite.Error as exc:
        note = f"rollback after this error also failed: {sanitize_error(exc)}"
        original.add_note(note)
        logger.error("store %s", note)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def _check(value: str, allowed: frozenset[str] | tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise InvalidValueError(f"{what} {value!r} is not one of {sorted(allowed)}")


class _Repo:
    def __init__(self, conn: aiosqlite.Connection, *, clock: Clock = utc_now,
                 ids: IdFactory = new_id) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat(timespec="milliseconds")

    async def _write(self, sql: str, params: Params, *, context: str) -> int:
        """Execute one write and commit under the connection's write lock; returns the
        changed row count."""
        return (await self._write_many([(sql, params)], context=context))[0]

    async def _write_many(self, statements: Sequence[tuple[str, Params]], *,
                          context: str) -> list[int]:
        """Execute several writes in ONE transaction (all or nothing) under the write lock;
        returns each statement's changed row count."""
        async with write_lock(self._conn):
            try:
                changed: list[int] = []
                for sql, params in statements:
                    cursor = await self._conn.execute(sql, params)
                    changed.append(cursor.rowcount)
                    await cursor.close()
                await self._conn.commit()
            except aiosqlite.IntegrityError as exc:
                message = str(exc)
                if "FOREIGN KEY" in message.upper():
                    mapped: StoreError = ForeignKeyViolationError(
                        f"{context}: a referenced row does not exist")
                else:
                    mapped = IntegrityViolationError(f"{context}: {message}")
                await safe_rollback(self._conn, mapped)
                raise mapped from None
            except aiosqlite.Error as exc:
                await safe_rollback(self._conn, exc)
                raise
        return changed

    async def _one(self, sql: str, params: Params) -> tuple[Any, ...] | None:
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return None if row is None else tuple(row)

    async def _all(self, sql: str, params: Params) -> list[tuple[Any, ...]]:
        async with self._conn.execute(sql, params) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


# --- Repositories ----------------------------------------------------------------------


class TenantRepo(_Repo):
    async def create(self, name: str, *, tenant_id: str | None = None) -> Tenant:
        tenant = Tenant(id=tenant_id or self._ids(), name=name, created_at=self._now())
        await self._write("INSERT INTO tenant (id, name, created_at) VALUES (?, ?, ?)",
                          (tenant.id, tenant.name, tenant.created_at),
                          context="create tenant")
        return tenant

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        row = await self._one("SELECT id, name, created_at FROM tenant WHERE id = ?",
                              (tenant_id,))
        return None if row is None else Tenant(*row)


_COLLECTION_COLUMNS: Final = ("id, tenant_id, name, retrieval_preset, version, created_at, "
                              "rerank, buffered")


def _collection(row: tuple[Any, ...]) -> Collection:
    values = list(row)
    values[6] = bool(values[6])  # rerank is stored as 0/1 (NULL on pre-002 rows -> False)
    values[7] = bool(values[7])  # buffered: 0/1 (NULL on pre-004 rows -> False)
    return Collection(*values)


class CollectionRepo(_Repo):
    async def create(self, tenant_id: str, name: str, *, retrieval_preset: str = "small_fast",
                     rerank: bool = False, collection_id: str | None = None,
                     buffered: bool = False) -> Collection:
        collection = Collection(id=collection_id or self._ids(), tenant_id=tenant_id,
                                name=name, retrieval_preset=retrieval_preset, version=0,
                                created_at=self._now(), rerank=rerank, buffered=buffered)
        await self._write(
            f"INSERT INTO collection ({_COLLECTION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (collection.id, tenant_id, name, retrieval_preset, 0, collection.created_at,
             int(rerank), int(buffered)),
            context=f"create collection: tenant {tenant_id!r} does not exist")
        return collection

    async def get_by_id(self, collection_id: str) -> Collection | None:
        row = await self._one(f"SELECT {_COLLECTION_COLUMNS} FROM collection WHERE id = ?",
                              (collection_id,))
        return None if row is None else _collection(row)

    async def find_by_name(self, tenant_id: str, name: str) -> Collection | None:
        row = await self._one(f"SELECT {_COLLECTION_COLUMNS} FROM collection "
                              "WHERE tenant_id = ? AND name = ? ORDER BY rowid LIMIT 1",
                              (tenant_id, name))
        return None if row is None else _collection(row)

    async def list_for_tenant(self, tenant_id: str) -> list[Collection]:
        rows = await self._all(f"SELECT {_COLLECTION_COLUMNS} FROM collection "
                               "WHERE tenant_id = ? ORDER BY created_at, rowid", (tenant_id,))
        return [_collection(row) for row in rows]

    async def bump_version(self, collection_id: str) -> int:
        """Increment the collection's version and return the new value."""
        async with write_lock(self._conn):
            try:
                async with self._conn.execute(
                        "UPDATE collection SET version = version + 1 WHERE id = ? "
                        "RETURNING version", (collection_id,)) as cursor:
                    row = await cursor.fetchone()
                await self._conn.commit()
            except aiosqlite.Error as exc:
                await safe_rollback(self._conn, exc)
                raise
        if row is None:
            raise NotFoundError(f"collection {collection_id!r} does not exist")
        return int(row[0])


_DOCUMENT_COLUMNS: Final = ("id, collection_id, source, sha256, title, pages, status, "
                             "created_at, cues")


class DocumentRepo(_Repo):
    async def create(self, collection_id: str, *, source: str, sha256: str,
                     title: str | None = None, pages: int | None = None,
                     status: str = "pending", document_id: str | None = None,
                     cues: str | None = None) -> Document:
        _check(status, DOCUMENT_STATUSES, "document status")
        document = Document(id=document_id or self._ids(), collection_id=collection_id,
                            source=source, sha256=sha256, title=title, pages=pages,
                            status=status, created_at=self._now(), cues=cues)
        await self._write(
            f"INSERT INTO document ({_DOCUMENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (document.id, collection_id, source, sha256, title, pages, status,
             document.created_at, cues),
            context=f"create document: collection {collection_id!r} does not exist")
        return document

    async def create_queued(self, collection_id: str, *, source: str, sha256: str,
                            title: str | None = None, pages: int | None = None,
                            document_id: str | None = None,
                            job_id: str | None = None) -> tuple[Document, IngestJob]:
        """A ``queued`` document and its ingest job, written in one transaction."""
        now = self._now()
        document = Document(id=document_id or self._ids(), collection_id=collection_id,
                            source=source, sha256=sha256, title=title, pages=pages,
                            status="queued", created_at=now)
        job = IngestJob(id=job_id or self._ids(), document_id=document.id, state="queued",
                        attempts=0, error_class=None, updated_at=now)
        await self._write_many([
            (f"INSERT INTO document ({_DOCUMENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (document.id, collection_id, source, sha256, title, pages, "queued", now, None)),
            (f"INSERT INTO ingest_job ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
             (job.id, document.id, "queued", 0, None, now)),
        ], context=f"queue document: collection {collection_id!r} does not exist")
        return document, job

    async def requeue(self, document_id: str) -> IngestJob:
        """Queue a document again (status ``queued`` plus a fresh job), in one transaction."""
        now = self._now()
        job = IngestJob(id=self._ids(), document_id=document_id, state="queued", attempts=0,
                        error_class=None, updated_at=now)
        changed = await self._write_many([
            ("UPDATE document SET status = 'queued' WHERE id = ?", (document_id,)),
            (f"INSERT INTO ingest_job ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
             (job.id, document_id, "queued", 0, None, now)),
        ], context=f"requeue document: document {document_id!r} does not exist")
        if changed[0] != 1:
            raise NotFoundError(f"document {document_id!r} does not exist")
        return job

    async def get_by_id(self, document_id: str) -> Document | None:
        row = await self._one(f"SELECT {_DOCUMENT_COLUMNS} FROM document WHERE id = ?",
                              (document_id,))
        return None if row is None else Document(*row)

    async def find_by_sha256(self, collection_id: str, sha256: str) -> Document | None:
        """The document with this content hash in this collection, if any (de-duplication)."""
        row = await self._one(f"SELECT {_DOCUMENT_COLUMNS} FROM document "
                              "WHERE collection_id = ? AND sha256 = ? ORDER BY rowid LIMIT 1",
                              (collection_id, sha256))
        return None if row is None else Document(*row)

    async def list_for_collection(self, collection_id: str) -> list[Document]:
        rows = await self._all(f"SELECT {_DOCUMENT_COLUMNS} FROM document "
                               "WHERE collection_id = ? ORDER BY created_at, rowid",
                               (collection_id,))
        return [Document(*row) for row in rows]

    async def counts_by_collection(self, collection_ids: Sequence[str]) -> dict[str, int]:
        """Documents per collection, in one query; collections without any are absent."""
        if not collection_ids:
            return {}
        marks = ", ".join("?" for _ in collection_ids)
        rows = await self._all(f"SELECT collection_id, COUNT(*) FROM document "
                               f"WHERE collection_id IN ({marks}) GROUP BY collection_id",
                               tuple(collection_ids))
        return {str(cid): int(n) for cid, n in rows}

    async def set_cues(self, document_id: str, cues: str | None) -> None:
        """Store the document's cues (JSON of ``DocumentCues``), or clear them."""
        changed = await self._write("UPDATE document SET cues = ? WHERE id = ?",
                                    (cues, document_id), context="set document cues")
        if changed != 1:
            raise NotFoundError(f"document {document_id!r} does not exist")

    async def update_status(self, document_id: str, status: str, *,
                            pages: int | None = None) -> Document:
        """Set the status (and the page count, when given)."""
        _check(status, DOCUMENT_STATUSES, "document status")
        changed = await self._write("UPDATE document SET status = ?, pages = COALESCE(?, pages) "
                                    "WHERE id = ?", (status, pages, document_id),
                                    context="update document")
        document = await self.get_by_id(document_id) if changed else None
        if document is None:
            raise NotFoundError(f"document {document_id!r} does not exist")
        return document


_JOB_COLUMNS: Final = "id, document_id, state, attempts, error_class, updated_at"


class IngestJobRepo(_Repo):
    async def create(self, document_id: str, *, state: str = "queued",
                     job_id: str | None = None) -> IngestJob:
        _check(state, INGEST_STATES, "ingest state")
        job = IngestJob(id=job_id or self._ids(), document_id=document_id, state=state,
                        attempts=0, error_class=None, updated_at=self._now())
        await self._write(
            f"INSERT INTO ingest_job ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (job.id, document_id, state, 0, None, job.updated_at),
            context=f"create ingest job: document {document_id!r} does not exist")
        return job

    async def get_by_id(self, job_id: str) -> IngestJob | None:
        row = await self._one(f"SELECT {_JOB_COLUMNS} FROM ingest_job WHERE id = ?", (job_id,))
        return None if row is None else IngestJob(*row)

    async def update_state(self, job_id: str, state: str, *, error_class: str | None = None,
                           increment_attempts: bool = False) -> IngestJob:
        _check(state, INGEST_STATES, "ingest state")
        changed = await self._write(
            "UPDATE ingest_job SET state = ?, error_class = ?, updated_at = ?, "
            "attempts = attempts + ? WHERE id = ?",
            (state, error_class, self._now(), 1 if increment_attempts else 0, job_id),
            context="update ingest job")
        job = await self.get_by_id(job_id) if changed else None
        if job is None:
            raise NotFoundError(f"ingest job {job_id!r} does not exist")
        return job

    async def complete(self, job_id: str, *, document_id: str, collection_id: str,
                       pages: int | None, cues: str | None = None) -> int:
        """Finish an ingest in ONE transaction: the document becomes ``ready``, the job
        ``ready``, and the collection version is bumped (returned). A request therefore
        never sees the new version without the new document, or the reverse."""
        async with write_lock(self._conn):
            try:
                cursor = await self._conn.execute(
                    "UPDATE document SET status = 'ready', pages = COALESCE(?, pages), "
                    "cues = COALESCE(?, cues) WHERE id = ? AND collection_id = ?",
                    (pages, cues, document_id, collection_id))
                documents = cursor.rowcount
                await cursor.close()
                cursor = await self._conn.execute(
                    "UPDATE ingest_job SET state = 'ready', error_class = NULL, updated_at = ? "
                    "WHERE id = ? AND document_id = ?", (self._now(), job_id, document_id))
                jobs = cursor.rowcount
                await cursor.close()
                async with self._conn.execute(
                        "UPDATE collection SET version = version + 1 WHERE id = ? "
                        "RETURNING version", (collection_id,)) as cursor:
                    row = await cursor.fetchone()
                if documents != 1 or jobs != 1 or row is None:
                    raise NotFoundError(f"complete ingest: job {job_id!r}, document "
                                        f"{document_id!r} or collection {collection_id!r} "
                                        "does not exist")
                await self._conn.commit()
            except (aiosqlite.Error, NotFoundError) as exc:
                await safe_rollback(self._conn, exc)
                raise
        return int(row[0])

    async def list_runnable(self, *, max_attempts: int, limit: int = 100) -> list[IngestJob]:
        """Pending jobs, plus failed jobs with attempts left; oldest update first."""
        marks = ", ".join("?" for _ in PENDING_STATES)
        rows = await self._all(f"SELECT {_JOB_COLUMNS} FROM ingest_job "
                               f"WHERE state IN ({marks}) OR (state = 'failed' AND attempts < ?) "
                               "ORDER BY updated_at, rowid LIMIT ?",
                               (*PENDING_STATES, max_attempts, limit))
        return [IngestJob(*row) for row in rows]

    async def latest_for_document(self, document_id: str) -> IngestJob | None:
        row = await self._one(f"SELECT {_JOB_COLUMNS} FROM ingest_job WHERE document_id = ? "
                              "ORDER BY rowid DESC LIMIT 1", (document_id,))
        return None if row is None else IngestJob(*row)

    async def list_pending(self, limit: int = 100) -> list[IngestJob]:
        """Jobs not yet ready or failed, oldest update first."""
        marks = ", ".join("?" for _ in PENDING_STATES)
        rows = await self._all(f"SELECT {_JOB_COLUMNS} FROM ingest_job "
                               f"WHERE state IN ({marks}) ORDER BY updated_at, rowid LIMIT ?",
                               (*PENDING_STATES, limit))
        return [IngestJob(*row) for row in rows]


_LOG_COLUMNS: Final = ("id, request_id, collection_id, tenant_id, question_hash, status, "
                       "ttft_ms, total_ms, tokens_in, tokens_out, cache_hit, coalesced, "
                       "verdict, created_at")


def _log_entry(row: tuple[Any, ...]) -> RequestLogEntry:
    values = list(row)
    values[10], values[11] = bool(values[10]), bool(values[11])
    return RequestLogEntry(*values)


class RequestLogRepo(_Repo):
    async def insert(self, *, status: str, question_hash: str | None = None,
                     ttft_ms: float | None = None, total_ms: float | None = None,
                     tokens_in: int | None = None, tokens_out: int | None = None,
                     cache_hit: bool = False, coalesced: bool = False,
                     verdict: str | None = None, collection_id: str | None = None,
                     tenant_id: str | None = None, entry_id: str | None = None,
                     request_id: str | None = None) -> RequestLogEntry:
        """One row per completed request."""
        entry = RequestLogEntry(
            id=entry_id or self._ids(), request_id=request_id, collection_id=collection_id,
            tenant_id=tenant_id,
            question_hash=question_hash, status=status, ttft_ms=ttft_ms, total_ms=total_ms,
            tokens_in=tokens_in, tokens_out=tokens_out, cache_hit=cache_hit,
            coalesced=coalesced, verdict=verdict, created_at=self._now())
        await self._write(
            f"INSERT INTO request_log ({_LOG_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.id, request_id, collection_id, tenant_id, question_hash, status, ttft_ms,
             total_ms,
             tokens_in, tokens_out, int(cache_hit), int(coalesced), verdict,
             entry.created_at),
            context="insert request log entry")
        return entry

    async def get_by_id(self, entry_id: str) -> RequestLogEntry | None:
        row = await self._one(f"SELECT {_LOG_COLUMNS} FROM request_log WHERE id = ?",
                              (entry_id,))
        return None if row is None else _log_entry(row)

    async def list_recent(self, limit: int = 100) -> list[RequestLogEntry]:
        """Newest first."""
        rows = await self._all(f"SELECT {_LOG_COLUMNS} FROM request_log "
                               "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return [_log_entry(row) for row in rows]


class ConversationRepo(_Repo):
    async def create(self, collection_id: str, *,
                     conversation_id: str | None = None) -> Conversation:
        conversation = Conversation(id=conversation_id or self._ids(),
                                    collection_id=collection_id, created_at=self._now())
        await self._write(
            "INSERT INTO conversation (id, collection_id, created_at) VALUES (?, ?, ?)",
            (conversation.id, collection_id, conversation.created_at),
            context=f"create conversation: collection {collection_id!r} does not exist")
        return conversation

    async def get_by_id(self, conversation_id: str) -> Conversation | None:
        row = await self._one("SELECT id, collection_id, created_at FROM conversation "
                              "WHERE id = ?", (conversation_id,))
        return None if row is None else Conversation(*row)

    async def append_message(self, conversation_id: str, role: str, content: str) -> Message:
        _check(role, MESSAGE_ROLES, "message role")
        message = Message(id=self._ids(), conversation_id=conversation_id, role=role,
                          content=content, created_at=self._now())
        await self._write(
            "INSERT INTO message (id, conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message.id, conversation_id, role, content, message.created_at),
            context=f"append message: conversation {conversation_id!r} does not exist")
        return message

    async def list_messages(self, conversation_id: str) -> list[Message]:
        rows = await self._all("SELECT id, conversation_id, role, content, created_at "
                               "FROM message WHERE conversation_id = ? "
                               "ORDER BY created_at, rowid", (conversation_id,))
        return [Message(*row) for row in rows]


__all__ = [
    "Collection", "CollectionRepo", "Conversation", "ConversationRepo", "Document",
    "DocumentRepo", "ForeignKeyViolationError", "IngestJob", "IngestJobRepo",
    "IntegrityViolationError", "InvalidValueError", "Message", "NotFoundError",
    "RequestLogEntry", "RequestLogRepo", "StoreError", "Tenant", "TenantRepo",
]
