"""Fetch and validate the three pinned arXiv papers; maintain `data/papers/manifest.json`.

Only the exact versions named in ASSIGNMENT.md are allowed, each from its
version-specific URL (``https://arxiv.org/pdf/<id>v<version>``). A PDF is accepted only
if it is a readable, unencrypted PDF whose first page carries arXiv's own version stamp
(``arXiv:<id>v<version>``) and the expected title. arXiv versions are immutable, so a
checksum change for an already-recorded paper is an error, never a silent update.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

import httpx
import pymupdf
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.ingestion.errors import ManifestError, PaperFetchError, PaperValidationError

MANIFEST_FILE: Final = "manifest.json"
MIN_PDF_BYTES: Final = 1_000
MAX_PDF_BYTES: Final = 60_000_000
FETCH_TIMEOUT_S: Final = 60.0
USER_AGENT: Final = "the-relay-ingest/0.1 (research corpus fetch)"

_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")
_LIGATURES: Final = str.maketrans(
    {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
)


class PaperSpec(BaseModel):
    model_config = _FROZEN

    arxiv_id: Annotated[str, Field(pattern=r"^\d{4}\.\d{4,5}$")]
    version: Annotated[int, Field(ge=1)]
    short_name: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    title: str
    # Headings missing from the PDF outline that must still start a new section.
    extra_headings: tuple[str, ...] = ()

    @property
    def paper_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    @property
    def url(self) -> str:
        return f"https://arxiv.org/pdf/{self.paper_id}"

    @property
    def file_name(self) -> str:
        return f"{self.paper_id}.pdf"


# The only papers the assistant may answer from (ASSIGNMENT.md page 1).
PAPERS: Final[tuple[PaperSpec, ...]] = (
    PaperSpec(arxiv_id="1706.03762", version=7, short_name="attention",
              title="Attention Is All You Need",
              # Appendix after the references (pages 13-15); not in the PDF outline.
              extra_headings=("Attention Visualizations",)),
    PaperSpec(arxiv_id="2410.00037", version=1, short_name="moshi",
              title="Moshi: a speech-text foundation model for real-time dialogue"),
    PaperSpec(arxiv_id="2507.07955", version=2, short_name="hnet",
              title="Dynamic Chunking for End-to-End Hierarchical Sequence Modeling"),
)


class PaperRecord(BaseModel):
    """One validated paper as recorded in the tracked manifest."""

    model_config = _FROZEN

    paper_id: str
    arxiv_id: str
    version: int
    short_name: str
    title: str
    source_url: str
    file_name: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(gt=0)]
    page_count: Annotated[int, Field(gt=0)]
    version_stamp: str  # e.g. "arXiv:1706.03762v7", as found on page 1
    pdf_metadata: dict[str, str]
    fetched_at: str | None  # ISO-8601 UTC of the first download; None if pre-existing file


class CorpusManifest(BaseModel):
    model_config = _FROZEN

    schema_version: int = 1
    papers: tuple[PaperRecord, ...] = ()
    extraction: dict[str, Any] | None = None

    def record_for(self, paper_id: str) -> PaperRecord | None:
        return next((record for record in self.papers if record.paper_id == paper_id), None)


def normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_LIGATURES)).strip()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_pdf_bytes(spec: PaperSpec, data: bytes) -> tuple[int, dict[str, str], str]:
    """Return (page_count, pdf_metadata, version_stamp) or raise PaperValidationError."""
    if not data.startswith(b"%PDF-"):
        raise PaperValidationError(f"{spec.paper_id}: not a PDF (missing %PDF- signature)")
    if not MIN_PDF_BYTES <= len(data) <= MAX_PDF_BYTES:
        raise PaperValidationError(f"{spec.paper_id}: implausible PDF size {len(data)} bytes")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as exc:  # pymupdf raises FileDataError (RuntimeError)
        raise PaperValidationError(f"{spec.paper_id}: PDF cannot be opened") from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise PaperValidationError(f"{spec.paper_id}: PDF is encrypted")
        if doc.page_count < 1:
            raise PaperValidationError(f"{spec.paper_id}: PDF has no pages")
        first_page = normalise_text(doc[0].get_text("text"))
        metadata = {k: str(v) for k, v in sorted((doc.metadata or {}).items()) if v}
        page_count = doc.page_count
    stamp = f"arXiv:{spec.paper_id}"
    if stamp not in first_page:
        found = re.findall(rf"arXiv:{re.escape(spec.arxiv_id)}v\d+", first_page)
        detail = f"found {sorted(set(found))}" if found else "no arXiv stamp found"
        raise PaperValidationError(f"{spec.paper_id}: version stamp {stamp} missing ({detail})")
    if spec.title.casefold() not in first_page.casefold():
        raise PaperValidationError(f"{spec.paper_id}: expected title not found on page 1")
    return page_count, metadata, stamp


def download_pdf(spec: PaperSpec, client: httpx.Client) -> bytes:
    try:
        response = client.get(spec.url)
    except httpx.HTTPError as exc:
        raise PaperFetchError(f"{spec.paper_id}: download failed ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise PaperFetchError(f"{spec.paper_id}: HTTP {response.status_code} from {spec.url}")
    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type.lower():
        raise PaperFetchError(f"{spec.paper_id}: unexpected content-type {content_type!r}")
    return response.content


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"user-agent": USER_AGENT},
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure_paper(
    spec: PaperSpec,
    papers_dir: Path,
    client: httpx.Client,
    *,
    existing: PaperRecord | None,
    refresh: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[PaperRecord, bool]:
    """Validate the cached PDF or download it. Returns (record, downloaded)."""
    path = papers_dir / spec.file_name
    downloaded = refresh or not path.is_file()
    data = download_pdf(spec, client) if downloaded else path.read_bytes()
    page_count, metadata, stamp = validate_pdf_bytes(spec, data)
    digest = sha256_hex(data)
    if existing is not None and existing.sha256 != digest:
        raise ManifestError(
            f"{spec.paper_id}: SHA-256 differs from the manifest; arXiv versions are immutable, "
            "so the file or manifest is wrong. Investigate before continuing."
        )
    if downloaded:
        papers_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, data)
    fetched_at = existing.fetched_at if existing is not None else None
    if downloaded and fetched_at is None:
        fetched_at = now().astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    record = PaperRecord(
        paper_id=spec.paper_id,
        arxiv_id=spec.arxiv_id,
        version=spec.version,
        short_name=spec.short_name,
        title=spec.title,
        source_url=spec.url,
        file_name=spec.file_name,
        sha256=digest,
        size_bytes=len(data),
        page_count=page_count,
        version_stamp=stamp,
        pdf_metadata=metadata,
        fetched_at=fetched_at,
    )
    return record, downloaded


def load_corpus_manifest(papers_dir: Path) -> CorpusManifest:
    path = papers_dir / MANIFEST_FILE
    if not path.is_file():
        return CorpusManifest()
    try:
        return CorpusManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=False)
    except ValidationError as exc:
        raise ManifestError(f"invalid manifest {path}: {exc.error_count()} errors") from exc


def render_manifest(manifest: CorpusManifest) -> str:
    return json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def write_corpus_manifest(papers_dir: Path, manifest: CorpusManifest) -> None:
    papers_dir.mkdir(parents=True, exist_ok=True)
    path = papers_dir / MANIFEST_FILE
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(render_manifest(manifest), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


__all__ = [
    "PAPERS",
    "CorpusManifest",
    "PaperRecord",
    "PaperSpec",
    "download_pdf",
    "ensure_paper",
    "load_corpus_manifest",
    "make_client",
    "normalise_text",
    "render_manifest",
    "sha256_hex",
    "validate_pdf_bytes",
    "write_corpus_manifest",
]
