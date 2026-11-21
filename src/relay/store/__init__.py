"""Metadata store (v2): SQLite in WAL mode behind repositories (plan V2-02).

    async with open_store("data/relay.sqlite3") as store:
        tenant = await store.tenants.create("acme")

``open_store`` checks the path, opens one ``aiosqlite`` connection with the store's
pragmas, applies migrations and yields a :class:`Store` that holds the connection and
every repository. The connection is closed on exit. ``":memory:"`` gives a private,
throw-away database (used by the tests).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from relay.errors import ConfigurationError, sanitize_error
from relay.store.migrations import (
    MEMORY,
    MigrationResult,
    journal_mode,
    migrate,
    open_connection,
)
from relay.store.repositories import (
    ApiKeyRepo,
    CollectionRepo,
    ConversationRepo,
    DocumentRepo,
    IngestJobRepo,
    RequestLogRepo,
    StoreError,
    TenantRepo,
)

logger = logging.getLogger(__name__)


class StoreOpenError(StoreError, ConfigurationError):
    """The store could not be opened (bad path, unreadable file). Raised before startup."""


@dataclass(frozen=True)
class Store:
    """One connection and every repository over it."""

    path: str
    conn: aiosqlite.Connection
    migration: MigrationResult
    journal_mode: str
    tenants: TenantRepo
    collections: CollectionRepo
    documents: DocumentRepo
    ingest_jobs: IngestJobRepo
    request_log: RequestLogRepo
    conversations: ConversationRepo
    api_keys: ApiKeyRepo  # V2-10

    @property
    def schema_version(self) -> int:
        return self.migration.to_version


def _check_path(path: str | Path) -> str:
    text = str(path)
    if text == MEMORY:
        return text
    file = Path(text)
    if file.is_dir():
        raise StoreOpenError(f"store path {file} is a directory; give a file path such as "
                             f"{file / 'relay.sqlite3'}")
    if not file.parent.is_dir():
        raise StoreOpenError(f"cannot open store {file}: directory {file.parent} does not "
                             "exist; create it or change paths.store_path")
    return text


@asynccontextmanager
async def open_store(path: str | Path) -> AsyncGenerator[Store]:
    """Open, migrate and yield the store; close it on exit.

    Every SQLite failure while opening or migrating (unreadable, read-only, locked or
    corrupt file) becomes a ``StoreOpenError`` carrying SQLite's own message, so the CLI
    reports it cleanly before the app starts.
    """
    target = _check_path(path)
    try:
        conn = await open_connection(target)
    except (aiosqlite.Error, OSError) as exc:
        raise StoreOpenError(f"cannot open store {target}: {type(exc).__name__}: "
                             f"{exc}") from None
    try:
        try:
            result = await migrate(conn)
            mode = await journal_mode(conn)
        except aiosqlite.Error as exc:
            raise StoreOpenError(f"cannot open store {target}: {type(exc).__name__}: "
                                 f"{exc}") from None
        yield Store(path=target, conn=conn, migration=result, journal_mode=mode,
                    tenants=TenantRepo(conn), collections=CollectionRepo(conn),
                    documents=DocumentRepo(conn), ingest_jobs=IngestJobRepo(conn),
                    request_log=RequestLogRepo(conn), conversations=ConversationRepo(conn),
                    api_keys=ApiKeyRepo(conn))
    finally:
        try:
            await conn.close()
        except aiosqlite.Error as exc:  # never let a failed close mask the real outcome
            logger.error("closing store %s failed: %s", target, sanitize_error(exc))


__all__ = ["Store", "StoreError", "StoreOpenError", "open_store"]
