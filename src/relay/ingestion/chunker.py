"""Deterministic word-window chunking (ARCHITECTURE.md section 6; sizes in words)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.ingestion.arxiv_fetcher import PaperSpec
from relay.ingestion.pdf_parser import ParsedPaper, SectionKind

_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")


class ChunkPreset(BaseModel):
    model_config = _FROZEN

    name: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    size_words: Annotated[int, Field(ge=20)]
    overlap_words: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _check_overlap(self) -> ChunkPreset:
        if self.overlap_words * 2 >= self.size_words:
            raise ValueError("overlap_words must be less than half of size_words")
        return self


# Sizes finalised in P04 from measured token counts (reranked preset fits 512 tokens).
CHUNK_PRESETS: Final[tuple[ChunkPreset, ...]] = (
    ChunkPreset(name="small_fast", size_words=160, overlap_words=25),
    ChunkPreset(name="balanced_rerank", size_words=220, overlap_words=35),
    ChunkPreset(name="large_wide", size_words=400, overlap_words=60),
)


class Chunk(BaseModel):
    model_config = _FROZEN

    chunk_id: str
    preset: str
    paper_id: str
    arxiv_id: str
    version: int
    short_name: str
    section_id: str
    section_number: str | None
    section_title: str
    section_path: tuple[str, ...]
    section_kind: SectionKind
    is_reference: bool
    page_start: int
    page_end: int
    word_start: int  # offset within the section's word stream
    word_end: int  # exclusive
    word_count: int
    text: str


def _section_streams(parsed: ParsedPaper) -> dict[str, list[tuple[str, int]]]:
    streams: dict[str, list[tuple[str, int]]] = {s.section_id: [] for s in parsed.sections}
    for block in parsed.blocks:
        if block.kind == "text":
            streams[block.section_id].extend((word, block.page) for word in block.text.split())
    return streams


def _windows(length: int, size: int, overlap: int) -> Iterable[tuple[int, int]]:
    start = 0
    while start < length:
        end = min(start + size, length)
        yield start, end
        if end == length:
            return
        start = end - overlap


def chunk_paper(parsed: ParsedPaper, spec: PaperSpec, preset: ChunkPreset) -> tuple[Chunk, ...]:
    if parsed.paper_id != spec.paper_id:
        raise ValueError(f"parsed paper {parsed.paper_id} does not match spec {spec.paper_id}")
    streams = _section_streams(parsed)
    per_page: Counter[int] = Counter()
    chunks: list[Chunk] = []
    for section in parsed.sections:
        words = streams[section.section_id]
        for start, end in _windows(len(words), preset.size_words, preset.overlap_words):
            window = words[start:end]
            page_start = window[0][1]
            chunk_id = f"{parsed.paper_id}_p{page_start}_c{per_page[page_start]}"
            per_page[page_start] += 1
            chunks.append(Chunk(
                chunk_id=chunk_id,
                preset=preset.name,
                paper_id=parsed.paper_id,
                arxiv_id=spec.arxiv_id,
                version=spec.version,
                short_name=spec.short_name,
                section_id=section.section_id,
                section_number=section.number,
                section_title=section.title,
                section_path=section.path,
                section_kind=section.kind,
                is_reference=section.is_reference,
                page_start=page_start,
                page_end=max(page for _, page in window),
                word_start=start,
                word_end=end,
                word_count=end - start,
                text=" ".join(word for word, _ in window),
            ))
    return tuple(chunks)


def render_jsonl(records: Sequence[BaseModel]) -> str:
    return "".join(
        json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n"
        for record in records
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["CHUNK_PRESETS", "Chunk", "ChunkPreset", "chunk_paper", "digest", "render_jsonl"]
