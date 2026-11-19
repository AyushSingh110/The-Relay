"""V2-03 tests: per-collection scope cues for the (v1) scope analyser. Pure functions."""

from __future__ import annotations

import pytest

from relay.retrieval.scope import V1_CUES, ScopeCues, analyze_scope

RAG = ScopeCues(strong={"doc-rag": (r"retrieval-augmented generation", r"2005\.11401")},
                weak={}, pinned_versions={"2005.11401": 4})


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
