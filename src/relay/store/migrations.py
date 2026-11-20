"""Versioned migrations for the metadata store (plan V2-02).

Migration 1 is ``schema.sql``. The applied version lives in a one-row ``_schema_version``
table. ``migrate`` applies every migration newer than the database's version, each in its
own transaction together with its version bump, so a failing migration leaves no partial
tables behind. Running ``migrate`` again on an up-to-date database changes nothing.

All I/O goes through ``aiosqlite``, which runs SQLite on its own thread, off the event
loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import aiosqlite

from relay.errors import RelayError

SCHEMA_FILE: Final = Path(__file__).with_name("schema.sql")
VERSION_TABLE: Final = "_schema_version"


class MigrationError(RelayError):
    """The database cannot be brought to the schema this code expects."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationResult:
    from_version: int
    to_version: int
    applied: tuple[int, ...]


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "initial schema", SCHEMA_FILE.read_text(encoding="utf-8")),
    # V2-03 (owner, 2026-11-18): correlate log rows with requests; per-collection rerank.
    Migration(2, "request_id and rerank", (
        "ALTER TABLE request_log ADD COLUMN request_id TEXT;\n"
        "ALTER TABLE collection ADD COLUMN rerank INT DEFAULT 0;\n"
        "CREATE INDEX idx_request_log_request_id ON request_log(request_id);\n"
        "CREATE INDEX idx_document_sha256 ON document(collection_id, sha256);")),
    # V2-04 (owner, 2026-11-19): each document's scope cues, as JSON (DocumentCues).
    Migration(3, "document cues", "ALTER TABLE document ADD COLUMN cues TEXT;"),
    # V2-05 (owner prompt): a collection can switch POST /v1/ask back to buffered mode.
    Migration(4, "collection buffered",
              "ALTER TABLE collection ADD COLUMN buffered INT DEFAULT 0;"),
    # V2-07 (owner prompt): per-collection switch for the advisory L4 entailment score.
    Migration(5, "collection l4_enabled",
              "ALTER TABLE collection ADD COLUMN l4_enabled INT DEFAULT 1;"),
)
LATEST_VERSION: Final = MIGRATIONS[-1].version

_VERSION_DDL: Final = (
    f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
    "id INTEGER PRIMARY KEY CHECK (id = 1), "
    "version INTEGER NOT NULL, "
    "applied_at TEXT NOT NULL)"
)


MEMORY: Final = ":memory:"
BUSY_TIMEOUT_MS: Final = 5000


async def open_connection(path: str | Path) -> aiosqlite.Connection:
    """Open a connection with the store's settings.

    Every connection enforces foreign keys and waits up to 5 s on a locked database. A file
    database uses WAL journalling (readers do not block the writer) with
    ``synchronous=NORMAL``. ``:memory:`` has no journal file, so WAL does not apply.
    """
    conn = await aiosqlite.connect(str(path))
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if str(path) != MEMORY:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
        async with conn.execute("PRAGMA foreign_keys") as cursor:
            row = await cursor.fetchone()
        if row is None or int(row[0]) != 1:
            raise MigrationError("this SQLite build cannot enforce foreign keys")
    except BaseException:
        await conn.close()
        raise
    return conn


async def journal_mode(conn: aiosqlite.Connection) -> str:
    async with conn.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
    return "" if row is None else str(row[0]).lower()


async def schema_version(conn: aiosqlite.Connection) -> int:
    """The applied schema version; 0 for an empty database."""
    if VERSION_TABLE not in await table_names(conn):
        return 0
    async with conn.execute(f"SELECT version FROM {VERSION_TABLE} WHERE id = 1") as cursor:
        row = await cursor.fetchone()
    return 0 if row is None else int(row[0])


async def table_names(conn: aiosqlite.Connection) -> list[str]:
    """User tables, sorted (SQLite's internal tables excluded)."""
    async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name") as cursor:
        return [str(row[0]) for row in await cursor.fetchall()]


async def row_counts(conn: aiosqlite.Connection) -> dict[str, int]:
    """Row count per user table (table names come from sqlite_master, not from input)."""
    counts: dict[str, int] = {}
    for name in await table_names(conn):
        async with conn.execute(f'SELECT COUNT(*) FROM "{name}"') as cursor:
            row = await cursor.fetchone()
        counts[name] = 0 if row is None else int(row[0])
    return counts


def _check_sequence(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migrations must be numbered 1..n without gaps, got {versions}")


async def migrate(conn: aiosqlite.Connection, *,
                  migrations: Sequence[Migration] = MIGRATIONS) -> MigrationResult:
    """Bring the database up to the latest version. Idempotent."""
    _check_sequence(migrations)
    await conn.execute(_VERSION_DDL)
    await conn.commit()
    current = await schema_version(conn)
    latest = migrations[-1].version
    if current > latest:
        raise MigrationError(f"database schema version {current} is newer than this code "
                             f"(latest known: {latest}); upgrade The Relay")
    applied: list[int] = []
    for migration in migrations:
        if migration.version <= current:
            continue
        await _apply(conn, migration)
        applied.append(migration.version)
    return MigrationResult(from_version=current, to_version=max(current, latest),
                           applied=tuple(applied))


async def _apply(conn: aiosqlite.Connection, migration: Migration) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    script = (
        "BEGIN;\n"
        f"{migration.sql}\n;\n"
        f"INSERT INTO {VERSION_TABLE} (id, version, applied_at) "
        f"VALUES (1, {migration.version}, '{stamp}') "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
        "applied_at = excluded.applied_at;\n"
        "COMMIT;"
    )
    try:
        await conn.executescript(script)
    except aiosqlite.Error as exc:
        error = MigrationError(f"migration {migration.version} ({migration.name}) failed: "
                               f"{type(exc).__name__}: {exc}")
        if conn.in_transaction:
            try:
                await conn.rollback()
            except aiosqlite.Error as rollback_exc:
                error.add_note(f"rollback also failed: {type(rollback_exc).__name__}")
        raise error from None


__all__ = ["LATEST_VERSION", "MEMORY", "MIGRATIONS", "Migration", "MigrationError",
           "MigrationResult", "journal_mode", "migrate", "open_connection", "row_counts",
           "schema_version", "table_names"]
