"""Collection and document routes (plan V2-03; docs/ARCHITECTURE_V2.md §9).

    POST /v1/collections                    {name, tenant_id, retrieval_preset?, rerank?}
    GET  /v1/collections
    POST /v1/collections/{id}/documents     multipart "file" (a PDF), or JSON {"arxiv_id"}
    GET  /v1/documents/{id}                 document + job state + extraction quality

**API keys are stubbed** until V2-05: any ``Authorization: Bearer <key>`` maps to the fixed
development tenant; a missing or non-Bearer header is 401. Every lookup is still checked
against the caller's tenant, and a collection or document of another tenant is 404 (its
existence is not revealed).

**Uploads:** PDF only (checked from the bytes, not the declared type), at most
``max_upload_bytes`` (413) and ``max_pages`` (422). Every check runs before any row is
written, so a rejected upload leaves nothing behind. The PDF is stored content-addressed
(``{uploads_dir}/{sha256}.pdf``); the same bytes uploaded again to the same collection
return the existing document (200) and queue no new job. A new document is ``queued``
with an ingest job (202); the ingest worker does the rest.

**arXiv:** an exact version is required (``2410.00037v1``). The PDF is taken from the
local papers directory when present, otherwise from ``fetch_arxiv``; its first page must
carry the ``arXiv:<id>v<n>`` stamp of the requested version.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final

import httpx
import pymupdf
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from relay.app.bootstrap import DEV_TENANT_ID
from relay.errors import sanitize_error
from relay.ingestion.arxiv_fetcher import (
    MAX_PDF_BYTES,
    PaperFetchError,
    load_corpus_manifest,
    make_client,
    normalise_text,
)
from relay.ingestion.registry import arxiv_source
from relay.ingestion.worker import FileLocator, read_extraction_quality
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.store import Store
from relay.store.repositories import Collection, Document, IngestJob

logger = logging.getLogger(__name__)

ArxivFetch = Callable[[str], bytes]
ARXIV_PDF_URL: Final = "https://arxiv.org/pdf/{paper_id}"
MULTIPART_OVERHEAD: Final = 64 * 1024  # form framing allowed on top of the file itself
TITLE_MAX_CHARS: Final = 200
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


class CreateCollection(BaseModel):
    model_config = _STRICT

    name: Annotated[str, Field(min_length=1, max_length=128)]
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]
    retrieval_preset: Annotated[str, Field(max_length=64)] = "small_fast"
    rerank: bool = False
    buffered: bool = False  # V2-05: POST /v1/ask answers in one piece for this collection


class ArxivDocument(BaseModel):
    model_config = _STRICT

    arxiv_id: Annotated[str, Field(pattern=r"^\d{4}\.\d{4,5}v[1-9]\d*$")]


@dataclass(frozen=True)
class IngestionContext:
    store: Store
    files: FileLocator
    collections_dir: Path
    max_upload_bytes: int
    max_pages: int
    fetch_arxiv: ArxivFetch | None = None
    # serialises de-duplication and row creation (one process, one event loop)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Rejected(Exception):
    """A request that is refused with ``status`` and a stable ``code``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    headers = {"www-authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status,
                        headers=headers)


def _invalid(exc: ValidationError) -> JSONResponse:
    # Location and error type only: the rejected input is not echoed back.
    return _error(422, "invalid_request", "the request body is not valid",
                  details=[{"loc": [str(p) for p in err["loc"]], "type": err["type"]}
                           for err in exc.errors()])


def caller_tenant(request: Request) -> str:
    """Stub of the V2-05 API-key check: any Bearer key is the development tenant."""
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise Rejected(401, "unauthorized", "an API key is required: Authorization: Bearer <key>")
    return DEV_TENANT_ID


# --- PDF checks ------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfInfo:
    pages: int
    title: str | None
    first_page: str


def inspect_pdf(data: bytes, *, max_pages: int) -> PdfInfo:
    """Blocking (run it in a thread). Raises ``Rejected`` for anything not ingestible."""
    if not data.startswith(b"%PDF-"):
        raise Rejected(415, "not_a_pdf", "only PDF files can be added")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError):  # pymupdf's FileDataError is a RuntimeError
        raise Rejected(422, "unreadable_pdf", "the PDF cannot be opened") from None
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise Rejected(422, "encrypted_pdf", "the PDF is encrypted")
        if doc.page_count < 1:
            raise Rejected(422, "unreadable_pdf", "the PDF has no pages")
        if doc.page_count > max_pages:
            raise Rejected(422, "too_many_pages",
                           f"the PDF has {doc.page_count} pages; the limit is {max_pages}")
        title = _clean_title((doc.metadata or {}).get("title") or "")
        return PdfInfo(pages=doc.page_count, title=title,
                       first_page=normalise_text(str(doc[0].get_text("text"))))


