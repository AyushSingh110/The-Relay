"""V2-03 tests: DocumentRegistry derives scope cues and retrieval settings from the store."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from relay.ingestion.registry import DocumentRegistry, document_paper_id
from relay.retrieval.scope import analyze_scope
from relay.store import Store, open_store


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as fresh:
        yield fresh


async def make_collection(store: Store, **kwargs: object) -> str:
    tenant = await store.tenants.create("acme")
    return (await store.collections.create(tenant.id, "c", **kwargs)).id  # type: ignore[arg-type]


async def test_cues_come_from_ready_documents_only(store: Store) -> None:
    cid = await make_collection(store)
    moshi = await store.documents.create(
        cid, source="arxiv:2410.00037v1", sha256="a",
        title="Moshi: a speech-text foundation model for real-time dialogue", status="ready")
    upload = await store.documents.create(cid, source="upload", sha256="b",
                                          title="Retrieval-Augmented Generation", status="ready")
    await store.documents.create(cid, source="upload", sha256="c", title="Not Yet Indexed",
                                 status="queued")
    cues = await DocumentRegistry(store.documents, store.collections).get_cues(cid)
    assert set(cues.strong) == {"2410.00037v1", upload.id}
    assert document_paper_id(moshi) == "2410.00037v1" and document_paper_id(upload) == upload.id
    assert cues.pinned_versions == {"2410.00037": 1}
    assert cues.weak == {}
    assert analyze_scope("How does Moshi work?", cues=cues).strong_papers == ("2410.00037v1",)
    assert analyze_scope("Explain retrieval-augmented generation.",
                         cues=cues).strong_papers == (upload.id,)
    assert analyze_scope("What about not yet indexed?", cues=cues).strong_papers == ()
    assert analyze_scope("In 2410.00037v3?", cues=cues).version_mismatch


async def test_regex_characters_in_titles_are_escaped(store: Store) -> None:
    cid = await make_collection(store)
    doc = await store.documents.create(cid, source="upload", sha256="a",
                                       title="C++ (and .NET) [draft]", status="ready")
    cues = await DocumentRegistry(store.documents, store.collections).get_cues(cid)
    assert analyze_scope("what does c++ (and .net) [draft] say?",
                         cues=cues).strong_papers == (doc.id,)


async def test_retrieval_setting_follows_the_collection(store: Store) -> None:
    registry = DocumentRegistry(store.documents, store.collections)
    plain = await registry.retrieval_setting(await make_collection(store))
    assert (plain.name, plain.chunk_preset, plain.rerank) == ("small_fast", "small_fast", False)
    wide = await registry.retrieval_setting(
        await make_collection(store, retrieval_preset="large_wide", rerank=True))
    assert (wide.name, wide.chunk_preset, wide.rerank) == ("large_wide_rerank", "large_wide",
                                                           True)
    balanced = await registry.retrieval_setting(
        await make_collection(store, retrieval_preset="balanced_rerank"))
    assert (balanced.name, balanced.rerank) == ("balanced_rerank", True)  # already reranks


async def test_unknown_collections_and_presets_fail_clearly(store: Store) -> None:
    from relay.ingestion.registry import CollectionNotFoundError, UnknownPresetError

    registry = DocumentRegistry(store.documents, store.collections)
    with pytest.raises(CollectionNotFoundError):
        await registry.get_cues("missing")
    with pytest.raises(UnknownPresetError):
        await registry.retrieval_setting(
            await make_collection(store, retrieval_preset="no_such_preset"))
