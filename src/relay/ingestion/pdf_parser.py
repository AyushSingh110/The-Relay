"""Column-aware, section-tagged text extraction with PyMuPDF."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import pymupdf
from pydantic import BaseModel, ConfigDict

from relay.ingestion.errors import IngestionError

PARSER_VERSION: Final = "p03-1"

TOP_MARGIN_FRACTION: Final = 0.075
BOTTOM_MARGIN_FRACTION: Final = 0.9
RUNNING_HEADER_MIN_PAGES: Final = 3
COLUMN_GAP_PT: Final = 12.0
COLUMN_MIN_SHARE: Final = 0.2
COLUMN_PARAGRAPH_WORDS: Final = 15
WORD_SOUP_MIN_BLOCKS: Final = 40
WORD_SOUP_SHARE: Final = 0.6
GLYPH_MIN_TOKENS: Final = 10
GLYPH_TOKEN_CHARS: Final = 2
GLYPH_SHORT_SHARE: Final = 0.8
LOW_TEXT_WORDS: Final = 80
SCRIPT_SIZE_RATIO: Final = 0.8

SectionKind = Literal[
    "front_matter", "abstract", "body", "acknowledgements", "references", "appendix"
]
Layout = Literal["single_column", "two_column", "no_text"]

_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")
_NUMBERED = re.compile(r"^(?:appendix\s+)?((?:[A-Z]|\d+)(?:\.\d+)*)\.?\s+(\S.*)$", re.IGNORECASE)
_PAGE_NUMBER = re.compile(r"^(\d{1,4}|[ivxlcdm]{1,8})$", re.IGNORECASE)
_TABLE_CAPTION = re.compile(r"^Table\s+\d+[:.]")
_CONNECTORS: Final = frozenset({"to", "of", "the", "and", "in", "on", "for", "by"})


class Section(BaseModel):
    model_config = _FROZEN

    section_id: str
    number: str | None
    title: str
    level: int
    kind: SectionKind
    is_reference: bool
    page_start: int
    path: tuple[str, ...]


class Block(BaseModel):
    model_config = _FROZEN

    order: int
    page: int
    kind: Literal["heading", "text"]
    text: str
    bbox: tuple[float, float, float, float]
    section_id: str


class PageReport(BaseModel):
    model_config = _FROZEN

    page: int
    layout: Layout
    kept_blocks: int
    words: int
    dropped: dict[str, int]
    flags: tuple[str, ...]


class ParsedPaper(BaseModel):
    model_config = _FROZEN

    paper_id: str
    parser_version: str
    page_count: int
    sections: tuple[Section, ...]
    blocks: tuple[Block, ...]
    pages: tuple[PageReport, ...]
    issues: tuple[str, ...]
    hyphenation: dict[str, int]
    hyphen_merges_unverified: tuple[str, ...]
    hyphen_kept_compounds: tuple[str, ...]

    def section(self, section_id: str) -> Section:
        for section in self.sections:
            if section.section_id == section_id:
                return section
        raise KeyError(section_id)

    def page_text(self, page: int) -> str:
        return "\n".join(b.text for b in self.blocks if b.page == page)


# --- Page reading ---------------------------------------------------------------------


@dataclass
class _RawBlock:
    page: int
    bbox: tuple[float, float, float, float]
    lines: list[str]

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def flat(self) -> str:
        return " ".join(" ".join(self.lines).split())


@dataclass
class _Page:
    number: int
    width: float
    height: float
    blocks: list[_RawBlock]
    dropped: Counter[str] = field(default_factory=Counter)
    flags: set[str] = field(default_factory=set)
    layout: Layout = "single_column"


def _line_text(line: dict[str, Any]) -> str:
    """Join spans, marking small raised/lowered spans as super/subscripts."""
    spans = [s for s in line["spans"] if s["text"]]
    if not spans:
        return ""
    dominant = max(spans, key=lambda s: len(s["text"].strip()) * s["size"])
    size, baseline = dominant["size"], dominant["origin"][1]
    out = ""
    after_script = False
    for span in spans:
        text = span["text"]
        small = span["size"] <= SCRIPT_SIZE_RATIO * size and 0 < len(text.strip()) <= 8
        attached = bool(out) and not out[-1].isspace() and not text[0].isspace()
        if small and attached:
            shift = span["origin"][1] - baseline
            if shift < -0.15 * size or shift > 0.1 * size:
                out += ("^" if shift < 0 else "_") + text
                after_script = True
                continue
        elif small and not out and text.strip() and span is not spans[-1]:
            text = text + " "  # leading footnote marker: keep it apart from the next word
        elif after_script and not small and text[:1].isalpha():
            text = " " + text  # "D^s" + "dimensions" -> "D^s dimensions"
        after_script = False
        out += text
    return out


def _ordered_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lines of one block in rows (vertical overlap > 50%), left to right within a row."""
    ordered: list[dict[str, Any]] = []
    row: list[dict[str, Any]] = []
    for line in sorted(lines, key=lambda ln: (ln["bbox"][1], ln["bbox"][0])):
        if row:
            top, bottom = min(r["bbox"][1] for r in row), max(r["bbox"][3] for r in row)
            shared = min(bottom, line["bbox"][3]) - max(top, line["bbox"][1])
            smaller = max(1.0, min(bottom - top, line["bbox"][3] - line["bbox"][1]))
            if shared > 0.5 * smaller:
                row.append(line)
                continue
            ordered.extend(sorted(row, key=lambda ln: ln["bbox"][0]))
        row = [line]
    ordered.extend(sorted(row, key=lambda ln: ln["bbox"][0]))
    return ordered