def _clean_title(text: str) -> str | None:
    text = " ".join(text.split())[:TITLE_MAX_CHARS]
    return text or None


def _write_upload(path: Path, data: bytes) -> None:
    if path.is_file():
        return  # content-addressed: the same bytes are already stored
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def fetch_arxiv_pdf(paper_id: str, *, transport: httpx.BaseTransport | None = None,
                    max_bytes: int = MAX_PDF_BYTES) -> bytes:
    """Download one exact arXiv version (blocking), reading at most ``max_bytes``.
    Raises ``PaperFetchError``."""
    url = ARXIV_PDF_URL.format(paper_id=paper_id)
    parts: list[bytes] = []
    size = 0
    with make_client(transport) as client:
        try:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise PaperFetchError(f"{paper_id}: HTTP {response.status_code} from arXiv")
                for part in response.iter_bytes():
                    size += len(part)
                    if size > max_bytes:
                        raise PaperFetchError(f"{paper_id}: the PDF is larger than "
                                              f"{max_bytes} bytes")
                    parts.append(part)
        except httpx.HTTPError as exc:
            raise PaperFetchError(f"{paper_id}: download failed ({type(exc).__name__})") from None
    return b"".join(parts)


# --- Views -----------------------------------------------------------------------------


def _collection_view(c: Collection) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "version": c.version,
            "retrieval_preset": c.retrieval_preset, "rerank": c.rerank,
            "buffered": c.buffered,
            "created_at": c.created_at}


