"""V2-03 tests: the ingest worker (parse -> chunk/embed -> index -> ready).

Stores are ``:memory:``; PDFs are generated into ``tmp_path``; embeddings use the fake
hashing embedder, so no model is loaded. The v1 chunk-hash regression parses the real v1
PDFs from ``data/papers`` (gitignored) and is skipped when they are absent.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.ingestion.pdf_parser import parse_pdf
from relay.ingestion.registry import arxiv_source, retrieval_setting_for
from relay.ingestion.worker import (
    FileLocator,
    IngestWorker,
    NoExtractableTextError,
    read_extraction_quality,
)
from relay.retrieval.hybrid_index import HybridIndex
from relay.retrieval.registry import IndexRegistry, resolve_index_dir
from relay.store import Store, open_store
from tests.test_index_registry import FakeEmbedder

PAPERS_DIR = Path("data/papers")


def make_pdf(path: Path, pages: Sequence[str | None]) -> bytes:
    """One page per entry: text, or None for a page with no text (scan-like)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            for line in range(25):
                page.insert_text((72, 120 + 18 * line), f"{text} line {line}.", fontsize=10)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return data


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as fresh:
        yield fresh


class Env:
    def __init__(self, store: Store, tmp: Path) -> None:
        self.store = store
        self.tmp = tmp
        self.files = FileLocator(papers_dir=tmp / "papers", uploads_dir=tmp / "uploads")
        self.files.uploads_dir.mkdir(parents=True)
        self.index_root = tmp / "indexes"

    def worker(self, **kwargs: Any) -> IngestWorker:
        return IngestWorker(self.store, index_root=self.index_root, files=self.files,
                            embedder=FakeEmbedder(), poll_interval_s=0.01, **kwargs)

    async def collection(self, name: str = "c", **kwargs: Any) -> str:
        tenant = await self.store.tenants.create("acme")
        return (await self.store.collections.create(tenant.id, name, **kwargs)).id

    async def upload(self, collection_id: str, pages: Sequence[str | None],
                     title: str = "Doc") -> tuple[str, str]:
        import hashlib

        path = self.tmp / "incoming.pdf"
        data = make_pdf(path, pages)
        sha = hashlib.sha256(data).hexdigest()
        path.replace(self.files.upload_path(sha))
        doc = await self.store.documents.create(collection_id, source="upload", sha256=sha,
                                                title=title, status="queued")
        job = await self.store.ingest_jobs.create(doc.id)
        return doc.id, job.id


@pytest.fixture
def env(store: Store, tmp_path: Path) -> Env:
    return Env(store, tmp_path)


async def test_a_queued_job_goes_through_every_state_to_ready(env: Env,
                                                              monkeypatch: Any) -> None:
    cid = await env.collection()
    doc_id, job_id = await env.upload(cid, ["Retrieval augmented generation", "More text"])
    seen: list[str] = []
    original = env.store.ingest_jobs.update_state

    async def recording(job: str, state: str, **kw: Any) -> Any:
        seen.append(state)
        return await original(job, state, **kw)

    monkeypatch.setattr(env.store.ingest_jobs, "update_state", recording)
    assert await env.worker().run_once() == 1
    # "ready" is written by IngestJobRepo.complete, with the version bump, in one transaction
    assert seen == ["parsing", "embedding", "indexing"]
    job = await env.store.ingest_jobs.get_by_id(job_id)
    doc = await env.store.documents.get_by_id(doc_id)
    collection = await env.store.collections.get_by_id(cid)
    assert job is not None and job.state == "ready" and job.attempts == 0
    assert doc is not None and doc.status == "ready" and doc.pages == 2
    assert collection is not None and collection.version == 1


async def test_a_new_pdf_is_queryable_without_a_restart(env: Env) -> None:
    cid = await env.collection()
    doc_id, _ = await env.upload(cid, ["Photosynthesis converts light into chemical energy"])
    await env.worker().run_once()
    collection = await env.store.collections.get_by_id(cid)
    assert collection is not None
    registry = IndexRegistry(env.index_root, embedder=FakeEmbedder())
    loaded = registry.load(cid, collection.version, retrieval_setting_for("small_fast", False))
    passages = loaded.retriever.retrieve("photosynthesis light energy").passages
    assert passages and {p.paper_id for p in passages} == {doc_id}


async def test_two_collections_never_share_passages(env: Env) -> None:
    first, second = await env.collection("one"), await env.collection("two")
    a, _ = await env.upload(first, ["Volcanoes erupt molten rock called lava"])
    b, _ = await env.upload(second, ["Volcanoes also appear in this other collection"])
    await env.worker().run_once()
    registry = IndexRegistry(env.index_root, embedder=FakeEmbedder())
    setting = retrieval_setting_for("small_fast", False)
    for cid, doc_id in ((first, a), (second, b)):
        loaded = registry.load(cid, 1, setting)
        assert {p.paper_id for p in loaded.retriever.retrieve("volcanoes").passages} == {doc_id}


async def test_adding_a_document_merges_into_the_collection_index(env: Env) -> None:
    cid = await env.collection()
    a, _ = await env.upload(cid, ["Glaciers carve valleys"], title="Ice")
    await env.worker().run_once()
    b, _ = await env.upload(cid, ["Rivers deposit sediment in deltas"], title="Water")
    await env.worker().run_once()
    index = HybridIndex.load(resolve_index_dir(env.index_root, cid))
    assert {c.paper_id for c in index.chunks} == {a, b}
    assert [c.paper_id for c in index.chunks] == sorted(c.paper_id for c in index.chunks)
    collection = await env.store.collections.get_by_id(cid)
    assert collection is not None and collection.version == 2


