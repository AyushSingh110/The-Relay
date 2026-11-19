"""V2-04 tests: the cue-set model and cue derivation (no paper data in the model)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from relay.retrieval.cues import CueSet, DocumentCues, derive_document_cues

RAG = DocumentCues(paper_id="doc-rag", title="Retrieval-Augmented Generation for NLP",
                   display_name="RAG", short_names=("RAG",), arxiv_id="2005.11401", version=4,
                   strong=(r"retrieval-augmented generation", r"2005\.11401"),
                   weak=(r"retrieval",))
NOTES = DocumentCues(paper_id="doc-notes", title="Field Notes", display_name='"Field Notes"',
                     strong=(r"field notes",))


def test_a_cue_set_exposes_titles_short_names_and_versioned_arxiv_ids() -> None:
    cues = CueSet(documents=(RAG, NOTES))
    assert cues.titles == ["Retrieval-Augmented Generation for NLP", "Field Notes"]
    assert cues.short_names == ["RAG"]
    assert cues.arxiv_ids == ["2005.11401v4"]
    assert cues.strong_cues() == {"doc-rag": RAG.strong, "doc-notes": NOTES.strong}
    assert cues.weak_cues() == {"doc-rag": (r"retrieval",)}
    assert cues.pinned_versions() == {"2005.11401": 4}
    assert cues.pinned_papers() == {"2005.11401": "doc-rag"}
    assert cues.name_cues() == {"doc-rag": (r"retrieval-augmented generation",),
                                "doc-notes": (r"field notes",)}  # no digits: names only
    assert cues.paper_ids == ("doc-rag", "doc-notes")


def test_cue_sets_reject_duplicate_documents_and_round_trip_through_json() -> None:
    with pytest.raises(ValidationError):
        CueSet(documents=(RAG, RAG))
    assert DocumentCues.model_validate_json(RAG.model_dump_json()) == RAG


def test_derived_cues_name_the_document_by_title_short_name_and_arxiv_id() -> None:
    derived = derive_document_cues("d1", title="Moshi: a speech-text foundation model",
                                   arxiv_id="2410.00037", version=1)
    assert derived.short_names == ("Moshi",)
    assert derived.display_name == "Moshi"
    assert derived.strong == (r"moshi:\ a\ speech\-text\ foundation\ model", "moshi",
                              r"2410\.00037")
    plain = derive_document_cues("d2", title="Basalt Formation Survey")
    assert (plain.display_name, plain.short_names, plain.arxiv_id) == (
        '"Basalt Formation Survey"', (), None)
    generic = derive_document_cues("abcdef123456", title="paper")
    assert generic.strong == () and generic.display_name == "document abcdef12"
