"""V2-02 tests: the versioned migration runner. In-memory SQLite only."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import aiosqlite
import pytest

from relay.store.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    migrate,
    row_counts,
    schema_version,
    table_names,
)

TABLES = {"tenant", "api_key", "collection", "document", "chunk", "ingest_job",
          "conversation", "message", "request_log", "eval_set", "eval_question", "grade"}


@pytest.fixture
async def conn() -> AsyncGenerator[aiosqlite.Connection]:
    connection = await aiosqlite.connect(":memory:")
    try:
        yield connection
    finally:
        await connection.close()


async def test_migrations_apply_cleanly_from_empty(conn: aiosqlite.Connection) -> None:
    assert await schema_version(conn) == 0
    result = await migrate(conn)
    everything = tuple(range(1, LATEST_VERSION + 1))
    assert (result.from_version, result.to_version, result.applied) == (0, LATEST_VERSION,
                                                                         everything)
    assert await schema_version(conn) == LATEST_VERSION
    assert TABLES | {"_schema_version"} == set(await table_names(conn))


async def test_migrations_are_idempotent(conn: aiosqlite.Connection) -> None:
    await migrate(conn)
    before = await row_counts(conn)
    second = await migrate(conn)
    assert second.applied == () and second.from_version == second.to_version == LATEST_VERSION
    assert await row_counts(conn) == before
    assert before["_schema_version"] == 1  # exactly one version row


async def test_a_database_newer_than_the_code_is_refused(conn: aiosqlite.Connection) -> None:
    newer = (*MIGRATIONS, Migration(LATEST_VERSION + 1, "future",
                                    "CREATE TABLE future_thing (id TEXT);"))
    await migrate(conn, migrations=newer)
    with pytest.raises(MigrationError, match="newer than this code"):
        await migrate(conn)


async def test_a_failing_migration_rolls_back_completely(conn: aiosqlite.Connection) -> None:
    await migrate(conn)
    broken = (*MIGRATIONS, Migration(LATEST_VERSION + 1, "broken",
                                     "CREATE TABLE half_done (id TEXT); NOT VALID SQL;"))
    with pytest.raises(MigrationError, match=f"migration {LATEST_VERSION + 1}"):
        await migrate(conn, migrations=broken)
    assert await schema_version(conn) == LATEST_VERSION
    assert "half_done" not in await table_names(conn)


def test_migrations_are_numbered_from_one_without_gaps() -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    assert MIGRATIONS[0].name == "initial schema" and "CREATE TABLE tenant" in MIGRATIONS[0].sql
