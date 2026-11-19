"""Ingest worker: queued documents become searchable collection indexes (plan V2-03).

A background loop polls the store for runnable jobs every ``poll_interval_s`` (default 2 s)
and processes them **one at a time** (so two jobs never merge into the same collection
index at once):

    queued -> parsing    v1 parser on the document's PDF (in a worker thread)
           -> embedding  v1 chunker with the collection's preset + dense vectors (thread)
           -> indexing   merge into {index_dir}/{collection_id}/ (thread)
           -> ready      collection version bumped; document ready

Reuse of v1, unchanged: ``parse_pdf``, ``chunk_paper``, ``HybridIndex``. ``chunk_paper`` is
typed for the v1 ``PaperSpec`` but only reads five attributes; uploads pass a
``DocumentSpec`` with the same attributes. Documents whose arXiv id is one of the v1 papers
keep that paper's parser hints (``extra_headings``), so the v1 papers chunk exactly as in
v1 (regression-tested against the v1 chunk-set hashes).

**Incremental indexing:** only the new document is embedded. Its chunks are merged into the
collection's current index (its own files, or the index an ``index_ref.json`` points at),
replacing any earlier chunks of the same document, and ordered by ``paper_id`` with each
document's chunk order kept, exactly as v1 orders its chunk sets.

**Failures:** any exception marks the job ``failed`` with the exception's class name (never
its message), increments ``attempts`` and marks the document ``failed``; a failed job is
retried on later polls until it has failed ``max_attempts`` times (default 3).
**Crash safety:** a job interrupted mid-way (process killed, worker cancelled) stays in its
in-progress state, and in-progress jobs are runnable, so the next worker resumes it from
the start. A ``ready`` document is never ingested again unless ``reingest`` is called.

**Extraction quality** (per document): total pages, pages with no extracted text, and a
suspected-scan flag (more than 40 % empty). It is written next to the collection index and
read back by ``GET /v1/documents/{id}``. A suspected scan is flagged, not rejected (owner
decision 2026-11-18); a PDF with no extractable text at all fails, since nothing can be
indexed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from relay.errors import ConfigurationError, RelayError, sanitize_error
from relay.ingestion.arxiv_fetcher import PAPERS
from relay.ingestion.chunker import CHUNK_PRESETS, Chunk, chunk_paper
from relay.ingestion.pdf_parser import ParsedPaper, parse_pdf
from relay.ingestion.registry import (
    cues_for_document,
    document_paper_id,
    parse_arxiv_source,
    retrieval_setting_for,
)
from relay.retrieval.embedder import Embedder
from relay.retrieval.hybrid_index import HybridIndex
from relay.retrieval.registry import INDEX_REF_FILE, CollectionNotReadyError, resolve_index_dir
from relay.store import Store
from relay.store.repositories import Document, IngestJob

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S: Final = 2.0
DEFAULT_MAX_ATTEMPTS: Final = 3
SCAN_EMPTY_SHARE: Final = 0.4
DOCS_DIR: Final = "docs"
BUILDS_DIR: Final = "builds"
INTERRUPTED: Final = "Interrupted"  # error_class of a job found half-done after a crash
IN_PROGRESS_STATES: Final = ("parsing", "embedding", "indexing")
EXTRACTION_FILE: Final = "extraction.json"
_V1_SPECS: Final = {spec.paper_id: spec for spec in PAPERS}
ParseFn = Callable[..., ParsedPaper]


class NoExtractableTextError(RelayError):
    """The PDF produced no text to index (for example a fully scanned document)."""


class DocumentFileError(RelayError):
    """The document's PDF is missing or does not match its recorded SHA-256."""


@dataclass(frozen=True)
class DocumentSpec:
    """The five attributes ``chunk_paper`` reads from a v1 ``PaperSpec``."""

    paper_id: str
    arxiv_id: str
    version: int
    short_name: str
    extra_headings: tuple[str, ...] = ()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")[:40] or "document"


def spec_for(document: Document) -> Any:
    """The chunking spec for a document (the v1 ``PaperSpec`` for the three v1 papers)."""
    paper_id = document_paper_id(document)
    if paper_id in _V1_SPECS:
        return _V1_SPECS[paper_id]
    arxiv = parse_arxiv_source(document.source)
    if arxiv is not None:
        return DocumentSpec(paper_id=paper_id, arxiv_id=arxiv[0], version=arxiv[1],
                            short_name=_slug(f"arxiv {arxiv[0]}"))
    return DocumentSpec(paper_id=paper_id, arxiv_id="", version=0,
                        short_name=_slug(document.title or ""))