def _read_page(page: pymupdf.Page) -> _Page:
    raw = page.get_text("dict")
    result = _Page(page.number + 1, page.rect.width, page.rect.height, [])
    for block in raw["blocks"]:
        if block["type"] != 0:
            result.dropped["image"] += 1
            continue
        lines = _ordered_lines(
            [ln for ln in block["lines"] if "".join(s["text"] for s in ln["spans"]).strip()]
        )
        if not lines:
            continue
        if any(abs(ln["dir"][1]) > 0.1 for ln in lines):
            result.dropped["rotated_text"] += 1
            continue
        bbox = tuple(round(float(v), 2) for v in block["bbox"])
        texts = [_line_text(ln) for ln in lines]
        result.blocks.append(_RawBlock(result.number, bbox, texts))  # type: ignore[arg-type]
    return result


def _norm_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _drop_margins(pages: list[_Page]) -> None:
    counts: Counter[str] = Counter()
    for page in pages:
        for block in page.blocks:
            if block.y1 <= TOP_MARGIN_FRACTION * page.height:
                counts[_norm_key(block.flat)] += 1
    repeated = {key for key, n in counts.items() if n >= RUNNING_HEADER_MIN_PAGES and key}
    for page in pages:
        kept: list[_RawBlock] = []
        for block in page.blocks:
            top = block.y1 <= TOP_MARGIN_FRACTION * page.height
            bottom = block.y0 >= BOTTOM_MARGIN_FRACTION * page.height
            if top and _norm_key(block.flat) in repeated:
                page.dropped["running_header"] += 1
            elif bottom and _PAGE_NUMBER.match(block.flat.strip()):
                page.dropped["page_number"] += 1
            else:
                kept.append(block)
        page.blocks = kept


def _is_glyph_text(block: _RawBlock) -> bool:
    """Figure glyphs: letter-spaced text, byte hex dumps, axis-tick runs."""
    text = block.flat
    if "=" in text:  # equations and table headers ("k = 1 k = 2 ...") are content
        return False
    tokens = text.split()
    short = sum(1 for token in tokens if len(token) <= GLYPH_TOKEN_CHARS)
    return len(tokens) >= GLYPH_MIN_TOKENS and short >= GLYPH_SHORT_SHARE * len(tokens)


def _drop_figure_text(page: _Page) -> None:
    glyphs = [b for b in page.blocks if _is_glyph_text(b)]
    if glyphs:
        glyph_ids = {id(b) for b in glyphs}
        page.blocks = [b for b in page.blocks if id(b) not in glyph_ids]
        page.dropped["figure_glyph_text"] += len(glyphs)
        page.flags.add("figure_text_removed")
    tiny = [b for b in page.blocks if len(b.lines) == 1 and len(b.flat.split()) <= 2]
    if len(page.blocks) >= WORD_SOUP_MIN_BLOCKS and len(tiny) >= WORD_SOUP_SHARE * len(page.blocks):
        tiny_ids = {id(b) for b in tiny}
        page.blocks = [b for b in page.blocks if id(b) not in tiny_ids]
        page.dropped["figure_word_soup"] += len(tiny)
        page.flags.add("figure_text_removed")


