"""The v1 corpus as a cue set: the only place the three v1 papers' cues are defined.

These are the curated v1 cues, moved unchanged out of the scope analyser (V2-04). They
serve:

- the v1 ``/ask`` path (no ``collection_id``), as the default cue set of the scope
  analyser, evidence gate, verifier, messages and prompt;
- the ``v1-papers`` collection, whose documents store these cues at registration;
- any v1 paper ingested into another collection (by its exact arXiv id and version).

``tests/test_v1_corpus.py`` pins them to the v1 constants, so v1 behaviour is unchanged.
"""

from __future__ import annotations

from typing import Final

from relay.ingestion.arxiv_fetcher import PAPERS
from relay.retrieval.cues import CueSet, DocumentCues

_ATTENTION, _MOSHI, _HNET = PAPERS

# (display name, short names, strong patterns, weak patterns), in v1's pattern order.
_CURATED: Final = {
    _ATTENTION.paper_id: (
        f'"{_ATTENTION.title}"', (),
        (r"attention is all you need", r"vaswani", r"1706\.03762"),
        (r"transformers?", r"self-attention", r"multi-head attention",
         r"positional encodings?", r"machine translation", r"wmt", r"bleu"),
    ),
    _MOSHI.paper_id: (
        "Moshi", ("Moshi",),
        (r"moshi", r"mimi", r"helium", r"kyutai", r"inner monologue", r"2410\.00037"),
        (r"speech", r"audio", r"dialogues?", r"full-duplex", r"rvq", r"codec", r"voice"),
    ),
    _HNET.paper_id: (
        "H-Net", ("H-Net",),
        (r"h-?net", r"dynamic chunking", r"2507\.07955", r"hierarchical sequence modell?ing"),
        (r"chunk(?:s|ing)?", r"byte-level", r"tokeni[sz]er-free", r"tokeni[sz]ation",
         r"routing module", r"dechunk\w*"),
    ),
}

V1_SUBJECT: Final = "three research papers"
V1_DOCUMENTS: Final = tuple(
    DocumentCues(paper_id=spec.paper_id, display_name=display, title=spec.title,
                 short_names=short, arxiv_id=spec.arxiv_id, version=spec.version,
                 strong=strong, weak=weak)
    for spec in PAPERS
    for display, short, strong, weak in (_CURATED[spec.paper_id],)
)
V1_CUE_SET: Final = CueSet(documents=V1_DOCUMENTS, noun="papers", noun_singular="paper",
                           subject=V1_SUBJECT)
_BY_ID: Final = {d.paper_id: d for d in V1_DOCUMENTS}


def v1_document_cues(paper_id: str) -> DocumentCues | None:
    """The curated cues of a v1 paper (exact arXiv id and version), else None."""
    return _BY_ID.get(paper_id)


def is_v1_corpus(cues: CueSet) -> bool:
    """True when ``cues`` holds exactly the three v1 papers' curated cues."""
    return cues.documents == V1_DOCUMENTS


__all__ = ["V1_CUE_SET", "V1_DOCUMENTS", "V1_SUBJECT", "is_v1_corpus", "v1_document_cues"]
