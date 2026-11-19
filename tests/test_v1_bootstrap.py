"""V2-03 tests: first start-up registers the three v1 papers as the ``v1-papers`` collection.

The registration reuses the v1 index files through ``index_ref.json`` (no re-embedding) and
is idempotent. Stores are :memory:; the index directory is a temporary stand-in for
``data/indexes`` holding only an index manifest marker.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from relay.app.bootstrap import (
    DEV_TENANT_ID,
    V1_COLLECTION_ID,
    ensure_dev_tenant,
    register_v1_papers,
)
from relay.errors import ConfigurationError
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.ingestion.registry import arxiv_source
from relay.retrieval.hybrid_index import INDEX_MANIFEST
from relay.retrieval.registry import resolve_index_dir
from relay.store import Store, open_store

PAPERS_DIR = Path("data/papers")
pytestmark = pytest.mark.skipif(not (PAPERS_DIR / "manifest.json").is_file(),
                                reason="the v1 corpus manifest is not present")


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as fresh:
        await ensure_dev_tenant(fresh)
        yield fresh


def v1_index(tmp_path: Path) -> Path:
    index_dir = tmp_path / "indexes"
    (index_dir / "small_fast").mkdir(parents=True)
    (index_dir / "small_fast" / INDEX_MANIFEST).write_text("{}", encoding="utf-8")
    return index_dir


async def register(store: Store, tmp_path: Path) -> object:
    index_dir = tmp_path / "indexes"
    if not index_dir.exists():
        v1_index(tmp_path)
    return await register_v1_papers(store, papers_dir=PAPERS_DIR, index_dir=index_dir,
                                    collections_dir=tmp_path / "collections")


async def test_dev_tenant_is_created_once(store: Store) -> None:
    again = await ensure_dev_tenant(store)
    assert again.id == DEV_TENANT_ID
    assert await store.tenants.get_by_id(DEV_TENANT_ID) == again


async def test_an_empty_store_gets_the_v1_papers_collection(store: Store,
                                                            tmp_path: Path) -> None:
    collection = await register(store, tmp_path)
    assert collection is not None
    stored = await store.collections.get_by_id(V1_COLLECTION_ID)
    assert stored is not None
    assert (stored.name, stored.tenant_id, stored.retrieval_preset, stored.version) == (
        "v1-papers", DEV_TENANT_ID, "small_fast", 1)
    documents = await store.documents.list_for_collection(V1_COLLECTION_ID)
    manifest = load_corpus_manifest(PAPERS_DIR).papers
    assert sorted((d.source, d.sha256, d.title, d.pages, d.status) for d in documents) == sorted(
        (arxiv_source(p.arxiv_id, p.version), p.sha256, p.title, p.page_count, "ready")
        for p in manifest)
    for document in documents:
        job = await store.ingest_jobs.latest_for_document(document.id)
        assert job is not None and job.state == "ready"
    # the collection reads the v1 small_fast index; nothing is copied or re-embedded
    assert resolve_index_dir(tmp_path / "collections", V1_COLLECTION_ID).resolve() == (
        tmp_path / "indexes" / "small_fast").resolve()


async def test_registration_is_idempotent(store: Store, tmp_path: Path) -> None:
    await register(store, tmp_path)
    await register(store, tmp_path)
    stored = await store.collections.get_by_id(V1_COLLECTION_ID)
    assert stored is not None and stored.version == 1
    assert len(await store.documents.list_for_collection(V1_COLLECTION_ID)) == 3
    assert len(await store.collections.list_for_tenant(DEV_TENANT_ID)) == 1


async def test_a_store_that_already_has_collections_is_left_alone(store: Store,
                                                                  tmp_path: Path) -> None:
    await store.collections.create(DEV_TENANT_ID, "mine")
    assert await register(store, tmp_path) is None
    assert await store.collections.get_by_id(V1_COLLECTION_ID) is None


async def test_a_missing_v1_index_fails_clearly(store: Store, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="small_fast"):
        await register_v1_papers(store, papers_dir=PAPERS_DIR, index_dir=tmp_path / "none",
                                 collections_dir=tmp_path / "collections")
    assert await store.collections.get_by_id(V1_COLLECTION_ID) is None