def _row_order(row: list[_RawBlock]) -> list[_RawBlock]:
    """Order a row: overlapping blocks stack top-down, stacks read left to right."""
    stacks: list[list[_RawBlock]] = []
    for block in sorted(row, key=lambda b: b.x0):
        for stack in stacks:
            if any(min(b.x1, block.x1) - max(b.x0, block.x0) > 0 for b in stack):
                stack.append(block)
                break
        else:
            stacks.append([block])
    stacks.sort(key=lambda stack: min(b.x0 for b in stack))
    return [b for stack in stacks for b in sorted(stack, key=lambda b: (b.y0, b.x0))]


def _rows(blocks: Iterable[_RawBlock]) -> list[_RawBlock]:
    """Top-to-bottom rows (blocks overlapping vertically by > 50%)."""
    ordered: list[_RawBlock] = []
    row: list[_RawBlock] = []
    for block in sorted(blocks, key=lambda b: (b.y0, b.x0)):
        if row:
            top, bottom = min(b.y0 for b in row), max(b.y1 for b in row)
            shared = min(bottom, block.y1) - max(top, block.y0)
            smaller = max(1.0, min(bottom - top, block.y1 - block.y0))
            if shared > 0.5 * smaller:
                row.append(block)
                continue
            ordered.extend(_row_order(row))
        row = [block]
    ordered.extend(_row_order(row))
    return ordered


def _order_page(page: _Page) -> list[_RawBlock]:
    if not page.blocks:
        page.layout = "no_text"
        return []
    mid = page.width / 2
    left = [b for b in page.blocks if b.x1 <= mid + COLUMN_GAP_PT]
    right = [b for b in page.blocks if b.x0 >= mid - COLUMN_GAP_PT]
    # Only paragraph-sized blocks decide the layout; figure labels and ticks do not.
    para_left = [b for b in left if len(b.flat.split()) >= COLUMN_PARAGRAPH_WORDS]
    para_right = [b for b in right if len(b.flat.split()) >= COLUMN_PARAGRAPH_WORDS]
    total = sum(len(b.flat) for b in page.blocks) or 1
    left_share = sum(len(b.flat) for b in para_left) / total
    right_share = sum(len(b.flat) for b in para_right) / total
    two_column = (
        len(para_left) >= 2 and len(para_right) >= 2
        and left_share >= COLUMN_MIN_SHARE and right_share >= COLUMN_MIN_SHARE
    )
    if not two_column:
        page.layout = "single_column"
        return _rows(page.blocks)
    page.layout = "two_column"
    left_ids, right_ids = {id(b) for b in left}, {id(b) for b in right}
    ordered: list[_RawBlock] = []
    band: list[_RawBlock] = []

    def flush() -> None:
        ordered.extend(_rows(b for b in band if id(b) in left_ids))
        ordered.extend(_rows(b for b in band if id(b) in right_ids))
        band.clear()

    for block in sorted(page.blocks, key=lambda b: (b.y0, b.x0)):
        if id(block) in left_ids or id(block) in right_ids:
            band.append(block)
        else:  # spans both columns: closes the current band
            flush()
            ordered.append(block)
    flush()
    return ordered


# --- Text assembly --------------------------------------------------------------------


@dataclass
class _Hyphenation:
    known_hyphenated: set[str]
    known_words: set[str]
    stats: Counter[str] = field(default_factory=Counter)
    unverified: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    def join(self, head: str, tail: str) -> str:
        """Join a line ending in '-' with the next line."""
        stem = re.search(r"([A-Za-z]+)-$", head)
        first = re.match(r"([A-Za-z]+)", tail)
        if not stem or not first or not tail[:1].islower():
            self.stats["kept_hyphen_non_lowercase"] += 1
            return head + tail
        hyphenated = f"{stem.group(1)}-{first.group(1)}".lower()
        merged = f"{stem.group(1)}{first.group(1)}".lower()
        if hyphenated in self.known_hyphenated and merged not in self.known_words:
            self.stats["kept_hyphen_known_compound"] += 1
            return head + tail
        if merged in self.known_words:
            self.stats["merged_known_word"] += 1
        elif (
            stem.group(1).lower() in self.known_words
            and first.group(1) in self.known_words
            and (
                len(first.group(1)) >= 4
                or first.group(1) in _CONNECTORS
                or stem.group(1).lower() in _CONNECTORS  # "state-of-the-" + "art"
            )
        ):
            # Both halves are words seen elsewhere ("sequence-aligned"): a compound.
            self.stats["kept_hyphen_both_parts_words"] += 1
            self.kept.append(hyphenated)
            return head + tail
        else:
            self.stats["merged_unverified"] += 1
            self.unverified.append(merged)
        return head[:-1] + tail


