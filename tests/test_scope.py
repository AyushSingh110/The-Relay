"""Scope analyzer tests (deterministic lexicon/regex decisions)."""

from __future__ import annotations

import pytest

from relay.retrieval.scope import analyze_scope

ATTENTION, MOSHI, HNET = "1706.03762v7", "2410.00037v1", "2507.07955v2"


@pytest.mark.parametrize(
    ("question", "strong"),
    [
        ("What does Attention Is All You Need report for EN-DE?", (ATTENTION,)),
        ("How many codebooks does Mimi use?", (MOSHI,)),
        ("What is Helium trained on?", (MOSHI,)),
        ("How does H-Net decide boundaries?", (HNET,)),
        ("Explain dynamic chunking in hnet", (HNET,)),
        ("In arXiv 2410.00037, what is the latency?", (MOSHI,)),
    ],
)
def test_strong_cues_suggest_a_single_paper(question: str, strong: tuple[str, ...]) -> None:
    decision = analyze_scope(question)
    assert decision.strong_papers == strong
    assert decision.suggested_paper_filter == strong
    assert not decision.comparison


def test_shared_vocabulary_is_weak_and_never_filters() -> None:
    decision = analyze_scope("How many layers does the Transformer encoder have?")
    assert decision.strong_papers == ()
    assert decision.weak_papers == (ATTENTION,)
    assert decision.suggested_paper_filter is None
    chunk = analyze_scope("What is a chunk size?")
    assert chunk.weak_papers == (HNET,) and chunk.suggested_paper_filter is None


def test_comparison_across_papers() -> None:
    decision = analyze_scope("Compare how Moshi and H-Net handle sequence length")
    assert decision.comparison
    assert decision.strong_papers == (MOSHI, HNET)
    assert decision.suggested_paper_filter == (MOSHI, HNET)
    assert analyze_scope("What is the difference between d_k and d_v?").comparison
    assert not analyze_scope("Is it more than 2 layers?").comparison


@pytest.mark.parametrize(
    ("question", "asks"),
    [
        ("Which work is cited for layer normalization?", True),
        ("What does reference [12] describe?", True),
        ("List the references about byte-level models", True),
        ("What is layer normalization?", False),
    ],
)
def test_reference_intent(question: str, asks: bool) -> None:
    assert analyze_scope(question).asks_references is asks


def test_identifiers_are_extracted_verbatim() -> None:
    decision = analyze_scope("Is d_model 512 at 12.5 Hz with RVQ in RQ-Transformer, 28.4 BLEU?")
    for identifier in ("d_model", "12.5 Hz", "RVQ", "RQ-Transformer", "28.4", "BLEU"):
        assert identifier in decision.identifiers


def test_version_mismatch_is_flagged() -> None:
    other = analyze_scope("What changed in 1706.03762v5 of the Transformer paper?")
    assert other.version_mentions == ("1706.03762v5",)
    assert other.version_mismatch
    pinned = analyze_scope("In 1706.03762v7, what is d_ff?")
    assert not pinned.version_mismatch


def test_corpus_coverage_and_out_of_corpus_signal() -> None:
    vocabulary = frozenset({"attention", "encoder", "layers", "codebooks"})
    off = analyze_scope("Recipe for chocolate sourdough bread", vocabulary)
    assert off.corpus_term_coverage == 0.0 and off.out_of_corpus_signal
    on = analyze_scope("How many encoder layers use attention?", vocabulary)
    assert on.corpus_term_coverage == 1.0 and not on.out_of_corpus_signal
    assert analyze_scope("Recipe for bread").corpus_term_coverage is None  # no vocabulary given


def test_scope_is_deterministic() -> None:
    question = "Compare Mimi codebooks with the Transformer d_model in 2410.00037v1"
    assert analyze_scope(question) == analyze_scope(question)
