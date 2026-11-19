"""Start-up registration for the store (plan V2-03).

``ensure_dev_tenant`` creates the fixed development tenant that the stubbed API-key check
maps every key to (real keys and tenants arrive with V2-05).

``register_v1_papers`` makes the three v1 papers queryable through the collection path on
first start-up with an empty store: a collection ``v1-papers`` (fixed id, preset
``small_fast``) with one ``ready`` document per paper from the v1 corpus manifest, whose
index is the **existing** v1 ``small_fast`` index, referenced through ``index_ref.json``
(nothing is copied or re-embedded). Every step is idempotent, so a start-up interrupted
half-way completes on the next one. A store that already has other collections is left
alone.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Final

from relay.errors import ConfigurationError
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.ingestion.registry import arxiv_source
from relay.ingestion.v1_corpus import v1_document_cues
from relay.retrieval.hybrid_index import INDEX_MANIFEST
from relay.retrieval.registry import INDEX_REF_FILE
from relay.store import Store
from relay.store.repositories import Collection, Tenant

logger = logging.getLogger(__name__)

DEV_TENANT_ID: Final = "dev"
DEV_TENANT_NAME: Final = "development"
V1_COLLECTION_ID: Final = "v1-papers"
V1_COLLECTION_NAME: Final = "v1-papers"
V1_PRESET: Final = "small_fast"


async def ensure_dev_tenant(store: Store) -> Tenant:
    tenant = await store.tenants.get_by_id(DEV_TENANT_ID)
    if tenant is None:
        tenant = await store.tenants.create(DEV_TENANT_NAME, tenant_id=DEV_TENANT_ID)
    return tenant


def _index_reference(index_dir: Path, collections_dir: Path) -> str:
    """The v1 index as seen from ``collections_dir`` (relative when possible)."""
    target = (index_dir / V1_PRESET).resolve()
    try:
        return Path(os.path.relpath(target, collections_dir.resolve())).as_posix()
    except ValueError:  # Windows: a different drive has no relative path
        return target.as_posix()


def _write_reference(collections_dir: Path, reference: str) -> None:
    folder = collections_dir / V1_COLLECTION_ID
    if (folder / INDEX_REF_FILE).is_file() or (folder / INDEX_MANIFEST).is_file():
        return  # already set up; after an upload it points at the collection's own build
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{INDEX_REF_FILE}.tmp"
    tmp.write_text(json.dumps({"index_dir": reference}) + "\n", encoding="utf-8")
    os.replace(tmp, folder / INDEX_REF_FILE)


async def register_v1_papers(store: Store, *, papers_dir: Path, index_dir: Path,
                             collections_dir: Path) -> Collection | None:
    """Register (or finish registering) ``v1-papers``; None if the store is not empty."""
    collection = await store.collections.get_by_id(V1_COLLECTION_ID)
    if collection is None and await store.collections.list_for_tenant(DEV_TENANT_ID):
        return None
    papers = load_corpus_manifest(papers_dir).papers
    if not papers:
        raise ConfigurationError(f"no v1 corpus manifest in {papers_dir}; "
                                 "run scripts/ingest_papers.py")
    if not (index_dir / V1_PRESET / INDEX_MANIFEST).is_file():
        raise ConfigurationError(f"the v1 {V1_PRESET} index is missing from {index_dir}; "
                                 "run scripts/build_indexes.py")
    if collection is None:
        collection = await store.collections.create(
            DEV_TENANT_ID, V1_COLLECTION_NAME, retrieval_preset=V1_PRESET,
            collection_id=V1_COLLECTION_ID)
    for paper in papers:
        curated = v1_document_cues(paper.paper_id)
        cues = None if curated is None else curated.model_dump_json()
        existing = await store.documents.find_by_sha256(collection.id, paper.sha256)
        if existing is not None:
            if cues is not None and existing.cues != cues:  # older store, or older cues
                await store.documents.set_cues(existing.id, cues)
            continue
        document = await store.documents.create(
            collection.id, source=arxiv_source(paper.arxiv_id, paper.version),
            sha256=paper.sha256, title=paper.title, pages=paper.page_count, status="ready",
            cues=cues)
        await store.ingest_jobs.create(document.id, state="ready")
    _write_reference(collections_dir, _index_reference(index_dir, collections_dir))
    if collection.version == 0:
        await store.collections.bump_version(collection.id)
        logger.info("registered the v1 papers as collection %s", collection.id)
    return await store.collections.get_by_id(collection.id)


__all__ = ["DEV_TENANT_ID", "V1_COLLECTION_ID", "V1_COLLECTION_NAME", "V1_PRESET",
           "ensure_dev_tenant", "register_v1_papers"]