@dataclass(frozen=True)
class FileLocator:
    """Where a document's PDF lives: the v1 papers directory for the v1 arXiv papers,
    otherwise the content-addressed uploads directory (``{sha256}.pdf``)."""

    papers_dir: Path
    uploads_dir: Path

    def upload_path(self, sha256: str) -> Path:
        return self.uploads_dir / f"{sha256}.pdf"

    def path_for(self, document: Document) -> Path:
        if parse_arxiv_source(document.source) is not None:
            local = self.papers_dir / f"{document_paper_id(document)}.pdf"
            if local.is_file():
                return local
        return self.upload_path(document.sha256)

    def verified_path(self, document: Document) -> Path:
        path = self.path_for(document)
        if not path.is_file():
            raise DocumentFileError(f"PDF for document {document.id} not found")
        if hashlib.sha256(path.read_bytes()).hexdigest() != document.sha256:
            raise DocumentFileError(f"PDF for document {document.id} does not match its hash")
        return path


# --- Extraction quality ----------------------------------------------------------------


def extraction_quality(parsed: ParsedPaper) -> dict[str, Any]:
    total = parsed.page_count
    empty = sum(1 for page in parsed.pages if page.words == 0)
    return {"total_pages": total, "empty_pages": empty,
            "suspected_scan": total > 0 and empty / total > SCAN_EMPTY_SHARE}


def extraction_report_path(index_root: Path, collection_id: str, document_id: str) -> Path:
    return index_root / collection_id / DOCS_DIR / document_id / EXTRACTION_FILE


