"""V2-03 tests: the /v1 collection and document routes (tests 1-4 of the phase prompt).

The routes run on an ASGI transport over a :memory: store; PDFs are generated in
``tmp_path``; the ingest worker uses the fake embedder. No network, no API key.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.app.routes.ingestion import IngestionContext, fetch_arxiv_pdf, ingestion_router
from relay.app.settings import IngestionSettings
from relay.ingestion.arxiv_fetcher import PaperFetchError
from relay.ingestion.worker import FileLocator, IngestWorker
from relay.store import Store, open_store
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import make_pdf

AUTH = {"Authorization": "Bearer relay-dev-key"}


class Api:
    def __init__(self, store: Store, tmp: Path, **limits: Any) -> None:
        self.store = store
        self.tmp = tmp
        self.files = FileLocator(papers_dir=tmp / "papers", uploads_dir=tmp / "uploads")
        self.files.papers_dir.mkdir()
        self.fetched: list[str] = []
        self.ctx = IngestionContext(store=store, files=self.files,
                                    collections_dir=tmp / "collections",
                                    max_upload_bytes=limits.get("max_upload_bytes", 5_000_000),
                                    max_pages=limits.get("max_pages", 500),
                                    fetch_arxiv=limits.get("fetch_arxiv"))
        app = FastAPI()
        app.include_router(ingestion_router(self.ctx))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test")

    def worker(self) -> IngestWorker:
        return IngestWorker(self.store, index_root=self.ctx.collections_dir, files=self.files,
                            embedder=FakeEmbedder(), poll_interval_s=0.01)

    async def collection(self, name: str = "papers", **extra: Any) -> str:
        reply = await self.client.post("/v1/collections", headers=AUTH,
                                       json={"name": name, "tenant_id": DEV_TENANT_ID, **extra})
        assert reply.status_code == 201, reply.text
        return reply.json()["id"]

    async def upload(self, collection_id: str, data: bytes,
                     name: str = "paper.pdf") -> httpx.Response:
        return await self.client.post(f"/v1/collections/{collection_id}/documents",
                                      headers=AUTH,
                                      files={"file": (name, data, "application/pdf")})

    async def counts(self) -> tuple[int, int]:
        conn = self.store.conn
        docs = (await (await conn.execute("SELECT COUNT(*) FROM document")).fetchone())[0]
        jobs = (await (await conn.execute("SELECT COUNT(*) FROM ingest_job")).fetchone())[0]
        return docs, jobs


@pytest.fixture
async def api(tmp_path: Path) -> AsyncGenerator[Api]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        client = Api(store, tmp_path)
        async with client.client:
            yield client


def pdf(tmp: Path, *pages: str | None) -> bytes:
    return make_pdf(tmp / "made.pdf", list(pages) or ["Some text about rocks"])


# 1 --------------------------------------------------------------------------------------
async def test_create_and_list_collections(api: Api) -> None:
    reply = await api.client.post("/v1/collections", headers=AUTH, json={
        "name": "geology", "tenant_id": DEV_TENANT_ID, "retrieval_preset": "large_wide"})
    assert reply.status_code == 201
    body = reply.json()
    assert body == {"id": body["id"], "name": "geology", "version": 0}
    stored = await api.store.collections.get_by_id(body["id"])
    assert stored is not None and stored.retrieval_preset == "large_wide"
    listed = (await api.client.get("/v1/collections", headers=AUTH)).json()["collections"]
    assert [(c["id"], c["name"], c["version"]) for c in listed] == [(body["id"], "geology", 0)]


async def test_collection_requests_are_validated(api: Api) -> None:
    post = api.client.post
    assert (await post("/v1/collections", json={"name": "x", "tenant_id": DEV_TENANT_ID})
            ).status_code == 401  # no API key
    assert (await post("/v1/collections", headers={"Authorization": "Basic abc"},
                       json={"name": "x", "tenant_id": DEV_TENANT_ID})).status_code == 401
    assert (await post("/v1/collections", headers=AUTH,
                       json={"name": "x", "tenant_id": "someone-else"})).status_code == 403
    bad_preset = await post("/v1/collections", headers=AUTH, json={
        "name": "x", "tenant_id": DEV_TENANT_ID, "retrieval_preset": "huge"})
    assert (bad_preset.status_code, bad_preset.json()["error"]) == (422, "unknown_preset")
    assert (await post("/v1/collections", headers=AUTH, json={"tenant_id": DEV_TENANT_ID})
            ).status_code == 422
    await api.collection("dup")
    assert (await post("/v1/collections", headers=AUTH,
                       json={"name": "dup", "tenant_id": DEV_TENANT_ID})).status_code == 409
    assert (await api.client.get("/v1/collections")).status_code == 401


# 2 --------------------------------------------------------------------------------------
async def test_an_upload_creates_a_queued_document_and_a_job(api: Api, tmp_path: Path) -> None:
    cid = await api.collection()
    data = pdf(tmp_path, "Basalt is a volcanic rock", "Granite is intrusive")
    reply = await api.upload(cid, data, name="Rocks of the World.pdf")
    assert reply.status_code == 202
    body = reply.json()
    assert body == {"id": body["id"], "status": "queued"}
    document = await api.store.documents.get_by_id(body["id"])
    assert document is not None
    sha = hashlib.sha256(data).hexdigest()
    assert (document.collection_id, document.status, document.sha256) == (cid, "queued", sha)
    assert document.title == "Rocks of the World"
    job = await api.store.ingest_jobs.latest_for_document(document.id)
    assert job is not None and (job.state, job.attempts) == ("queued", 0)
    assert api.files.upload_path(sha).read_bytes() == data


# 3 --------------------------------------------------------------------------------------
async def test_a_duplicate_upload_returns_the_existing_document_without_a_new_job(
        api: Api, tmp_path: Path) -> None:
    cid = await api.collection()
    data = pdf(tmp_path, "Basalt is a volcanic rock")
    first = (await api.upload(cid, data)).json()
    before = await api.counts()
    again = await api.upload(cid, data, name="renamed.pdf")
    assert again.status_code == 200
    assert again.json() == {"id": first["id"], "status": "queued", "deduplicated": True}
    assert await api.counts() == before == (1, 1)
    other = await api.collection("other")  # de-duplication is per collection
    assert (await api.upload(other, data)).status_code == 202


# 4 --------------------------------------------------------------------------------------
async def test_an_upload_over_the_size_limit_is_413_and_creates_no_rows(
        tmp_path: Path) -> None:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        api = Api(store, tmp_path, max_upload_bytes=20_000)
        async with api.client:
            cid = await api.collection()
            big = pdf(tmp_path, *(["x" * 60] * 40))
            assert len(big) > 20_000
            reply = await api.upload(cid, big)
            assert (reply.status_code, reply.json()["error"]) == (413, "payload_too_large")
            assert await api.counts() == (0, 0)
            assert not api.files.uploads_dir.exists() or not any(api.files.uploads_dir.iterdir())


def test_the_default_upload_limit_is_50_mb_and_500_pages() -> None:
    limits = IngestionSettings()
    assert (limits.max_upload_bytes, limits.max_pages) == (50 * 1024 * 1024, 500)


async def test_non_pdfs_and_page_limits_are_rejected_without_rows(tmp_path: Path) -> None:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        api = Api(store, tmp_path, max_pages=2)
        async with api.client:
            cid = await api.collection()
            text = await api.upload(cid, b"just some text, not a pdf", name="notes.pdf")
            assert (text.status_code, text.json()["error"]) == (415, "not_a_pdf")
            broken = await api.upload(cid, b"%PDF-1.7 truncated garbage")
            assert (broken.status_code, broken.json()["error"]) == (422, "unreadable_pdf")
            long = await api.upload(cid, pdf(tmp_path, "a", "b", "c"))
            assert (long.status_code, long.json()["error"]) == (422, "too_many_pages")
            missing = await api.client.post(f"/v1/collections/{cid}/documents", headers=AUTH,
                                            files={"other": ("a.pdf", b"x")})
            assert (missing.status_code, missing.json()["error"]) == (422, "missing_file")
            assert await api.counts() == (0, 0)


async def test_documents_of_unknown_or_foreign_collections_are_404(api: Api,
                                                                   tmp_path: Path) -> None:
    tenant = await api.store.tenants.create("someone else")
    foreign = await api.store.collections.create(tenant.id, "theirs")
    data = pdf(tmp_path, "text")
    assert (await api.upload("no-such-id", data)).status_code == 404
    assert (await api.upload(foreign.id, data)).status_code == 404
    document = await api.store.documents.create(foreign.id, source="upload", sha256="a" * 64)
    assert (await api.client.get(f"/v1/documents/{document.id}", headers=AUTH)
            ).status_code == 404
    assert (await api.client.get("/v1/documents/nope", headers=AUTH)).status_code == 404
    assert await api.counts() == (1, 0)


# arXiv by exact version ------------------------------------------------------------------
async def test_an_arxiv_id_is_ingested_from_the_local_papers_dir(api: Api,
                                                                 tmp_path: Path) -> None:
    cid = await api.collection()
    make_pdf(api.files.papers_dir / "2501.00001v2.pdf", ["arXiv:2501.00001v2 [cs.CL] A paper"])
    reply = await api.client.post(f"/v1/collections/{cid}/documents", headers=AUTH,
                                  json={"arxiv_id": "2501.00001v2"})
    assert reply.status_code == 202, reply.text
    document = await api.store.documents.get_by_id(reply.json()["id"])
    assert document is not None and document.source == "arxiv:2501.00001v2"
    assert api.fetched == []


async def test_arxiv_requests_need_an_exact_version_and_a_matching_stamp(
        api: Api, tmp_path: Path) -> None:
    cid = await api.collection()
    post = api.client.post
    url = f"/v1/collections/{cid}/documents"
    assert (await post(url, headers=AUTH, json={"arxiv_id": "2501.00001"})).status_code == 422
    make_pdf(api.files.papers_dir / "2501.00001v1.pdf", ["arXiv:2501.00001v3 is another"])
    wrong = await post(url, headers=AUTH, json={"arxiv_id": "2501.00001v1"})
    assert (wrong.status_code, wrong.json()["error"]) == (422, "version_stamp_missing")
    offline = await post(url, headers=AUTH, json={"arxiv_id": "2502.00002v1"})
    assert (offline.status_code, offline.json()["error"]) == (503, "arxiv_fetch_unavailable")
    assert (await post(url, headers=AUTH, content=b"text", )).status_code == 415
    assert await api.counts() == (0, 0)


async def test_an_arxiv_id_not_on_disk_is_fetched(tmp_path: Path) -> None:
    fetched: list[str] = []

    def fetch(paper_id: str) -> bytes:
        fetched.append(paper_id)
        return make_pdf(tmp_path / "fetched.pdf", [f"arXiv:{paper_id} [cs.LG] Fetched"])

    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        api = Api(store, tmp_path, fetch_arxiv=fetch)
        async with api.client:
            cid = await api.collection()
            reply = await api.client.post(f"/v1/collections/{cid}/documents", headers=AUTH,
                                          json={"arxiv_id": "2503.00003v1"})
            assert reply.status_code == 202, reply.text
            document = await store.documents.get_by_id(reply.json()["id"])
            assert document is not None
            assert api.files.upload_path(document.sha256).is_file()
    assert fetched == ["2503.00003v1"]


def test_the_default_arxiv_fetcher_asks_for_the_exact_version_offline() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("v1"):
            return httpx.Response(200, content=b"%PDF-1.7 ...")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    assert fetch_arxiv_pdf("2503.00003v1", transport=transport) == b"%PDF-1.7 ..."
    with pytest.raises(PaperFetchError, match="HTTP 404"):
        fetch_arxiv_pdf("2503.00003v2", transport=transport)
    assert seen == ["https://arxiv.org/pdf/2503.00003v1", "https://arxiv.org/pdf/2503.00003v2"]


# status -------------------------------------------------------------------------------
async def test_document_status_shows_the_job_and_the_extraction_quality(
        api: Api, tmp_path: Path) -> None:
    cid = await api.collection()
    doc_id = (await api.upload(cid, pdf(tmp_path, "Basalt rock", None, None))).json()["id"]
    queued = (await api.client.get(f"/v1/documents/{doc_id}", headers=AUTH)).json()
    assert (queued["status"], queued["job"]["state"], queued["extraction_quality"]) == (
        "queued", "queued", None)
    await api.worker().run_once()
    ready = (await api.client.get(f"/v1/documents/{doc_id}", headers=AUTH)).json()
    assert (ready["status"], ready["pages"], ready["collection_id"]) == ("ready", 3, cid)
    assert ready["job"] == {**ready["job"], "state": "ready", "attempts": 0,
                            "error_class": None}
    assert ready["extraction_quality"] == {"total_pages": 3, "empty_pages": 2,
                                           "suspected_scan": True}  # flagged, not rejected
    collection = (await api.client.get("/v1/collections", headers=AUTH)).json()["collections"]
    assert collection[0]["version"] == 1
