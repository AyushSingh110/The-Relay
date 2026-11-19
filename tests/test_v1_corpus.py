"""V2-04 tests: the v1 corpus cue set equals the v1 constants it replaces, exactly.

The literals below are the v1 ``STRONG_CUES`` / ``WEAK_CUES`` from ``relay.retrieval.scope``
as of tag v1-submission. If these differ, v1 scope decisions would change.
"""

from __future__ import annotations

from relay.ingestion.arxiv_fetcher import PAPERS
from relay.ingestion.v1_corpus import V1_CUE_SET, v1_document_cues

V1_STRONG = {
    "1706.03762v7": (r"attention is all you need", r"vaswani", r"1706\.03762"),
    "2410.00037v1": (r"moshi", r"mimi", r"helium", r"kyutai", r"inner monologue",
                     r"2410\.00037"),
    "2507.07955v2": (r"h-?net", r"dynamic chunking", r"2507\.07955",
                     r"hierarchical sequence modell?ing"),
}
V1_WEAK = {
    "1706.03762v7": (r"transformers?", r"self-attention", r"multi-head attention",
                     r"positional encodings?", r"machine translation", r"wmt", r"bleu"),
    "2410.00037v1": (r"speech", r"audio", r"dialogues?", r"full-duplex", r"rvq", r"codec",
                     r"voice"),
    "2507.07955v2": (r"chunk(?:s|ing)?", r"byte-level", r"tokeni[sz]er-free",
                     r"tokeni[sz]ation", r"routing module", r"dechunk\w*"),
}


def test_the_v1_cue_set_reproduces_the_v1_constants() -> None:
    assert V1_CUE_SET.paper_ids == ("1706.03762v7", "2410.00037v1", "2507.07955v2")
    # sorted by paper id: what a collection's cue set is compared against (cue_set_for)
    assert list(V1_CUE_SET.paper_ids) == sorted(V1_CUE_SET.paper_ids)
    assert V1_CUE_SET.strong_cues() == V1_STRONG
    assert V1_CUE_SET.weak_cues() == V1_WEAK
    assert V1_CUE_SET.pinned_versions() == {"1706.03762": 7, "2410.00037": 1, "2507.07955": 2}
    assert V1_CUE_SET.titles_by_paper() == {p.paper_id: p.title for p in PAPERS}
    assert V1_CUE_SET.arxiv_ids == ["1706.03762v7", "2410.00037v1", "2507.07955v2"]
    assert (V1_CUE_SET.noun, V1_CUE_SET.subject) == ("papers", "three research papers")
    assert [d.display_name for d in V1_CUE_SET.documents] == [
        '"Attention Is All You Need"', "Moshi", "H-Net"]


def test_v1_document_cues_are_found_by_paper_id_only() -> None:
    assert v1_document_cues("2410.00037v1") == V1_CUE_SET.documents[1]
    assert v1_document_cues("2410.00037v2") is None
    assert v1_document_cues("some-upload") is None