async def test_parsing_failures_are_recorded_and_retried_at_most_three_times(env: Env) -> None:
    cid = await env.collection()
    doc_id, job_id = await env.upload(cid, ["Text"])
    calls = 0

    def broken_parse(path: Path, paper_id: str, **_: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ValueError("secret detail that must not be stored")

    worker = env.worker(parse=broken_parse)
    for expected_attempts in (1, 2, 3):
        assert await worker.run_once() == 1
        job = await env.store.ingest_jobs.get_by_id(job_id)
        assert job is not None and (job.state, job.attempts) == ("failed", expected_attempts)
        assert job.error_class == "ValueError"  # the class name, never the message
    assert await worker.run_once() == 0 and calls == 3  # no fourth attempt
    doc = await env.store.documents.get_by_id(doc_id)
    assert doc is not None and doc.status == "failed"


async def test_a_crash_mid_ingestion_resumes_on_restart(env: Env) -> None:
    cid = await env.collection()
    doc_id, job_id = await env.upload(cid, ["Tectonic plates move slowly"])
    entered = threading.Event()
    release = threading.Event()

    def hanging_parse(path: Path, paper_id: str, **kw: Any) -> Any:
        entered.set()
        release.wait(timeout=10)  # a long parse; the worker is killed meanwhile
        return parse_pdf(path, paper_id, **kw)

    crashing = env.worker(parse=hanging_parse)
    await crashing.start()
    await asyncio.to_thread(entered.wait, 5)
    await crashing.aclose()  # the worker dies mid-job
    release.set()
    job = await env.store.ingest_jobs.get_by_id(job_id)
    assert job is not None and job.state == "parsing"  # left in progress, not failed
    assert await env.worker().run_once() == 1  # a new worker after "restart"
    job = await env.store.ingest_jobs.get_by_id(job_id)
    doc = await env.store.documents.get_by_id(doc_id)
    # the interruption counts as one failed attempt (review fix I3: no crash loops)
    assert job is not None and job.state == "ready" and job.attempts == 1
    assert doc is not None and doc.status == "ready"


async def test_a_ready_document_is_not_re_ingested_unless_requested(env: Env) -> None:
    cid = await env.collection()
    doc_id, _ = await env.upload(cid, ["Coral reefs host many species"])
    await env.worker().run_once()
    calls = 0

    def counting_parse(path: Path, paper_id: str, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        return parse_pdf(path, paper_id, **kw)

    stray = await env.store.ingest_jobs.create(doc_id)  # a duplicate job for a ready document
    worker = env.worker(parse=counting_parse)
    assert await worker.run_once() == 1 and calls == 0
    skipped = await env.store.ingest_jobs.get_by_id(stray.id)
    assert skipped is not None and skipped.state == "ready"
    await worker.reingest(doc_id)  # explicit request
    assert await worker.run_once() == 1 and calls == 1


async def test_extraction_quality_flags_suspected_scans_without_rejecting(env: Env) -> None:
    cid = await env.collection()
    scan, _ = await env.upload(cid, ["Some readable text on the first page", None, None])
    await env.worker().run_once()
    doc = await env.store.documents.get_by_id(scan)
    assert doc is not None and doc.status == "ready"  # flagged, not rejected (owner decision)
    quality = read_extraction_quality(env.index_root, cid, scan)
    assert quality == {"total_pages": 3, "empty_pages": 2, "suspected_scan": True}
    clean, _ = await env.upload(cid, ["Page one text", "Page two text"])
    await env.worker().run_once()
    assert read_extraction_quality(env.index_root, cid, clean) == {
        "total_pages": 2, "empty_pages": 0, "suspected_scan": False}


async def test_a_pdf_with_no_text_at_all_fails_with_a_clear_reason(env: Env) -> None:
    cid = await env.collection()
    _, job_id = await env.upload(cid, [None, None])
    await env.worker(max_attempts=1).run_once()
    job = await env.store.ingest_jobs.get_by_id(job_id)
    assert job is not None and job.state == "failed"
    assert job.error_class == NoExtractableTextError.__name__


@pytest.mark.skipif(not all((PAPERS_DIR / f"{p}.pdf").is_file() for p in
                            ("1706.03762v7", "2410.00037v1", "2507.07955v2")),
                    reason="v1 PDFs are not present in data/papers (gitignored)")
@pytest.mark.parametrize("preset", ["small_fast", "large_wide"])
async def test_v1_papers_through_the_new_path_give_the_v1_chunk_hashes(
        store: Store, tmp_path: Path, preset: str) -> None:
    corpus = load_corpus_manifest(PAPERS_DIR)
    tenant = await store.tenants.create("acme")
    collection = await store.collections.create(tenant.id, "v1-check", retrieval_preset=preset)
    for record in corpus.papers:
        doc = await store.documents.create(collection.id, source=arxiv_source(
            record.arxiv_id, record.version), sha256=record.sha256, title=record.title,
            status="queued")
        await store.ingest_jobs.create(doc.id)
    worker = IngestWorker(store, index_root=tmp_path / "idx",
                          files=FileLocator(papers_dir=PAPERS_DIR, uploads_dir=tmp_path),
                          embedder=FakeEmbedder())
    assert await worker.run_once() == 3
    index = HybridIndex.load(resolve_index_dir(tmp_path / "idx", collection.id))
    expected = (corpus.extraction or {})["chunk_sets"][preset]
    assert len(index.chunks) == expected["count"]
    assert index.digest()["chunks_sha256"] == expected["sha256"]
    assert json.loads(json.dumps(sorted({c.paper_id for c in index.chunks}))) == [
        "1706.03762v7", "2410.00037v1", "2507.07955v2"]