def _vocabulary(blocks: Iterable[_RawBlock]) -> tuple[set[str], set[str]]:
    hyphenated: set[str] = set()
    words: set[str] = set()
    for block in blocks:
        for line in block.lines:
            tokens = line.split()
            for index, token in enumerate(tokens):
                if index == len(tokens) - 1 and token.endswith("-"):
                    continue  # a line-end fragment, not evidence
                lowered = token.lower().strip(".,;:()[]\"'")
                if re.fullmatch(r"[a-z]+-[a-z]+", lowered):
                    hyphenated.add(lowered)
                elif re.fullmatch(r"[a-z]+", lowered):
                    words.add(lowered)  # standalone words only, never parts of compounds
    return hyphenated, words


def _clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _assemble(block: _RawBlock, hyphenation: _Hyphenation) -> list[str]:
    """Return the block's lines after hyphen repair (still line-separated)."""
    lines: list[str] = []
    for raw in block.lines:
        line = raw.strip()
        if lines and lines[-1].endswith("-"):
            lines[-1] = hyphenation.join(lines[-1], line)
        else:
            lines.append(line)
    return lines


# --- Sections -------------------------------------------------------------------------


@dataclass(frozen=True)
class _TocEntry:
    level: int
    title: str
    page: int


def _split_number(text: str) -> tuple[str | None, str]:
    match = _NUMBERED.match(text)
    if match and _norm_key(match.group(2)):
        return match.group(1), match.group(2)
    return None, text


def _special_kind(text: str) -> SectionKind | None:
    key = _norm_key(text)
    if len(key.split()) > 6:
        return None
    if key == "abstract":
        return "abstract"
    if key.startswith("acknowledg"):
        return "acknowledgements"
    if key in ("references", "bibliography"):
        return "references"
    return None


@dataclass
class _SectionBuilder:
    paper_id: str
    sections: list[Section] = field(default_factory=list)
    stack: list[tuple[int, str]] = field(default_factory=list)
    after_references: bool = False

    def open(self, title: str, number: str | None, level: int, kind: SectionKind,
             page: int) -> Section:
        if kind == "references":
            self.after_references = True
        elif kind == "body" and self.after_references:
            kind = "appendix"
        label = f"{number} {title}" if number else title
        if kind in ("body", "appendix"):
            self.stack = [(lvl, name) for lvl, name in self.stack if lvl < level]
            self.stack.append((level, label))
            path = tuple(name for _, name in self.stack)
        else:
            self.stack = []
            path = (label,)
        section = Section(
            section_id=f"{self.paper_id}_s{len(self.sections):03d}",
            number=number,
            title=title,
            level=level,
            kind=kind,
            is_reference=kind == "references",
            page_start=page,
            path=path,
        )
        self.sections.append(section)
        return section


def _toc_match(lines: Sequence[str], entry: _TocEntry) -> tuple[int, str | None] | None:
    """(lines used, heading number) if the block starts with this TOC entry's heading."""
    target = _norm_key(entry.title)
    for used in (1, 2, 3):
        if used > len(lines):
            break
        text = _clean(" ".join(lines[:used]))
        number, title = _split_number(text)
        if number is not None and _norm_key(title) == target:
            return used, number
        if _norm_key(text) == target:
            return used, None
    return None


@dataclass
class _Assembled:
    page: int
    raw: _RawBlock
    lines: list[str]


_Heading = tuple[int, str | None, str, int, SectionKind]


def _locate_headings(
    blocks: Sequence[_Assembled], toc: Sequence[_TocEntry], extra: set[str], issues: list[str]
) -> dict[int, _Heading]:
    """Map block index -> heading."""
    found: dict[int, _Heading] = {}
    cursor = 0
    for entry in toc:
        best: tuple[int, tuple[int, str | None]] | None = None
        for index in range(cursor, len(blocks)):
            block = blocks[index]
            if block.page < entry.page:
                continue
            if block.page > entry.page + 1:
                break
            match = _toc_match(block.lines, entry)
            if match is None or index in found:
                continue
            if match[1] is not None:
                best = (index, match)
                break
            if best is None:
                best = (index, match)
        if best is None:
            issues.append(f"TOC heading not found: {entry.title!r} (page {entry.page})")
            continue
        index, (used, number) = best
        found[index] = (used, number, entry.title, entry.level, "body")
        cursor = index + 1
    for index, block in enumerate(blocks):
        if index in found or not block.lines:
            continue
        first = _clean(block.lines[0])
        kind = _special_kind(first)
        if kind is not None:
            found[index] = (1, None, first, 1, kind)
        elif _norm_key(first) in extra:
            found[index] = (1, None, first, 1, "body")
    return found


