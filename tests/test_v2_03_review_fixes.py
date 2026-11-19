"""V2-03 review fixes: one regression test per finding of the code review and the
silent-failure review (see reports/phase-v2-03-collections-and-ingestion.md).

Stores are :memory:, PDFs are generated, embeddings use the fake embedder. No network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest

from relay.app.bootstrap import (
    DEV_TENANT_ID,
    V1_COLLECTION_ID,
    ensure_dev_tenant,
    register_v1_papers,
)
from relay.app.collections import LazyEmbedder, StoreCollectionResolver
from relay.app.routes.ingestion import fetch_arxiv_pdf
from relay.ingestion.arxiv_fetcher import PaperFetchError
from relay.ingestion.registry import document_cues
from relay.orchestrator.tools import ToolGateway
from relay.retrieval.hybrid_index import INDEX_MANIFEST, RETRIEVAL_SETTINGS, HybridIndex
from relay.retrieval.registry import INDEX_REF_FILE, IndexRegistry, resolve_index_dir
from relay.store import open_store
from relay.store.repositories import Document, IntegrityViolationError
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import Env
from tests.test_ingestion_routes import AUTH, Api
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        yield Env(store, tmp_path)


def resolver(env: Env, embedder: Any = None) -> StoreCollectionResolver:
    return StoreCollectionResolver(env.store, IndexRegistry(
        env.index_root, embedder=embedder or FakeEmbedder()))


def failing_parse(path: Path, paper_id: str, **_: Any) -> Any:
    raise ValueError("secret detail")


# C1: an interrupted index rebuild must not damage the live index --------------------
async def test_a_failed_index_save_leaves_the_live_index_intact(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = await env.collection()
    await env.upload(cid, ["Basalt is a volcanic rock"])
    await env.worker().run_once()
    live = resolve_index_dir(env.index_root, cid)
    before = HybridIndex.load(live).digest()

    def crashing_save(self: HybridIndex, target: Path, **_: Any) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "chunks.jsonl").write_text("half-written", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(HybridIndex, "save", crashing_save)
    await env.upload(cid, ["Granite is an intrusive rock"])
    await env.worker(max_attempts=1).run_once()
    assert HybridIndex.load(resolve_index_dir(env.index_root, cid)).digest() == before
    monkeypatch.undo()
    glass, _ = await env.upload(cid, ["Obsidian is volcanic glass"])
    await env.worker().run_once()  # a later ingest still works on top of the intact index
    merged = HybridIndex.load(resolve_index_dir(env.index_root, cid))
    assert glass in {c.paper_id for c in merged.chunks}


# I1/I2: completion is one transaction; no request sees the new version without the doc
async def test_no_request_sees_the_new_version_without_the_new_document(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = await env.collection()
    first, _ = await env.upload(cid, ["Basalt is a volcanic rock"], title="Basalt Survey")
    await env.worker().run_once()
    res = resolver(env)
    await res.resolve(cid)
    doc, _ = await env.upload(cid, ["Granite is intrusive"], title="Granite Field Notes")
    original = env.store.documents.update_status

    async def racing_update(document_id: str, status: str, **kw: Any) -> Document:
        if status == "ready":
            await res.resolve(cid)  # a request arriving between the two writes
        return await original(document_id, status, **kw)

    monkeypatch.setattr(env.store.documents, "update_status", racing_update)
    await env.worker().run_once()
    cues = (await res.resolve(cid)).cues
    assert {first, doc} <= set(cues.strong)


async def test_completing_an_ingest_is_atomic(env: Env) -> None:
    cid = await env.collection()
    doc_id, job_id = await env.upload(cid, ["Text"])
    with pytest.raises(Exception):  # noqa: B017 - any store error; the point is the rollback
        await env.store.ingest_jobs.complete("no-such-job", document_id=doc_id,
                                             collection_id=cid, pages=1)
    collection = await env.store.collections.get_by_id(cid)
    document = await env.store.documents.get_by_id(doc_id)
    assert collection is not None and collection.version == 0
    assert document is not None and document.status == "queued"
    assert await env.store.ingest_jobs.complete(job_id, document_id=doc_id,
                                                collection_id=cid, pages=1) == 1


# I3: an interrupted job counts as a failed attempt and is capped -----------------------
async def test_interrupted_jobs_count_as_attempts_and_stop_at_the_cap(env: Env) -> None:
    cid = await env.collection()
    doc_id, job_id = await env.upload(cid, ["Text"])
    for _ in range(3):  # the process died mid-parse three times
        await env.store.ingest_jobs.update_state(job_id, "parsing")
        if _ < 2:
            await env.store.ingest_jobs.update_state(job_id, "parsing",
                                                     increment_attempts=True)
    calls: list[str] = []

    def parse(path: Path, paper_id: str, **_: Any) -> Any:
        calls.append(paper_id)
        raise AssertionError("an exhausted job must not run again")

    await env.worker(parse=parse).run_once()
    job = await env.store.ingest_jobs.get_by_id(job_id)
    document = await env.store.documents.get_by_id(doc_id)
    assert job is not None and (job.state, job.attempts, job.error_class) == (
        "failed", 3, "Interrupted")
    assert document is not None and document.status == "failed" and calls == []
    assert await env.worker().run_once() == 0


async def test_a_job_interrupted_once_resumes_and_records_the_interruption(env: Env) -> None:
    cid = await env.collection()
    doc_id, job_id = await env.upload(cid, ["Text about rocks"])
    await env.store.ingest_jobs.update_state(job_id, "embedding")
    await env.worker().run_once()
    job = await env.store.ingest_jobs.get_by_id(job_id)
    assert job is not None and (job.state, job.attempts) == ("ready", 1)


# I4: retries wait for the poll interval ---------------------------------------------
async def test_failed_jobs_are_not_retried_in_a_tight_loop(env: Env) -> None:
    cid = await env.collection()
    _, job_id = await env.upload(cid, ["Text"])
    worker = env.worker(parse=failing_parse)
    worker._poll = 0.5  # noqa: SLF001 - a long interval, to see whether the loop waits
    await worker.start()
    await asyncio.sleep(0.25)
    await worker.aclose()
    job = await env.store.ingest_jobs.get_by_id(job_id)
    assert job is not None and job.attempts == 1


# I8: the worker loop logs sanitized errors only --------------------------------------
async def test_a_failing_poll_is_logged_without_the_exception_message(
        env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    worker = env.worker()

    async def broken() -> int:
        raise RuntimeError("token=sk-secret-value")

    monkeypatch.setattr(worker, "run_once", broken)
    with caplog.at_level(logging.ERROR, logger="relay.ingestion.worker"):
        await worker.start()
        await asyncio.sleep(0.05)
        await worker.aclose()
    assert caplog.records and "sk-secret-value" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


# I5: a document and its job are created together ------------------------------------
async def test_a_document_and_its_job_are_created_in_one_transaction(env: Env) -> None:
    cid = await env.collection()
    document, job = await env.store.documents.create_queued(cid, source="upload",
                                                            sha256="a" * 64, title="T")
    assert (document.status, job.state, job.document_id) == ("queued", "queued", document.id)
    with pytest.raises(IntegrityViolationError):
        await env.store.documents.create_queued(cid, source="upload", sha256="b" * 64,
                                                job_id=job.id)  # the job insert fails
    assert [d.sha256 for d in await env.store.documents.list_for_collection(cid)] == ["a" * 64]


# I6: a failed document can be added again -------------------------------------------
async def test_re_uploading_a_failed_document_queues_it_again(tmp_path: Path) -> None:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        api = Api(store, tmp_path)
        async with api.client:
            cid = await api.collection()
            data = pymupdf_pdf("Basalt is a volcanic rock")
            doc_id = (await api.upload(cid, data)).json()["id"]
            worker = api.worker()
            worker._parse = failing_parse  # noqa: SLF001
            worker._max_attempts = 1  # noqa: SLF001
            await worker.run_once()
            again = await api.upload(cid, data)
            assert again.status_code == 202
            assert again.json() == {"id": doc_id, "status": "queued", "requeued": True}
            job = await store.ingest_jobs.latest_for_document(doc_id)
            assert job is not None and (job.state, job.attempts) == ("queued", 0)
            await api.worker().run_once()
            document = await store.documents.get_by_id(doc_id)
            assert document is not None and document.status == "ready"


def pymupdf_pdf(text: str, *, title: str | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for line in range(20):
        page.insert_text((72, 120 + 18 * line), f"{text} sentence {line}.", fontsize=10)
    if title:
        doc.set_metadata({"title": title})
    data = doc.tobytes()
    doc.close()
    return data


# code review 1: the collection gateway accepts the collection's own paper ids ---------
async def test_the_collection_gateway_accepts_paper_filters_for_its_documents(
        env: Env) -> None:
    cid = await env.collection()
    doc, _ = await env.upload(cid, ["Basalt is a volcanic rock"])
    await env.worker().run_once()
    resources = await resolver(env).resolve(cid)
    assert resources.paper_ids == frozenset({doc})
    base = ToolGateway({"small_fast": StubRetriever(PASSAGES)})
    gateway = base.with_retrievers({"small_fast": resources.retriever},
                                   paper_ids=resources.paper_ids)
    call = await gateway.retrieve_passages({"query": "basalt", "retrieval_setting": "small_fast",
                                            "paper_filter": (doc,)})
    assert call.record.ok and {p.paper_id for p in call.result.passages} == {doc}


# code review 2: generic titles are not scope cues; the PDF's own title is preferred ---
def test_generic_titles_are_not_scope_cues() -> None:
    def doc(title: str) -> Document:
        return Document(id="d", collection_id="c", source="upload", sha256="a" * 64,
                        title=title, pages=1, status="ready", created_at="t")

    assert document_cues(doc("paper")) == ()
    assert document_cues(doc("a")) == ()
    assert document_cues(doc("Basalt Formation Survey")) == (r"basalt\ formation\ survey",)


async def test_the_pdf_title_is_preferred_over_the_file_name(tmp_path: Path) -> None:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        api = Api(store, tmp_path)
        async with api.client:
            cid = await api.collection()
            data = pymupdf_pdf("Basalt text", title="Basalt Formation Survey")
            doc_id = (await api.upload(cid, data, name="paper.pdf")).json()["id"]
            document = await store.documents.get_by_id(doc_id)
            assert document is not None and document.title == "Basalt Formation Survey"


# code review 5 / M6: no unbounded spooling or downloads ------------------------------
async def test_an_upload_without_a_content_length_is_411_and_creates_no_rows(
        tmp_path: Path) -> None:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        api = Api(store, tmp_path)
        async with api.client:
            cid = await api.collection()
            boundary = "relayboundary"
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                    f"filename=\"a.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode() \
                + pymupdf_pdf("text") + f"\r\n--{boundary}--\r\n".encode()

            async def chunked() -> AsyncIterator[bytes]:
                yield body

            reply = await api.client.post(
                f"/v1/collections/{cid}/documents", content=chunked(),
                headers={**AUTH, "content-type": f"multipart/form-data; boundary={boundary}"})
            assert (reply.status_code, reply.json()["error"]) == (411, "length_required")
            assert await api.counts() == (0, 0)


def test_arxiv_downloads_are_size_capped() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"%PDF-" +
                                                                   b"x" * 5000))
    with pytest.raises(PaperFetchError, match="larger than"):
        fetch_arxiv_pdf("2503.00003v1", transport=transport, max_bytes=1000)
    assert fetch_arxiv_pdf("2503.00003v1", transport=transport, max_bytes=10_000)[:5] == b"%PDF-"


# code review 8: models load before the timed tool call ------------------------------
def test_loading_a_collection_warms_its_lazy_models(tmp_path: Path) -> None:
    made: list[int] = []

    def factory() -> FakeEmbedder:
        made.append(1)
        return FakeEmbedder()

    from tests.test_index_registry import write_index

    write_index(tmp_path, "c1", "doc-a", "alpha beta")
    registry = IndexRegistry(tmp_path, embedder=LazyEmbedder(factory,
                                                             model_id="fake/hash-embedder"))
    registry.load("c1", 1, RETRIEVAL_SETTINGS["small_fast"])
    assert made == [1]


# I7: a collection whose index is missing is "not ready", not an internal error --------
async def test_a_collection_without_index_files_is_not_ready(env: Env) -> None:
    cid = await env.collection()
    await env.store.collections.bump_version(cid)  # version 1, but no index on disk
    with pytest.raises(Exception) as caught:
        await resolver(env).describe(cid)
    assert type(caught.value).__name__ == "CollectionNotReadyError"


# bootstrap: a restart must not point v1-papers back at the v1 index -----------------
async def test_a_restart_keeps_v1_papers_documents_added_later(tmp_path: Path) -> None:
    papers_dir = Path("data/papers")
    if not (papers_dir / "manifest.json").is_file():
        pytest.skip("the v1 corpus manifest is not present")
    index_dir = tmp_path / "indexes"
    (index_dir / "small_fast").mkdir(parents=True)
    (index_dir / "small_fast" / INDEX_MANIFEST).write_text("{}", encoding="utf-8")
    collections = tmp_path / "collections"
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        await register_v1_papers(store, papers_dir=papers_dir, index_dir=index_dir,
                                 collections_dir=collections)
        ref = collections / V1_COLLECTION_ID / INDEX_REF_FILE
        merged = {"index_dir": f"{V1_COLLECTION_ID}/builds/b1"}  # an upload was merged
        ref.write_text(json.dumps(merged), encoding="utf-8")
        await register_v1_papers(store, papers_dir=papers_dir, index_dir=index_dir,
                                 collections_dir=collections)
        assert json.loads(ref.read_text(encoding="utf-8")) == merged
        assert (await store.collections.list_for_tenant(DEV_TENANT_ID))[0].version == 1
