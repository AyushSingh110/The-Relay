"""V2-04 review fixes: one regression test per finding of the whole-phase code review
(see reports/phase-v2-04-collection-aware-safety.md). :memory: stores, no network."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from pydantic import ValidationError

from relay.app.bootstrap import V1_COLLECTION_ID, ensure_dev_tenant, register_v1_papers
from relay.app.collections import StoreCollectionResolver
from relay.ingestion.v1_corpus import V1_CUE_SET, v1_document_cues
from relay.orchestrator.messages import refusal_messages
from relay.orchestrator.verifier import Verifier
from relay.retrieval.cues import CueSet, DocumentCues
from relay.retrieval.hybrid_index import INDEX_MANIFEST
from relay.retrieval.registry import IndexRegistry
from relay.store import open_store
from tests.test_collection_aware_safety import attention_passage, passage
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import Env

PAPERS_DIR = Path("data/papers")


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        yield Env(store, tmp_path)


def resolver(env: Env) -> StoreCollectionResolver:
    return StoreCollectionResolver(env.store, IndexRegistry(env.index_root,
                                                            embedder=FakeEmbedder()))


async def sibling(env: Env, collection_id: str, name: str) -> str:
    first = await env.store.collections.get_by_id(collection_id)
    assert first is not None
    return (await env.store.collections.create(first.tenant_id, name)).id


# 1: the same document in two collections is not "foreign" to either ----------------
async def test_a_document_in_both_collections_is_not_a_foreign_name(env: Env) -> None:
    first = await env.collection("a")
    second = await sibling(env, first, "b")
    for cid in (first, second):
        await env.upload(cid, ["Basalt is a volcanic rock formed from lava"],
                         title="Basalt Atlas: volcanic rocks of the world")
    await env.worker().run_once()
    res = await resolver(env).resolve(second)
    names = {p for d in res.foreign for p in d.strong}
    assert res.foreign == () or not names & {p for d in res.cues.documents for p in d.strong}


# 2: foreign documents are blocked by their names, not by their aliases or topics -----
def test_foreign_documents_contribute_titles_and_short_names_only() -> None:
    moshi = v1_document_cues("2410.00037v1")
    assert moshi is not None
    chemistry = CueSet(documents=(DocumentCues(paper_id="doc-chem", display_name="Chem",
                                               strong=(r"chem notes",)),))
    text = "He has the lowest boiling point of all elements."  # the passage says "He"
    chem_passage = passage("doc-chem").model_copy(update={"text": text})
    verdict = Verifier(chemistry, foreign=(moshi,)).verify(
        "Helium has the lowest boiling point of all elements [P1].", [chem_passage])
    assert verdict.verdict == "VERIFIED"  # "helium" is a Moshi alias, not its name
    named = Verifier(chemistry, foreign=(moshi,)).verify(
        "Moshi has the lowest boiling point of all elements [P1].", [chem_passage])
    assert named.verdict == "BLOCKED"


# 3 and 6: collection answers are cached and verified against the tenant's current state
async def test_the_cache_key_and_safety_change_when_another_collection_changes(
        env: Env) -> None:
    rocks = await env.collection("rocks")
    await env.upload(rocks, ["Basalt is a volcanic rock formed from lava"], title="Rocks Book")
    await env.worker().run_once()
    res = resolver(env)
    first_tag, again_tag = (await res.describe(rocks))[2], (await res.describe(rocks))[2]
    one, two = await res.resolve(rocks), await res.resolve(rocks)
    assert first_tag == again_tag and one.safety is two.safety  # cached, not rebuilt
    minerals = await sibling(env, rocks, "minerals")
    await env.upload(minerals, ["Quartz is hard"], title="Granite Primer: minerals")
    await env.worker().run_once()
    assert (await res.describe(rocks))[2] != first_tag
    three = await res.resolve(rocks)
    assert three.safety is not one.safety
    assert "p06-1" in first_tag  # the collection prompt version is part of the key


# 4: the curated v1 cues are refreshed in stores that hold older ones ----------------
@pytest.mark.skipif(not (PAPERS_DIR / "manifest.json").is_file(),
                    reason="the v1 corpus manifest is not present")
async def test_stale_stored_v1_cues_are_refreshed_at_start_up(tmp_path: Path) -> None:
    index_dir = tmp_path / "indexes"
    (index_dir / "small_fast").mkdir(parents=True)
    (index_dir / "small_fast" / INDEX_MANIFEST).write_text("{}", encoding="utf-8")
    args = {"papers_dir": PAPERS_DIR, "index_dir": index_dir,
            "collections_dir": tmp_path / "collections"}
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        await register_v1_papers(store, **args)
        doc = (await store.documents.list_for_collection(V1_COLLECTION_ID))[1]
        stale = DocumentCues(paper_id="2410.00037v1", display_name="Moshi", strong=(r"moshi",))
        await store.documents.set_cues(doc.id, stale.model_dump_json())
        await register_v1_papers(store, **args)
        resolved = StoreCollectionResolver(store, IndexRegistry(
            tmp_path / "collections", embedder=FakeEmbedder()))
        assert await resolved.documents.get_cues(V1_COLLECTION_ID) == V1_CUE_SET


# 5: stored patterns must compile ---------------------------------------------------
def test_cues_with_invalid_patterns_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentCues(paper_id="d", display_name="D", strong=(r"foo(",))
    with pytest.raises(ValidationError):
        DocumentCues(paper_id="d", display_name="D", weak=(r"[unclosed",))


# 8: long collections are not listed one by one in refusals ---------------------------
def test_refusals_for_large_collections_give_the_count_only() -> None:
    many = CueSet(documents=tuple(
        DocumentCues(paper_id=f"d{i:03d}", display_name=f'"Doc {i}"', arxiv_id=f"2501.{i:05d}",
                     version=1) for i in range(30)))
    messages = refusal_messages(many)
    assert messages.out_of_corpus == ("I can only answer from 30 documents in this collection, "
                                      "and they do not cover this question.")
    assert len(messages.version) < 200 and '"Doc 3"' not in messages.out_of_corpus


def test_the_v1_verifier_still_blocks_v1_aliases_within_its_own_corpus() -> None:
    answer = "Mimi relies on multi-head attention over the encoder outputs [P1]."
    assert Verifier(V1_CUE_SET).verify(answer, [attention_passage()]).verdict == "BLOCKED"