def read_extraction_quality(index_root: Path, collection_id: str,
                            document_id: str) -> dict[str, Any] | None:
    path = extraction_report_path(index_root, collection_id, document_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- Index merging ---------------------------------------------------------------------


def merge_indexes(parts: Sequence[HybridIndex], *, replace_paper: str) -> HybridIndex:
    """One index from ``parts``; chunks of ``replace_paper`` are taken only from the last
    part (the new document). Chunks are ordered by ``paper_id``, keeping each document's
    own chunk order, as v1 builds its chunk sets."""
    embedders = {part.embedder_id for part in parts}
    if len(embedders) != 1:
        raise ConfigurationError(f"cannot merge indexes built with different embedding "
                                 f"models: {sorted(embedders)}")
    groups: list[tuple[Chunk, list[np.ndarray]]] = []
    for position, part in enumerate(parts):
        rows: dict[int, list[np.ndarray]] = {}
        for window, owner in enumerate(part.owners.tolist()):
            rows.setdefault(owner, []).append(part.vectors[window])
        last = position == len(parts) - 1
        for i, chunk in enumerate(part.chunks):
            if chunk.paper_id == replace_paper and not last:
                continue  # the earlier version of the document being (re)ingested
            groups.append((chunk, rows[i]))
    groups.sort(key=lambda group: group[0].paper_id)  # stable: per-document order kept
    vectors = np.stack([row for _, rows in groups for row in rows])
    owners = np.array([i for i, (_, rows) in enumerate(groups) for _ in rows], dtype=np.int64)
    return HybridIndex([chunk for chunk, _ in groups], vectors, owners, embedders.pop())


def rebuild_collection_index(index_root: Path, collection_id: str, new: HybridIndex,
                             paper_id: str) -> HybridIndex:
    """Merge ``new`` into the collection's current index, atomically.

    The merged index is written to a fresh build directory, and only when it is complete
    is ``index_ref.json`` replaced (``os.replace``) to point at it. A crash or a failed
    write therefore leaves the live index untouched; readers see the old index or the new
    one, never a mix. The build replaced now is kept (a reader may be loading it); older
    builds are removed."""
    try:
        current_dir: Path | None = resolve_index_dir(index_root, collection_id)
        current: HybridIndex | None = HybridIndex.load(current_dir)
    except CollectionNotReadyError:
        current_dir, current = None, None
    merged = new if current is None else merge_indexes([current, new], replace_paper=paper_id)
    folder = index_root / collection_id
    build = folder / BUILDS_DIR / uuid.uuid4().hex
    merged.save(build, extra={"collection_id": collection_id})
    _write_json(folder / INDEX_REF_FILE,
                {"index_dir": f"{collection_id}/{BUILDS_DIR}/{build.name}"})
    keep = {build.resolve()} | ({current_dir.resolve()} if current_dir is not None else set())
    for old in (folder / BUILDS_DIR).iterdir():
        if old.resolve() not in keep:
            try:
                shutil.rmtree(old)
            except OSError as exc:  # leftover files waste space but break nothing
                logger.warning("could not remove old index build %s: %s", old,
                               sanitize_error(exc))
    return merged


# --- Worker ----------------------------------------------------------------------------


class IngestWorker:
    def __init__(self, store: Store, *, index_root: Path, files: FileLocator,
                 embedder: Embedder, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS, parse: ParseFn = parse_pdf) -> None:
        if poll_interval_s <= 0 or max_attempts < 1:
            raise ValueError("poll_interval_s and max_attempts must be positive")
        self._store = store
        self._index_root = index_root
        self._files = files
        self._embedder = embedder
        self._poll = poll_interval_s
        self._max_attempts = max_attempts
        self._parse = parse
        self._task: asyncio.Task[None] | None = None
        self.processed = 0
        self.failed = 0
        self.last_pass_succeeded = 0

    # --- lifecycle ---

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ingest-worker")

    async def aclose(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                handled = await self.run_once()
                busy = handled > 0 and self.last_pass_succeeded > 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep polling; a store outage must not end the worker
                logger.error("ingest worker poll failed: %s", sanitize_error(exc))
                busy = False
            if not busy:  # idle, or every job failed: wait before retrying
                await asyncio.sleep(self._poll)

    # --- work ---

    async def run_once(self) -> int:
        """Process every runnable job once; returns how many were handled."""
        jobs = await self._store.ingest_jobs.list_runnable(max_attempts=self._max_attempts)
        self.last_pass_succeeded = 0
        for job in jobs:
            if await self._process(job):
                self.last_pass_succeeded += 1
        return len(jobs)

    async def reingest(self, document_id: str) -> IngestJob:
        """Explicitly re-ingest a document, even one that is already ready."""
        return await self._store.documents.requeue(document_id)

    async def _process(self, job: IngestJob) -> bool:
        """Run one job; True if the document became ready."""
        jobs, documents = self._store.ingest_jobs, self._store.documents
        document = await documents.get_by_id(job.document_id)
        if document is None:
            await jobs.update_state(job.id, "failed", error_class="DocumentMissing",
                                    increment_attempts=True)
            return False
        if document.status == "ready":
            await jobs.update_state(job.id, "ready")  # never re-ingest unless requested
            return False
        if job.state in IN_PROGRESS_STATES:
            # Found half-done: the process stopped mid-job. That counts as a failed attempt,
            # so a PDF that crashes the process every time cannot crash-loop the service.
            job = await jobs.update_state(job.id, job.state, error_class=INTERRUPTED,
                                          increment_attempts=True)
            logger.warning("ingest job %s was interrupted (attempt %d of %d)", job.id,
                           job.attempts, self._max_attempts)
            if job.attempts >= self._max_attempts:
                self.failed += 1
                await jobs.update_state(job.id, "failed", error_class=INTERRUPTED)
                await documents.update_status(document.id, "failed")
                return False
        try:
            await self._ingest(job, document)
        except asyncio.CancelledError:
            raise  # left in its in-progress state: the next worker resumes it
        except Exception as exc:
            self.failed += 1
            logger.error("ingesting document %s failed (attempt %d of %d): %s", document.id,
                         job.attempts + 1, self._max_attempts, sanitize_error(exc))
            await jobs.update_state(job.id, "failed", error_class=type(exc).__name__,
                                    increment_attempts=True)
            await documents.update_status(document.id, "failed")
            return False
        return True

    async def _ingest(self, job: IngestJob, document: Document) -> None:
        jobs, documents = self._store.ingest_jobs, self._store.documents
        collection = await self._store.collections.get_by_id(document.collection_id)
        if collection is None:
            raise ConfigurationError(f"collection {document.collection_id} does not exist")
        setting = retrieval_setting_for(collection.retrieval_preset, collection.rerank)
        preset = next(p for p in CHUNK_PRESETS if p.name == setting.chunk_preset)
        spec = spec_for(document)

        await jobs.update_state(job.id, "parsing")
        await documents.update_status(document.id, "ingesting")
        path = await asyncio.to_thread(self._files.verified_path, document)
        parsed = await asyncio.to_thread(self._parse, path, spec.paper_id,
                                         extra_headings=spec.extra_headings)
        await asyncio.to_thread(
            _write_json, extraction_report_path(self._index_root, collection.id, document.id),
            extraction_quality(parsed))

        await jobs.update_state(job.id, "embedding")
        chunks = await asyncio.to_thread(chunk_paper, parsed, spec, preset)
        if not chunks:
            raise NoExtractableTextError(f"document {document.id} has no extractable text")
        doc_index = await asyncio.to_thread(HybridIndex.build, chunks, self._embedder)

        await jobs.update_state(job.id, "indexing")
        await asyncio.to_thread(rebuild_collection_index, self._index_root, collection.id,
                                doc_index, spec.paper_id)
        cues = cues_for_document(document).model_dump_json()  # stored with the document
        await jobs.complete(job.id, document_id=document.id, collection_id=collection.id,
                            pages=parsed.page_count, cues=cues)  # one transaction
        self.processed += 1


__all__ = ["DEFAULT_MAX_ATTEMPTS", "DEFAULT_POLL_INTERVAL_S", "DocumentFileError",
           "DocumentSpec", "FileLocator", "IngestWorker", "NoExtractableTextError",
           "extraction_quality", "extraction_report_path", "merge_indexes",
           "read_extraction_quality", "rebuild_collection_index", "spec_for"]