def _job_view(job: IngestJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {"id": job.id, "state": job.state, "attempts": job.attempts,
            "error_class": job.error_class, "updated_at": job.updated_at}


def _document_view(d: Document, job: IngestJob | None,
                   quality: dict[str, Any] | None) -> dict[str, Any]:
    return {"id": d.id, "collection_id": d.collection_id, "source": d.source,
            "title": d.title, "sha256": d.sha256, "pages": d.pages, "status": d.status,
            "created_at": d.created_at, "job": _job_view(job),
            "extraction_quality": quality}


# --- Router ----------------------------------------------------------------------------


def ingestion_router(ctx: IngestionContext) -> APIRouter:
    router = APIRouter(prefix="/v1")
    store = ctx.store

    async def own_collection(collection_id: str, tenant: str) -> Collection:
        collection = await store.collections.get_by_id(collection_id)
        if collection is None or collection.tenant_id != tenant:
            raise Rejected(404, "collection_not_found", "no such collection")
        return collection

    async def read_upload(request: Request) -> tuple[bytes, str | None]:
        declared = request.headers.get("content-length", "")
        if not declared.isdigit():  # without it the form parser would spool any size to disk
            raise Rejected(411, "length_required", "uploads need a Content-Length header")
        if int(declared) > ctx.max_upload_bytes + MULTIPART_OVERHEAD:
            raise Rejected(413, "payload_too_large", _too_large(ctx.max_upload_bytes))
        async with request.form(max_files=1, max_fields=10) as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise Rejected(422, "missing_file", "send the PDF as the multipart field 'file'")
            data = await upload.read(ctx.max_upload_bytes + 1)
            name = upload.filename
        if len(data) > ctx.max_upload_bytes:
            raise Rejected(413, "payload_too_large", _too_large(ctx.max_upload_bytes))
        return data, _clean_title(Path(name).stem) if name else None

    async def read_arxiv(request: Request) -> tuple[bytes, str, str | None]:
        body = ArxivDocument.model_validate_json(await request.body())
        paper_id = body.arxiv_id
        local = ctx.files.papers_dir / f"{paper_id}.pdf"
        if await asyncio.to_thread(local.is_file):
            data = await asyncio.to_thread(local.read_bytes)
        elif ctx.fetch_arxiv is None:
            raise Rejected(503, "arxiv_fetch_unavailable",
                           f"{paper_id} is not available locally and fetching is disabled")
        else:
            try:
                data = await asyncio.to_thread(ctx.fetch_arxiv, paper_id)
            except PaperFetchError as exc:
                raise Rejected(502, "arxiv_fetch_failed", str(exc)) from None
        if len(data) > ctx.max_upload_bytes:
            raise Rejected(413, "payload_too_large", _too_large(ctx.max_upload_bytes))
        record = (await asyncio.to_thread(load_corpus_manifest, ctx.files.papers_dir)
                  ).record_for(paper_id)
        return data, paper_id, None if record is None else record.title

    async def add_document(collection: Collection, request: Request) -> JSONResponse:
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "multipart/form-data":
            data, file_title = await read_upload(request)
            source, paper_id, known_title = "upload", None, None
        elif kind == "application/json":
            data, paper_id, known_title = await read_arxiv(request)
            file_title = None
            arxiv_id, _, version = paper_id.rpartition("v")
            source = arxiv_source(arxiv_id, int(version))
        else:
            raise Rejected(415, "unsupported_media_type",
                           "send multipart/form-data with a 'file', or JSON {\"arxiv_id\"}")
        info = await asyncio.to_thread(inspect_pdf, data, max_pages=ctx.max_pages)
        if paper_id is not None and f"arXiv:{paper_id}" not in info.first_page:
            raise Rejected(422, "version_stamp_missing",
                           f"the PDF's first page does not carry the arXiv:{paper_id} stamp")
        sha = hashlib.sha256(data).hexdigest()
        # the curated title (v1 manifest), else the PDF's own, else the file name
        title = known_title or info.title or file_title
        async with ctx.lock:
            existing = await store.documents.find_by_sha256(collection.id, sha)
            if existing is not None and existing.status != "failed":
                return JSONResponse({"id": existing.id, "status": existing.status,
                                     "deduplicated": True}, status_code=200)
            if source == "upload" or not (ctx.files.papers_dir / f"{paper_id}.pdf").is_file():
                await asyncio.to_thread(_write_upload, ctx.files.upload_path(sha), data)
            if existing is not None:  # it failed before: adding it again retries it
                await store.documents.requeue(existing.id)
                return JSONResponse({"id": existing.id, "status": "queued",
                                     "requeued": True}, status_code=202)
            document, _ = await store.documents.create_queued(
                collection.id, source=source, sha256=sha, title=title, pages=info.pages)
        return JSONResponse({"id": document.id, "status": document.status}, status_code=202)

    @router.post("/collections")
    async def create_collection(request: Request) -> JSONResponse:
        try:
            tenant = caller_tenant(request)
            body = CreateCollection.model_validate_json(await request.body())
            if not body.name.strip():
                raise Rejected(422, "invalid_request", "the collection name is blank")
            if body.tenant_id != tenant:
                raise Rejected(403, "tenant_mismatch", "tenant_id is not the caller's tenant")
            if body.retrieval_preset not in RETRIEVAL_SETTINGS:
                raise Rejected(422, "unknown_preset",
                               f"retrieval_preset must be one of {sorted(RETRIEVAL_SETTINGS)}")
            async with ctx.lock:
                if await store.collections.find_by_name(tenant, body.name) is not None:
                    raise Rejected(409, "collection_exists",
                                   "this tenant already has a collection with that name")
                collection = await store.collections.create(
                    tenant, body.name, retrieval_preset=body.retrieval_preset,
                    rerank=body.rerank, buffered=body.buffered)
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        except ValidationError as exc:
            return _invalid(exc)
        return JSONResponse({"id": collection.id, "name": collection.name,
                             "version": collection.version}, status_code=201)

    @router.get("/collections")
    async def list_collections(request: Request) -> JSONResponse:
        try:
            tenant = caller_tenant(request)
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        collections = await store.collections.list_for_tenant(tenant)
        return JSONResponse({"collections": [_collection_view(c) for c in collections]})

    @router.post("/collections/{collection_id}/documents")
    async def create_document(collection_id: str, request: Request) -> JSONResponse:
        try:
            collection = await own_collection(collection_id, caller_tenant(request))
            return await add_document(collection, request)
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        except ValidationError as exc:
            return _invalid(exc)

    @router.get("/documents/{document_id}")
    async def get_document(document_id: str, request: Request) -> JSONResponse:
        try:
            tenant = caller_tenant(request)
            document = await store.documents.get_by_id(document_id)
            if document is None:
                raise Rejected(404, "document_not_found", "no such document")
            try:
                await own_collection(document.collection_id, tenant)
            except Rejected:
                raise Rejected(404, "document_not_found", "no such document") from None
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        job = await store.ingest_jobs.latest_for_document(document.id)
        try:
            quality = await asyncio.to_thread(read_extraction_quality, ctx.collections_dir,
                                              document.collection_id, document.id)
        except (OSError, json.JSONDecodeError) as exc:
            # The report is advisory: the status is still served, and the failure logged.
            logger.warning("extraction report for document %s unreadable: %s", document.id,
                           sanitize_error(exc))
            quality = None
        return JSONResponse(_document_view(document, job, quality))

    return router


def _too_large(limit: int) -> str:
    return f"the PDF is larger than the {limit / (1024 * 1024):g} MB limit"


__all__ = ["ArxivFetch", "IngestionContext", "caller_tenant", "fetch_arxiv_pdf",
           "ingestion_router", "inspect_pdf"]