def _page_flags(page: _Page, words: int, texts: Sequence[str]) -> None:
    if words < LOW_TEXT_WORDS:
        page.flags.add("low_text")
    if any(_TABLE_CAPTION.match(text) for text in texts):
        page.flags.add("contains_table")
    joined = "".join(texts)
    if joined:
        symbols = sum(1 for ch in joined if not (ch.isascii() or ch.isspace()))
        if symbols / len(joined) > 0.03 or joined.count("^") + joined.count("_") > 25:
            page.flags.add("math_heavy")
    if page.layout == "two_column":
        page.flags.add("two_column_reordered")


# --- Entry point ----------------------------------------------------------------------


def parse_document(
    doc: pymupdf.Document, paper_id: str, *, extra_headings: Iterable[str] = ()
) -> ParsedPaper:
    if doc.page_count < 1:
        raise IngestionError(f"{paper_id}: document has no pages")
    pages = [_read_page(page) for page in doc]
    _drop_margins(pages)
    ordered_pages: list[tuple[_Page, list[_RawBlock]]] = []
    for page in pages:
        _drop_figure_text(page)
        ordered_pages.append((page, _order_page(page)))

    hyphenated, words = _vocabulary(b for _, blocks in ordered_pages for b in blocks)
    hyphenation = _Hyphenation(hyphenated, words)
    assembled = [
        _Assembled(page.number, raw, _assemble(raw, hyphenation))
        for page, raw_blocks in ordered_pages
        for raw in raw_blocks
    ]
    toc = [_TocEntry(level, title, page) for level, title, page, *_ in doc.get_toc(simple=True)]
    issues: list[str] = []
    headings = _locate_headings(assembled, toc, {_norm_key(h) for h in extra_headings}, issues)

    builder = _SectionBuilder(paper_id)
    current = builder.open("Front matter", None, 1, "front_matter", 1)
    blocks: list[Block] = []
    page_texts: dict[int, list[str]] = {page.number: [] for page in pages}
    page_words: Counter[int] = Counter()
    for index, item in enumerate(assembled):
        lines = item.lines
        heading = headings.get(index)
        if heading is not None:
            used, number, title, level, kind = heading
            current = builder.open(title, number, level, kind, item.page)
            heading_text = _clean(" ".join(lines[:used]))
            blocks.append(Block(order=len(blocks), page=item.page, kind="heading",
                                text=heading_text, bbox=item.raw.bbox,
                                section_id=current.section_id))
            page_texts[item.page].append(heading_text)
            lines = lines[used:]
        text = _clean(" ".join(lines))
        if not text:
            continue
        blocks.append(Block(order=len(blocks), page=item.page, kind="text", text=text,
                            bbox=item.raw.bbox, section_id=current.section_id))
        page_texts[item.page].append(text)
        page_words[item.page] += len(text.split())

    reports: list[PageReport] = []
    for page in pages:
        _page_flags(page, page_words[page.number], page_texts[page.number])
        reports.append(PageReport(
            page=page.number,
            layout=page.layout,
            kept_blocks=sum(1 for b in blocks if b.page == page.number),
            words=page_words[page.number],
            dropped=dict(sorted(page.dropped.items())),
            flags=tuple(sorted(page.flags)),
        ))
    return ParsedPaper(
        paper_id=paper_id,
        parser_version=PARSER_VERSION,
        page_count=doc.page_count,
        sections=tuple(builder.sections),
        blocks=tuple(blocks),
        pages=tuple(reports),
        issues=tuple(issues),
        hyphenation=dict(sorted(hyphenation.stats.items())),
        hyphen_merges_unverified=tuple(sorted(set(hyphenation.unverified))),
        hyphen_kept_compounds=tuple(sorted(set(hyphenation.kept))),
    )


def parse_pdf(path: Path, paper_id: str, *, extra_headings: Iterable[str] = ()) -> ParsedPaper:
    with pymupdf.open(path) as doc:
        return parse_document(doc, paper_id, extra_headings=extra_headings)


__all__ = [
    "PARSER_VERSION",
    "Block",
    "PageReport",
    "ParsedPaper",
    "Section",
    "parse_document",
    "parse_pdf",
]
