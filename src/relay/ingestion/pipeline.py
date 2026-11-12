"""End-to-end ingestion: fetch/validate -> parse -> chunk -> write artefacts + manifest.

Outputs:
- ``data/papers/<paper_id>.pdf`` (gitignored) and ``data/papers/manifest.json`` (tracked):
  provenance (SHA-256, size, page count, version stamp) plus a deterministic extraction
  summary (section list, flagged pages, drop counts, chunk counts and SHA-256 digests).
- ``data/processed/`` (gitignored, regenerable): per paper ``blocks.jsonl``,
  ``sections.json``, ``pages.json``; ``chunks/<preset>.jsonl`` across all papers.

The manifest contains no timestamps except each paper's first ``fetched_at``, so a re-run
on the same PDFs reproduces it byte for byte.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pymupdf
from pydantic import BaseModel, ConfigDict

from relay.ingestion.arxiv_fetcher import (
    PAPERS,
    CorpusManifest,
    PaperRecord,
    PaperSpec,
    ensure_paper,
    load_corpus_manifest,
    make_client,
    render_manifest,
)
from relay.ingestion.chunker import (
    CHUNK_PRESETS,
    Chunk,
    ChunkPreset,
    chunk_paper,
    digest,
    render_jsonl,
)
from relay.ingestion.errors import PaperValidationError
from relay.ingestion.pdf_parser import PARSER_VERSION, ParsedPaper, parse_pdf


class IngestResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    manifest: CorpusManifest
    parsed: dict[str, ParsedPaper]
    chunks: dict[str, tuple[Chunk, ...]]  # preset -> chunks across all papers
    downloaded: tuple[str, ...]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _pages_with(parsed: ParsedPaper, flag: str) -> list[int]:
    return [page.page for page in parsed.pages if flag in page.flags]


def extraction_summary(
    parsed: ParsedPaper, chunks: dict[str, tuple[Chunk, ...]]
) -> dict[str, Any]:
    dropped: Counter[str] = Counter()
    for page in parsed.pages:
        dropped.update(page.dropped)
    flags = sorted({flag for page in parsed.pages for flag in page.flags})
    reference_pages = sorted({
        block.page for block in parsed.blocks if parsed.section(block.section_id).is_reference
    })
    return {
        "pages": parsed.page_count,
        "words": sum(page.words for page in parsed.pages),
        "blocks": len(parsed.blocks),
        "blocks_sha256": digest(render_jsonl(parsed.blocks)),
        "sections": [
            {"id": s.section_id, "number": s.number, "title": s.title, "kind": s.kind,
             "page_start": s.page_start}
            for s in parsed.sections
        ],
        "reference_pages": reference_pages,
        "toc_issues": list(parsed.issues),
        "dropped_blocks": dict(sorted(dropped.items())),
        "flagged_pages": {flag: _pages_with(parsed, flag) for flag in flags},
        "pages_without_text": [p.page for p in parsed.pages if p.layout == "no_text"],
        "hyphenation": parsed.hyphenation,
        "hyphen_merges_unverified": list(parsed.hyphen_merges_unverified),
        "hyphen_kept_compounds": list(parsed.hyphen_kept_compounds),
        "chunks": {
            preset: {
                "count": sum(1 for c in preset_chunks if c.paper_id == parsed.paper_id),
                "reference_chunks": sum(
                    1 for c in preset_chunks if c.paper_id == parsed.paper_id and c.is_reference
                ),
            }
            for preset, preset_chunks in chunks.items()
        },
    }


def run_ingest(
    papers_dir: Path,
    processed_dir: Path,
    *,
    specs: Sequence[PaperSpec] = PAPERS,
    presets: Sequence[ChunkPreset] = CHUNK_PRESETS,
    offline: bool = False,
    refresh: bool = False,
    write: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> IngestResult:
    existing = load_corpus_manifest(papers_dir)
    records: list[PaperRecord] = []
    downloaded: list[str] = []
    with make_client(transport) as client:
        for spec in specs:
            if offline and not (papers_dir / spec.file_name).is_file():
                raise PaperValidationError(f"{spec.paper_id}: PDF missing and --offline is set")
            record, fetched = ensure_paper(
                spec, papers_dir, client, existing=existing.record_for(spec.paper_id),
                refresh=refresh and not offline,
            )
            records.append(record)
            if fetched:
                downloaded.append(spec.paper_id)

    parsed = {
        spec.paper_id: parse_pdf(papers_dir / spec.file_name, spec.paper_id,
                                 extra_headings=spec.extra_headings)
        for spec in specs
    }
    chunks = {
        preset.name: tuple(c for spec in specs for c in chunk_paper(parsed[spec.paper_id], spec,
                                                                     preset))
        for preset in presets
    }
    for preset_chunks in chunks.values():
        ids = [c.chunk_id for c in preset_chunks]
        if len(ids) != len(set(ids)):
            raise PaperValidationError("duplicate chunk_id within a preset")

    manifest = CorpusManifest(
        papers=tuple(sorted(records, key=lambda r: r.paper_id)),
        extraction={
            "parser_version": PARSER_VERSION,
            "pymupdf_version": pymupdf.VersionBind,
            "chunk_presets": [p.model_dump() for p in presets],
            "papers": {pid: extraction_summary(parsed[pid], chunks) for pid in sorted(parsed)},
            "chunk_sets": {
                name: {"count": len(items), "sha256": digest(render_jsonl(items))}
                for name, items in chunks.items()
            },
        },
    )
    if write:
        for pid, paper in parsed.items():
            base = processed_dir / pid
            _write_text(base / "blocks.jsonl", render_jsonl(paper.blocks))
            _write_text(base / "sections.json", json.dumps(
                [s.model_dump(mode="json") for s in paper.sections], indent=2) + "\n")
            _write_text(base / "pages.json", json.dumps(
                [p.model_dump(mode="json") for p in paper.pages], indent=2) + "\n")
        for name, items in chunks.items():
            _write_text(processed_dir / "chunks" / f"{name}.jsonl", render_jsonl(items))
        _write_text(papers_dir / "manifest.json", render_manifest(manifest))
    return IngestResult(manifest=manifest, parsed=parsed, chunks=chunks,
                        downloaded=tuple(downloaded))


__all__ = ["IngestResult", "extraction_summary", "run_ingest"]
