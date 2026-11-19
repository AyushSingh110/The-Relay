"""V2-05 tests: migration 004 adds ``collection.buffered`` (per-collection buffered mode)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.store import Store, open_store
from relay.store.migrations import LATEST_VERSION, MIGRATIONS
from tests.test_ingestion_routes import AUTH, Api


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as fresh:
        await ensure_dev_tenant(fresh)
        yield fresh


async def test_migration_004_adds_the_buffered_flag_default_off(store: Store) -> None:
    assert LATEST_VERSION >= 4 and MIGRATIONS[3].version == 4
    plain = await store.collections.create(DEV_TENANT_ID, "streaming")
    buffered = await store.collections.create(DEV_TENANT_ID, "buffered", buffered=True)
    assert (plain.buffered, buffered.buffered) == (False, True)
    again = await store.collections.get_by_id(buffered.id)
    assert again is not None and again.buffered is True


async def test_the_collection_route_accepts_and_lists_buffered(store: Store, tmp_path) -> None:  # type: ignore[no-untyped-def]
    api = Api(store, tmp_path)
    async with api.client:
        created = await api.client.post("/v1/collections", headers=AUTH, json={
            "name": "b", "tenant_id": DEV_TENANT_ID, "buffered": True})
        assert created.status_code == 201
        listed = (await api.client.get("/v1/collections", headers=AUTH)).json()["collections"]
    assert listed[0]["buffered"] is True
