"""V2-03 tests (V2-04 model): per-collection cue sets for the scope analyser."""

from __future__ import annotations

import pytest

from relay.ingestion.v1_corpus import V1_CUE_SET as V1_CUES
from relay.retrieval.cues import CueSet, DocumentCues
from relay.retrieval.scope import analyze_scope

RAG = CueSet(documents=(DocumentCues(
    paper_id="doc-rag", display_name="RAG", arxiv_id="2005.11401", version=4,
    strong=(r"retrieval-augmented generation", r"2005\.11401")),))


@pytest.mark.parametrize("question", [
    "How does Moshi handle full-duplex speech?",
    "Compare H-Net and the Transformer.",
    "In 1706.03762v5, what is d_model?",
    "Who won the football league?",
])
def test_default_cues_are_exactly_the_v1_behaviour(question: str) -> None:
    assert analyze_scope(question) == analyze_scope(question, cues=V1_CUES)


def test_collection_cues_replace_the_v1_constants() -> None:
    hit = analyze_scope("What is retrieval-augmented generation?", cues=RAG)
    assert hit.strong_papers == ("doc-rag",) and hit.suggested_paper_filter == ("doc-rag",)
    v1_name = analyze_scope("How does Moshi work?", cues=RAG)
    assert v1_name.strong_papers == () and v1_name.weak_papers == ()  # v1 cues not consulted


def test_pinned_versions_come_from_the_collection() -> None:
    other_version = analyze_scope("What does 2005.11401v2 claim?", cues=RAG)
    assert other_version.version_mismatch
    v1_version = analyze_scope("In 1706.03762v5, what is d_model?", cues=RAG)
    assert not v1_version.version_mismatch  # not pinned in this collection
