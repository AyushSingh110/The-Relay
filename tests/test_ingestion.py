"""P03 ingestion tests: PDF validation, fetching, extraction, reading order, sections,
references, hyphenation, chunk boundaries and reproducibility.

All PDFs here are synthetic, built with PyMuPDF; arXiv is never contacted (MockTransport).
The final test checks the real corpus and is skipped when the PDFs are not present.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest

from relay.ingestion import pdf_parser as pp
from relay.ingestion.arxiv_fetcher import (
    PAPERS,
    CorpusManifest,
    PaperSpec,
    ensure_paper,
    load_corpus_manifest,
    make_client,
    render_manifest,
    sha256_hex,
    validate_pdf_bytes,
    write_corpus_manifest,
)
from relay.ingestion.chunker import Chunk, ChunkPreset, chunk_paper, render_jsonl
from relay.ingestion.errors import ManifestError, PaperFetchError, PaperValidationError
from relay.ingestion.pdf_parser import Block, PageReport, ParsedPaper, Section, parse_document
from relay.ingestion.pipeline import run_ingest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = PaperSpec(arxiv_id="1234.56789", version=2, short_name="synthetic",
                 title="A Synthetic Paper About Chunking")
LOREM = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi "
    "rho sigma tau upsilon phi chi psi omega"
)

PageBuilder = Callable[[pymupdf.Page], None]


def words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{i}" for i in range(count))


def make_pdf(builders: list[PageBuilder], toc: list[list[Any]] | None = None) -> bytes:
    doc = pymupdf.open()
    for build in builders:
        build(doc.new_page(width=612, height=792))
    if toc:
        doc.set_toc(toc)
    return doc.tobytes()


def box(page: pymupdf.Page, rect: tuple[float, float, float, float], text: str,
        size: float = 10) -> None:
    x0, y0, x1, y1 = rect
    y1 = max(y1, y0 + 2 * size)  # text is drawn from the top; only the box grows
    rc = page.insert_textbox(pymupdf.Rect(x0, y0, x1, y1), text, fontsize=size, fontname="helv")
    assert rc >= 0, "fixture text overflowed its box"


def stamp(page: pymupdf.Page, paper_id: str) -> None:
    page.insert_text((25, 500), f"arXiv:{paper_id} [cs.CL] 1 Jan 2025", fontsize=12, rotate=90)


def front_page(spec: PaperSpec = SPEC) -> PageBuilder:
    def build(page: pymupdf.Page) -> None:
        stamp(page, spec.paper_id)
        box(page, (100, 80, 520, 110), spec.title, 14)
        box(page, (100, 140, 520, 160), "Abstract", 12)
        box(page, (100, 170, 520, 260), "We study " + LOREM)
        box(page, (300, 740, 320, 755), "1")

    return build


def body_page(page: pymupdf.Page) -> None:
    box(page, (72, 100, 540, 400), " ".join([LOREM] * 6))


def parse_bytes(data: bytes, paper_id: str = SPEC.paper_id, **kwargs: Any) -> ParsedPaper:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return parse_document(doc, paper_id, **kwargs)


def texts(parsed: ParsedPaper, page: int | None = None) -> list[str]:
    return [b.text for b in parsed.blocks if page is None or b.page == page]


# --- Paper registry and validation --------------------------------------------------


def test_registry_pins_exact_versions_and_urls() -> None:
    assert [(p.paper_id, p.url) for p in PAPERS] == [
        ("1706.03762v7", "https://arxiv.org/pdf/1706.03762v7"),
        ("2410.00037v1", "https://arxiv.org/pdf/2410.00037v1"),
        ("2507.07955v2", "https://arxiv.org/pdf/2507.07955v2"),
    ]


def test_valid_pdf_is_accepted() -> None:
    data = make_pdf([front_page(), lambda page: box(page, (100, 100, 500, 200), LOREM)])
    pages, metadata, version_stamp = validate_pdf_bytes(SPEC, data)
    assert (pages, version_stamp) == (2, "arXiv:1234.56789v2")
    assert isinstance(metadata, dict)


@pytest.mark.parametrize(
    ("data_factory", "message"),
    [
        (lambda: b"<html>not a pdf</html>" * 100, "missing %PDF- signature"),
        (lambda: b"%PDF-1.7\n", "implausible PDF size"),
        (lambda: b"%PDF-1.7\n" + b"0" * 5000, "cannot be opened"),
        (lambda: make_pdf([lambda page: box(page, (100, 80, 520, 110), SPEC.title), body_page]),
         "no arXiv stamp found"),
        (lambda: make_pdf([front_page(SPEC.model_copy(update={"version": 1}))]),
         "found ['arXiv:1234.56789v1']"),
        (lambda: make_pdf([lambda page: stamp(page, SPEC.paper_id), body_page]),
         "expected title not found"),
    ],
    ids=["not-pdf", "too-small", "corrupt", "no-stamp", "wrong-version", "no-title"],
)
def test_invalid_pdfs_are_rejected(data_factory: Callable[[], bytes], message: str) -> None:
    with pytest.raises(PaperValidationError) as info:
        validate_pdf_bytes(SPEC, data_factory())
    assert message in str(info.value)


def test_encrypted_pdf_is_rejected() -> None:
    doc = pymupdf.open()
    front_page()(doc.new_page(width=612, height=792))
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(PaperValidationError, match="encrypted"):
        validate_pdf_bytes(SPEC, data)


# --- Fetching -----------------------------------------------------------------------


class Server:
    def __init__(self, response: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response(request)


def pdf_server(data: bytes) -> Server:
    return Server(lambda request: httpx.Response(
        200, content=data, headers={"content-type": "application/pdf"}))


def fixed_now() -> datetime:
    return datetime(2026, 11, 12, 3, 0, tzinfo=UTC)


def test_download_validates_writes_and_records(tmp_path: Path) -> None:
    data = make_pdf([front_page()])
    server = pdf_server(data)
    with make_client(server.transport) as client:
        record, downloaded = ensure_paper(SPEC, tmp_path, client, existing=None, now=fixed_now)
        again, downloaded_again = ensure_paper(SPEC, tmp_path, client, existing=record)
    assert downloaded and not downloaded_again
    assert [str(r.url) for r in server.requests] == ["https://arxiv.org/pdf/1234.56789v2"]
    assert (tmp_path / "1234.56789v2.pdf").read_bytes() == data
    assert record.sha256 == sha256_hex(data) and record.size_bytes == len(data)
    assert (record.page_count, record.version_stamp) == (1, "arXiv:1234.56789v2")
    assert record.fetched_at == "2026-11-12T03:00:00Z"
    assert again == record  # cached file re-validated, fetched_at preserved


def test_checksum_change_against_manifest_is_an_error(tmp_path: Path) -> None:
    data = make_pdf([front_page()])
    with make_client(pdf_server(data).transport) as client:
        record, _ = ensure_paper(SPEC, tmp_path, client, existing=None)
        tampered = record.model_copy(update={"sha256": "0" * 64})
        with pytest.raises(ManifestError, match="immutable"):
            ensure_paper(SPEC, tmp_path, client, existing=tampered)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, text="missing"), "HTTP 404"),
        (httpx.Response(200, text="<html/>", headers={"content-type": "text/html"}),
         "unexpected content-type"),
    ],
)
def test_bad_downloads_are_rejected(tmp_path: Path, response: httpx.Response,
                                    message: str) -> None:
    with (
        make_client(Server(lambda request: response).transport) as client,
        pytest.raises(PaperFetchError, match=message),
    ):
        ensure_paper(SPEC, tmp_path, client, existing=None)
    assert not (tmp_path / SPEC.file_name).exists()


def test_invalid_download_is_not_written(tmp_path: Path) -> None:
    wrong = make_pdf([front_page(SPEC.model_copy(update={"version": 1}))])
    with (
        make_client(pdf_server(wrong).transport) as client,
        pytest.raises(PaperValidationError),
    ):
        ensure_paper(SPEC, tmp_path, client, existing=None)
    assert not (tmp_path / SPEC.file_name).exists()


def test_manifest_round_trip_is_deterministic(tmp_path: Path) -> None:
    with make_client(pdf_server(make_pdf([front_page()])).transport) as client:
        record, _ = ensure_paper(SPEC, tmp_path, client, existing=None, now=fixed_now)
    manifest = CorpusManifest(papers=(record,), extraction={"b": 1, "a": [2]})
    write_corpus_manifest(tmp_path, manifest)
    first = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    loaded = load_corpus_manifest(tmp_path)
    assert loaded == manifest
    assert render_manifest(loaded) == first


# --- Reading order and page cleanup -------------------------------------------------


def two_column_page(page: pymupdf.Page) -> None:
    box(page, (72, 60, 540, 90), "Full width title spanning both columns of the page", 12)
    box(page, (72, 110, 290, 250), words("L1w", 30))
    box(page, (72, 270, 290, 410), words("L2w", 30))
    box(page, (320, 110, 540, 250), words("R1w", 30))
    box(page, (320, 270, 540, 410), words("R2w", 30))
    box(page, (72, 430, 540, 470),
        "Closing full width paragraph after both columns end, long enough to cross the middle.")


def test_two_column_page_reads_left_column_then_right() -> None:
    parsed = parse_bytes(make_pdf([two_column_page]))
    order = [t.split()[0] for t in texts(parsed, 1)]
    assert order == ["Full", "L1w0", "L2w0", "R1w0", "R2w0", "Closing"]
    assert parsed.pages[0].layout == "two_column"
    assert "two_column_reordered" in parsed.pages[0].flags


def test_single_column_rows_read_left_to_right() -> None:
    def page(p: pymupdf.Page) -> None:
        box(p, (72, 100, 540, 160), words("para", 40))
        box(p, (300, 180, 340, 195), "middle")
        box(p, (100, 180, 140, 195), "left")
        box(p, (450, 180, 500, 195), "right")

    parsed = parse_bytes(make_pdf([page]))
    # MuPDF may merge the three same-row fragments into one block; order must still be L->R.
    assert " ".join(texts(parsed)).split()[-3:] == ["left", "middle", "right"]
    assert parsed.pages[0].layout == "single_column"


def test_margins_stamp_headers_and_page_numbers_are_dropped() -> None:
    def page(n: int) -> PageBuilder:
        def build(p: pymupdf.Page) -> None:
            stamp(p, SPEC.paper_id)
            box(p, (150, 25, 460, 40), "Running Header Title")
            box(p, (72, 100, 540, 200), words(f"body{n}x", 30))
            box(p, (300, 740, 320, 755), str(n))

        return build

    parsed = parse_bytes(make_pdf([page(1), page(2), page(3)]))
    assert all(t.startswith("body") for t in texts(parsed))
    for report in parsed.pages:
        assert report.dropped == {"page_number": 1, "rotated_text": 1, "running_header": 1}


def test_header_on_too_few_pages_is_kept() -> None:
    def page(p: pymupdf.Page) -> None:
        box(p, (150, 25, 460, 40), "Only Once Header")
        box(p, (72, 100, 540, 200), words("body", 30))

    parsed = parse_bytes(make_pdf([page]))
    assert texts(parsed)[0] == "Only Once Header"


def test_figure_glyph_text_is_dropped_but_equations_are_kept() -> None:
    def page(p: pymupdf.Page) -> None:
        box(p, (72, 100, 540, 120), "w i t h k e y p r o p e r t i e s t h a t")
        box(p, (72, 140, 540, 160), "77 69 74 68 20 6b 65 79 20 70 72 6f 70 65")
        box(p, (72, 180, 540, 200), "V s,1 = A s,1 V s,q = A s,q if s > 1 , q > 1")
        box(p, (72, 220, 540, 300), words("body", 30))

    parsed = parse_bytes(make_pdf([page]))
    kept = texts(parsed)
    assert kept[0].startswith("V s,1 =") and kept[1].startswith("body0")
    assert parsed.pages[0].dropped["figure_glyph_text"] == 2
    assert "figure_text_removed" in parsed.pages[0].flags


def test_word_soup_pages_are_cleaned_and_flagged() -> None:
    # MuPDF merges synthetic same-baseline words into lines, so test the filter directly.
    caption = pp._RawBlock(1, (72, 60, 540, 80), ["Figure 3: attention visualisation caption"])
    soup = [pp._RawBlock(1, (72, 100 + i, 90, 110 + i), [f"w{i}"]) for i in range(45)]
    page = pp._Page(1, 612, 792, [caption, *soup])
    pp._drop_figure_text(page)
    assert page.blocks == [caption]
    assert page.dropped["figure_word_soup"] == 45 and "figure_text_removed" in page.flags


def test_few_short_blocks_are_not_word_soup() -> None:
    blocks = [pp._RawBlock(1, (72, 100 + i, 90, 110 + i), [f"w{i}"]) for i in range(10)]
    page = pp._Page(1, 612, 792, list(blocks))
    pp._drop_figure_text(page)
    assert page.blocks == blocks


# --- Line assembly ------------------------------------------------------------------


def span(text: str, size: float = 10.0, baseline: float = 100.0) -> dict[str, Any]:
    return {"text": text, "size": size, "origin": (0.0, baseline)}


def test_super_and_subscripts_are_marked() -> None:
    line = {"spans": [span("costs 1.0 · 10"), span("20", 7, 96), span(" FLOPs with d"),
                      span("model", 7, 102), span("dimensions", 10)]}
    assert pp._line_text(line) == "costs 1.0 · 10^20 FLOPs with d_model dimensions"


def test_leading_footnote_marker_is_separated() -> None:
    line = {"spans": [span("5", 7, 96), span("We used values of 2.8 TFLOPS")]}
    assert pp._line_text(line) == "5 We used values of 2.8 TFLOPS"


def hyphenation(hyphenated: set[str], known: set[str]) -> pp._Hyphenation:
    return pp._Hyphenation(hyphenated, known)


@pytest.mark.parametrize(
    ("head", "tail", "hyphenated", "known", "expected", "stat"),
    [
        ("a transfor-", "mation here", set(), {"transformation"}, "a transformation here",
         "merged_known_word"),
        ("uses self-", "attention", {"self-attention"}, set(), "uses self-attention",
         "kept_hyphen_known_compound"),
        ("the Berkeley-", "Parser", set(), set(), "the Berkeley-Parser",
         "kept_hyphen_non_lowercase"),
        ("are sequence-", "aligned", set(), {"sequence", "aligned"}, "are sequence-aligned",
         "kept_hyphen_both_parts_words"),
        ("English-", "to-German", set(), {"english", "to"}, "English-to-German",
         "kept_hyphen_both_parts_words"),
        ("multipli-", "cative", set(), {"multi"}, "multiplicative", "merged_unverified"),
        ("high-", "est", set(), {"high", "est"}, "highest", "merged_unverified"),
        ("state-of-the-", "art models", set(), {"the", "art"}, "state-of-the-art models",
         "kept_hyphen_both_parts_words"),
    ],
)
def test_hyphenation_repair(head: str, tail: str, hyphenated: set[str], known: set[str],
                            expected: str, stat: str) -> None:
    repair = hyphenation(hyphenated, known)
    assert repair.join(head, tail) == expected
    assert repair.stats[stat] == 1


def test_vocabulary_ignores_line_end_fragments_and_compound_parts() -> None:
    block = pp._RawBlock(1, (0, 0, 1, 1), ["the multi-head attention trans-", "former model"])
    hyphenated, known = pp._vocabulary([block])
    assert hyphenated == {"multi-head"}
    assert "multi" not in known and "trans-" not in known and "former" in known


def test_ligatures_and_math_letters_are_normalised() -> None:
    assert pp._clean("ﬁne-tuning   with  𝑑 model") == "fine-tuning with d model"


# --- Sections and references --------------------------------------------------------


def paper_pdf(extra_page: PageBuilder | None = None) -> bytes:
    def intro(p: pymupdf.Page) -> None:
        box(p, (72, 60, 540, 80), "Multi-Head Attention")  # a figure label repeating a title
        box(p, (72, 100, 540, 120), "1 Introduction", 12)
        box(p, (72, 130, 540, 230), words("intro", 40))
        box(p, (72, 250, 540, 270), "1.1 Multi-Head Attention", 11)
        box(p, (72, 280, 540, 380), words("mha", 40))

    def refs(p: pymupdf.Page) -> None:
        box(p, (72, 60, 540, 80), "Acknowledgements", 12)
        box(p, (72, 90, 540, 130), words("thanks", 10))
        box(p, (72, 150, 540, 170), "References", 12)
        box(p, (72, 180, 540, 300), "[1] A. Author. A cited paper title. In Proc. 2017.")

    def appendix(p: pymupdf.Page) -> None:
        box(p, (72, 60, 540, 80), "A Extra Results", 12)
        box(p, (72, 90, 540, 190), words("appx", 30))
        box(p, (72, 210, 540, 230), "Attention Visualizations", 12)
        box(p, (72, 240, 540, 300), words("viz", 20))

    pages = [front_page(), intro, refs, appendix] + ([extra_page] if extra_page else [])
    toc = [[1, "Introduction", 2], [2, "Multi-Head Attention", 2], [1, "Extra Results", 4]]
    return make_pdf(pages, toc)


def test_sections_come_from_toc_with_numbers_and_special_headings() -> None:
    parsed = parse_bytes(paper_pdf(), extra_headings=("Attention Visualizations",))
    summary = [(s.kind, s.number, s.title, s.page_start, s.is_reference) for s in parsed.sections]
    assert summary == [
        ("front_matter", None, "Front matter", 1, False),
        ("abstract", None, "Abstract", 1, False),
        ("body", "1", "Introduction", 2, False),
        ("body", "1.1", "Multi-Head Attention", 2, False),
        ("acknowledgements", None, "Acknowledgements", 3, False),
        ("references", None, "References", 3, True),
        ("appendix", "A", "Extra Results", 4, False),
        ("appendix", None, "Attention Visualizations", 4, False),
    ]
    assert parsed.issues == ()
    assert parsed.sections[3].path == ("1 Introduction", "1.1 Multi-Head Attention")
    # The figure label before the real heading stays body text of the front matter section.
    label = next(b for b in parsed.blocks if b.text == "Multi-Head Attention")
    assert label.kind == "text" and parsed.section(label.section_id).kind == "abstract"


def test_reference_blocks_are_tagged() -> None:
    parsed = parse_bytes(paper_pdf(), extra_headings=("Attention Visualizations",))
    tagged = [b.text for b in parsed.blocks
              if parsed.section(b.section_id).is_reference and b.kind == "text"]
    assert tagged == ["[1] A. Author. A cited paper title. In Proc. 2017."]


def test_without_extra_heading_the_appendix_after_references_stays_appendix() -> None:
    parsed = parse_bytes(paper_pdf())
    viz = next(b for b in parsed.blocks if b.text.startswith("viz0"))
    assert parsed.section(viz.section_id).title == "Extra Results"


def test_missing_toc_heading_is_reported() -> None:
    data = make_pdf([front_page()], toc=[[1, "Nonexistent Section", 1]])
    parsed = parse_bytes(data)
    assert parsed.issues == ("TOC heading not found: 'Nonexistent Section' (page 1)",)


def test_parsing_is_deterministic() -> None:
    data = paper_pdf()
    first, second = parse_bytes(data), parse_bytes(data)
    assert first == second
    assert render_jsonl(first.blocks) == render_jsonl(second.blocks)


# --- Chunking -----------------------------------------------------------------------


def synthetic_parsed(section_words: list[tuple[str, bool, list[tuple[int, int]]]]) -> ParsedPaper:
    """Sections given as (title, is_reference, [(page, word count), ...])."""
    sections: list[Section] = []
    blocks: list[Block] = []
    for index, (title, is_ref, parts) in enumerate(section_words):
        section_id = f"{SPEC.paper_id}_s{index:03d}"
        kind = "references" if is_ref else "body"
        sections.append(Section(section_id=section_id, number=str(index), title=title, level=1,
                                kind=kind, is_reference=is_ref, page_start=parts[0][0],
                                path=(title,)))
        blocks.append(Block(order=len(blocks), page=parts[0][0], kind="heading", text=title,
                            bbox=(0, 0, 1, 1), section_id=section_id))
        offset = 0
        for page, count in parts:
            text = " ".join(f"{title[:1].lower()}{offset + i}" for i in range(count))
            offset += count
            blocks.append(Block(order=len(blocks), page=page, kind="text", text=text,
                                bbox=(0, 0, 1, 1), section_id=section_id))
    pages = sorted({b.page for b in blocks})
    return ParsedPaper(
        paper_id=SPEC.paper_id, parser_version="test", page_count=max(pages),
        sections=tuple(sections), blocks=tuple(blocks),
        pages=tuple(PageReport(page=p, layout="single_column", kept_blocks=0, words=0,
                               dropped={}, flags=()) for p in pages),
        issues=(), hyphenation={}, hyphen_merges_unverified=(), hyphen_kept_compounds=(),
    )


PRESET = ChunkPreset(name="test", size_words=40, overlap_words=10)


def test_chunk_windows_overlap_and_stop_at_section_end() -> None:
    parsed = synthetic_parsed([("Body", False, [(1, 60), (2, 40)]), ("Refs", True, [(3, 25)])])
    chunks = chunk_paper(parsed, SPEC, PRESET)
    body = [c for c in chunks if c.section_title == "Body"]
    assert [(c.word_start, c.word_end) for c in body] == [(0, 40), (30, 70), (60, 100)]
    assert [(c.page_start, c.page_end) for c in body] == [(1, 1), (1, 2), (2, 2)]
    assert body[1].text.split()[:2] == ["b30", "b31"]  # overlap of 10 words
    assert all(c.word_count == c.word_end - c.word_start <= 40 for c in chunks)
    refs = [c for c in chunks if c.section_title == "Refs"]
    assert len(refs) == 1 and refs[0].is_reference and refs[0].text.startswith("r0")
    assert all("Body" not in c.text and "Refs" not in c.text for c in chunks)  # no headings


def test_chunk_ids_are_stable_and_unique() -> None:
    parsed = synthetic_parsed([("Body", False, [(1, 60), (2, 40)]), ("Next", False, [(2, 30)])])
    chunks = chunk_paper(parsed, SPEC, PRESET)
    assert [c.chunk_id for c in chunks] == [
        "1234.56789v2_p1_c0", "1234.56789v2_p1_c1", "1234.56789v2_p2_c0", "1234.56789v2_p2_c1",
    ]
    assert chunks == chunk_paper(parsed, SPEC, PRESET)
    assert all(isinstance(c, Chunk) and c.version == 2 and c.arxiv_id == "1234.56789"
               for c in chunks)


def test_short_and_empty_sections() -> None:
    parsed = synthetic_parsed([("Tiny", False, [(1, 5)]), ("Empty", False, [(1, 0)])])
    chunks = chunk_paper(parsed, SPEC, PRESET)
    assert [(c.section_title, c.word_count) for c in chunks] == [("Tiny", 5)]


def test_preset_validation() -> None:
    with pytest.raises(ValueError):
        ChunkPreset(name="bad", size_words=40, overlap_words=20)
    with pytest.raises(ValueError):
        ChunkPreset(name="bad", size_words=10, overlap_words=0)


# --- Pipeline -----------------------------------------------------------------------


def test_pipeline_is_reproducible_and_offline_capable(tmp_path: Path) -> None:
    papers, processed = tmp_path / "papers", tmp_path / "processed"
    server = pdf_server(paper_pdf())
    first = run_ingest(papers, processed, specs=(SPEC,), presets=(PRESET,),
                       transport=server.transport)
    manifest_1 = (papers / "manifest.json").read_text(encoding="utf-8")
    chunks_1 = (processed / "chunks" / "test.jsonl").read_text(encoding="utf-8")
    second = run_ingest(papers, processed, specs=(SPEC,), presets=(PRESET,), offline=True)
    assert len(server.requests) == 1 and first.downloaded == (SPEC.paper_id,)
    assert second.downloaded == ()
    assert (papers / "manifest.json").read_text(encoding="utf-8") == manifest_1
    assert (processed / "chunks" / "test.jsonl").read_text(encoding="utf-8") == chunks_1
    summary = json.loads(manifest_1)["extraction"]["papers"][SPEC.paper_id]
    assert summary["reference_pages"] == [3]
    assert summary["chunks"]["test"]["reference_chunks"] == 1
    for name in ("blocks.jsonl", "sections.json", "pages.json"):
        assert (processed / SPEC.paper_id / name).is_file()


def test_pipeline_offline_without_pdf_fails(tmp_path: Path) -> None:
    with pytest.raises(PaperValidationError, match="--offline"):
        run_ingest(tmp_path / "papers", tmp_path / "processed", specs=(SPEC,), offline=True)


# --- Real corpus (skipped when the PDFs are not present) ----------------------------

REAL_PAPERS = REPO_ROOT / "data" / "papers"
HAVE_REAL = all((REAL_PAPERS / spec.file_name).is_file() for spec in PAPERS)


@pytest.mark.skipif(not HAVE_REAL, reason="real PDFs not downloaded (run scripts/ingest_papers.py)")
def test_real_corpus_matches_manifest_and_structure() -> None:
    manifest = load_corpus_manifest(REAL_PAPERS)
    for spec in PAPERS:
        record = manifest.record_for(spec.paper_id)
        assert record is not None
        assert sha256_hex((REAL_PAPERS / spec.file_name).read_bytes()) == record.sha256
    attention = pp.parse_pdf(REAL_PAPERS / PAPERS[0].file_name, PAPERS[0].paper_id,
                             extra_headings=PAPERS[0].extra_headings)
    titles = {(s.number, s.title) for s in attention.sections}
    assert ("3.2.2", "Multi-Head Attention") in titles
    assert attention.issues == ()
    refs = next(s for s in attention.sections if s.is_reference)
    assert refs.page_start == 10
    page8 = attention.page_text(8)
    assert "28.4" in page8 and "10^20" in page8  # table row and exponent survive
