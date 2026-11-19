"""V2-04 tests: migration 003 stores each document's cues; the registry reads them.

Stores are :memory:; ingestion uses generated PDFs and the fake embedder. No network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from relay.app.bootstrap import (
    DEV_TENANT_ID,
    V1_COLLECTION_ID,
    ensure_dev_tenant,
    register_v1_papers,
)
from relay.ingestion.registry import DocumentRegistry
from relay.ingestion.v1_corpus import V1_CUE_SET, v1_document_cues
from relay.retrieval.cues import DocumentCues
from relay.retrieval.hybrid_index import INDEX_MANIFEST
from relay.store import open_store
from relay.store.migrations import LATEST_VERSION, MIGRATIONS
from tests.test_ingest_worker import Env

PAPERS_DIR = Path("data/papers")


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        yield Env(store, tmp_path)


def registry(env: Env) -> DocumentRegistry:
    return DocumentRegistry(env.store.documents, env.store.collections)


async def test_migration_003_adds_the_document_cues_column(env: Env) -> None:
    assert LATEST_VERSION >= 3 and MIGRATIONS[2].version == 3
    async with env.store.conn.execute("PRAGMA table_info(document)") as cursor:
        columns = [row[1] for row in await cursor.fetchall()]
    assert "cues" in columns


async def test_ingestion_stores_the_derived_cues(env: Env) -> None:
    cid = await env.collection()
    doc_id, _ = await env.upload(cid, ["Basalt is a volcanic rock"],
                                 title="Basalt Atlas: volcanic rocks of the world")
    await env.worker().run_once()
    document = await env.store.documents.get_by_id(doc_id)
    assert document is not None and document.cues is not None
    stored = DocumentCues.model_validate_json(document.cues)
    assert (stored.paper_id, stored.short_names) == (doc_id, ("Basalt Atlas",))
    cues = await registry(env).get_cues(cid)
    assert cues.documents == (stored,)


async def test_stored_cues_are_the_source_of_truth(env: Env) -> None:
    cid = await env.collection()
    doc = await env.store.documents.create(cid, source="upload", sha256="a" * 64,
                                           title="Basalt Atlas", status="ready")
    curated = DocumentCues(paper_id=doc.id, display_name="Basalt Atlas", title="Basalt Atlas",
                           strong=(r"basalt atlas", r"the atlas"), weak=(r"lava",))
    await env.store.documents.set_cues(doc.id, curated.model_dump_json())
    assert (await registry(env).get_cues(cid)).documents == (curated,)


async def test_unreadable_stored_cues_fall_back_to_derivation(
        env: Env, caplog: pytest.LogCaptureFixture) -> None:
    cid = await env.collection()
    doc = await env.store.documents.create(cid, source="upload", sha256="a" * 64,
                                           title="Basalt Atlas: rocks", status="ready")
    await env.store.documents.set_cues(doc.id, '{"not": "cues"}')
    with caplog.at_level(logging.WARNING, logger="relay.ingestion.registry"):
        cues = await registry(env).get_cues(cid)
    assert cues.documents[0].short_names == ("Basalt Atlas",)
    assert "unreadable cues" in caplog.text


@pytest.mark.skipif(not (PAPERS_DIR / "manifest.json").is_file(),
                    reason="the v1 corpus manifest is not present")
async def test_v1_papers_store_the_curated_cues_and_old_stores_are_backfilled(
        tmp_path: Path) -> None:
    index_dir = tmp_path / "indexes"
    (index_dir / "small_fast").mkdir(parents=True)
    (index_dir / "small_fast" / INDEX_MANIFEST).write_text("{}", encoding="utf-8")
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        args = {"papers_dir": PAPERS_DIR, "index_dir": index_dir,
                "collections_dir": tmp_path / "collections"}
        await register_v1_papers(store, **args)
        documents = await store.documents.list_for_collection(V1_COLLECTION_ID)
        for document in documents:
            assert document.cues is not None
            stored = DocumentCues.model_validate_json(document.cues)
            assert stored == v1_document_cues(stored.paper_id)
        await store.documents.set_cues(documents[0].id, None)  # a store from before V2-04
        await register_v1_papers(store, **args)
        again = await store.documents.get_by_id(documents[0].id)
        assert again is not None and again.cues == documents[0].cues
        reg = DocumentRegistry(store.documents, store.collections)
        assert await reg.get_cues(V1_COLLECTION_ID) == V1_CUE_SET
        assert (await store.collections.list_for_tenant(DEV_TENANT_ID))[0].version == 1
