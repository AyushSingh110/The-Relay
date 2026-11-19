"""V2-04 tests: the scope analyser, evidence gate, verifier and messages take a collection's
cue set instead of the three v1 papers' constants (tests 1-4 and 7 of the phase prompt;
tests 5 and 6 are in ``test_pipeline_collections.py`` and ``test_v1_regression.py``).

Pure functions and scripted providers; no network, no API key.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.orchestrator.evidence_gate import EvidenceGate
from relay.orchestrator.messages import refusal_messages
from relay.orchestrator.prompt import (
    COLLECTION_PROMPT_VERSION,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_messages,
    passage_metadata,
    prompt_version,
    system_prompt,
)
from relay.orchestrator.verifier import Verifier, verify
from relay.retrieval.cues import CueSet, DocumentCues
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.scope import ScopeAnalyser, analyze_scope

ROCKS = DocumentCues(paper_id="doc-rocks", display_name="Basalt Atlas",
                     title="Basalt Atlas: volcanic rocks of the world",
                     short_names=("Basalt Atlas",), arxiv_id="2501.00001", version=2,
                     strong=(r"basalt atlas: volcanic rocks of the world", r"basalt atlas",
                             r"2501\.00001"),
                     weak=(r"lava", r"volcan\w*"))
GEOLOGY = CueSet(documents=(ROCKS,))
ROCK_VOCABULARY = frozenset({"basalt", "lava", "volcanic", "rock", "rocks", "flow", "cool",
                             "column", "columns", "atlas"})
V1_NAMES = re.compile(r"attention is all you need|vaswani|moshi|mimi|kyutai|helium|h-?net|"
                      r"flashattention|1706\.03762|2410\.00037|2507\.07955|dynamic chunking",
                      re.IGNORECASE)


# 1 --------------------------------------------------------------------------------------
def test_a_question_about_a_collection_document_is_in_scope() -> None:
    analyser = ScopeAnalyser(GEOLOGY)
    decision = analyser.analyze("What does the Basalt Atlas say about lava flows?",
                                ROCK_VOCABULARY)
    assert decision.strong_papers == ("doc-rocks",)
    assert decision.suggested_paper_filter == ("doc-rocks",)
    assert not decision.out_of_corpus_signal
    topical = analyser.analyze("How do volcanic columns cool?", ROCK_VOCABULARY)
    assert topical.weak_papers == ("doc-rocks",) and not topical.out_of_corpus_signal
    assert analyser.analyze("In 2501.00001v1, what is basalt?").version_mismatch


# 2 --------------------------------------------------------------------------------------
def test_an_unrelated_question_is_out_of_scope_for_the_collection() -> None:
    analyser = ScopeAnalyser(GEOLOGY)
    off = analyser.analyze("How does Moshi handle full-duplex speech dialogue?",
                           ROCK_VOCABULARY)
    assert off.strong_papers == () and off.weak_papers == ()  # v1 cues are not consulted
    assert off.out_of_corpus_signal
    assert not analyser.analyze("In 1706.03762v5, what is d_model?").version_mismatch


def test_the_default_cue_set_is_the_v1_corpus() -> None:
    for question in ("How does Moshi handle full-duplex speech?",
                     "Compare H-Net and the Transformer.", "In 1706.03762v5, what is d_model?"):
        assert analyze_scope(question) == ScopeAnalyser(V1_CUE_SET).analyze(question)


@pytest.mark.parametrize("module", ["src/relay/retrieval/scope.py",
                                    "src/relay/orchestrator/evidence_gate.py",
                                    "src/relay/orchestrator/verifier.py"])
def test_no_paper_names_are_hard_coded(module: str) -> None:
    source = Path(module).read_text(encoding="utf-8")
    assert V1_NAMES.findall(source) == []


# 3 --------------------------------------------------------------------------------------
def passage(paper_id: str, n: int = 0) -> RetrievedPassage:
    return RetrievedPassage(
        rank=n + 1, chunk_id=f"{paper_id}_p1_c{n}", paper_id=paper_id, arxiv_id="", version=0,
        short_name="doc", section_number=None, section_title="Intro", section_path=("Intro",),
        section_kind="body", page_start=1, page_end=1, is_reference=False,
        text="Basalt is a volcanic rock formed when lava cools.", bm25_rank=1, dense_rank=1,
        rrf_score=0.5, rerank_score=None, final_score=0.5, reference_downweighted=False)


def test_the_gate_requery_uses_the_collection_short_names() -> None:
    notes = DocumentCues(paper_id="doc-notes", display_name="Field Notes",
                         title="Field Notes: columnar jointing", short_names=("Field Notes",),
                         strong=(r"field notes",))
    cues = CueSet(documents=(ROCKS, notes))
    gate = EvidenceGate(cues)
    question = ("What is multi-head attention in the Transformer? "
                "What does the Basalt Atlas say about lava?")
    assert gate.requery_text(question, ("doc-rocks",)) == (
        "What does the Basalt Atlas say about lava?")
    scope = ScopeAnalyser(cues).analyze(
        "What does the Basalt Atlas say about lava? How do the Field Notes compare?")
    decision = gate.decide(scope, [passage("doc-rocks")], loop_count=0)
    assert (decision.action, decision.paper_filter) == ("LOOP_EXPAND", ("doc-notes",))
    assert decision.requery == "How do the Field Notes compare?"
    # the v1 names mean nothing to this gate: no v1 sentence is chosen for the re-query
    assert gate.requery_text("How does Moshi work? Tell me about H-Net.", ("doc-rocks",)) == (
        "How does Moshi work? Tell me about H-Net.")
    mismatch = gate.decide(ScopeAnalyser(cues).analyze("In 2501.00001v1, what is basalt?"),
                           [passage("doc-rocks")], loop_count=0)
    assert mismatch.reason == "version_not_in_corpus" and "doc-rocks" in mismatch.detail


# 7 (verifier level) -----------------------------------------------------------------------
ATTENTION_ONLY = CueSet(documents=(V1_CUE_SET.documents[0],))
MOSHI = V1_CUE_SET.documents[1]


def attention_passage() -> RetrievedPassage:
    return passage("1706.03762v7").model_copy(update={
        "arxiv_id": "1706.03762", "version": 7, "short_name": "attention",
        "text": "The Transformer uses multi-head attention over the encoder outputs."})


def test_the_verifier_blocks_names_of_documents_outside_the_collection() -> None:
    answer = "Moshi relies on multi-head attention over the encoder outputs [P1]."
    isolated = Verifier(ATTENTION_ONLY, foreign=(MOSHI,)).verify(answer, [attention_passage()])
    assert isolated.verdict == "BLOCKED"
    assert [f.code for f in isolated.failures] == ["paper_mismatch"]
    assert "2410.00037v1" in isolated.failures[0].detail
    own = Verifier(ATTENTION_ONLY, foreign=(MOSHI,)).verify(
        "The Transformer uses multi-head attention over the encoder outputs [P1].",
        [attention_passage()])
    assert own.verdict == "VERIFIED"


def test_the_v1_verifier_is_the_default() -> None:
    answer = "Moshi relies on multi-head attention over the encoder outputs [P1]."
    assert verify(answer, [attention_passage()]) == Verifier(V1_CUE_SET).verify(
        answer, [attention_passage()])
    assert verify(answer, [attention_passage()]).verdict == "BLOCKED"  # v1 knows Moshi


# 4 --------------------------------------------------------------------------------------
V1_OUT_OF_CORPUS = (
    "I can only answer from three papers (\"Attention Is All You Need\", arXiv:1706.03762v7; "
    "Moshi, arXiv:2410.00037v1; H-Net, arXiv:2507.07955v2), and they do not cover this "
    "question.")
V1_VERSION = (
    "I can only answer from the exact paper versions in the library (arXiv:1706.03762v7, "
    "arXiv:2410.00037v1, arXiv:2507.07955v2), not from other versions of these papers.")
V1_LLM_REFUSAL = ("I can't answer this from the three papers in the library: the passages I "
                  "found do not contain the information needed.")
V1_SAFE_REFUSAL = ("I could not produce an answer that I can verify against the papers, so I "
                   "am not answering rather than risk an unsupported claim.")


def test_refusal_messages_name_the_collections_own_documents() -> None:
    notes = DocumentCues(paper_id="doc-notes", display_name='"Field Notes"', title="Field Notes",
                         strong=(r"field notes",))
    messages = refusal_messages(CueSet(documents=(ROCKS, notes)))
    assert messages.out_of_corpus == (
        'I can only answer from two documents (Basalt Atlas, arXiv:2501.00001v2; '
        '"Field Notes"), and they do not cover this question.')
    assert messages.version == (
        "I can only answer from the exact document version in the library "
        "(arXiv:2501.00001v2), not from other versions of this document.")
    assert messages.llm_refusal.startswith("I can't answer this from the two documents in")
    assert "against the documents" in messages.safe_refusal
    single = refusal_messages(GEOLOGY)
    assert single.out_of_corpus == ("I can only answer from one document (Basalt Atlas, "
                                    "arXiv:2501.00001v2), and it does not cover this question.")
    for text in (*vars(messages).values(), *vars(single).values()):
        assert V1_NAMES.findall(text) == [] and "three papers" not in text


def test_the_v1_corpus_renders_the_v1_messages_and_prompt_byte_for_byte() -> None:
    v1 = refusal_messages(V1_CUE_SET)
    assert (v1.out_of_corpus, v1.version, v1.llm_refusal, v1.safe_refusal) == (
        V1_OUT_OF_CORPUS, V1_VERSION, V1_LLM_REFUSAL, V1_SAFE_REFUSAL)
    assert system_prompt(V1_CUE_SET) == SYSTEM_PROMPT
    assert SYSTEM_PROMPT.startswith("You answer questions about three research papers using")
    assert prompt_version(V1_CUE_SET) == PROMPT_VERSION == "p05-1"
    collection = system_prompt(GEOLOGY)
    assert collection.startswith("You answer questions about one document (Basalt Atlas) using")
    assert prompt_version(GEOLOGY) == COLLECTION_PROMPT_VERSION != PROMPT_VERSION


# review fixes (prompt and messages) --------------------------------------------------------
def test_braces_in_document_names_do_not_break_the_prompt() -> None:
    braces = DocumentCues(paper_id="doc-b", display_name='"notes{v2} {0}"', title="notes{v2} {0}")
    messages = build_messages("What is in the notes?", [passage("doc-b")],
                              cues=CueSet(documents=(braces,)))
    assert 'one document ("notes{v2} {0}")' in messages[0].content


def test_the_arxiv_label_comes_from_the_arxiv_fields() -> None:
    odd = passage("doc-x").model_copy(update={"arxiv_id": "2501.00001", "version": 2})
    assert "(arXiv:2501.00001v2)" in passage_metadata(odd, {})
    assert "(document doc-y)" in passage_metadata(passage("doc-y"), {})


def test_the_version_message_without_arxiv_documents_has_no_empty_list() -> None:
    notes = DocumentCues(paper_id="doc-notes", display_name='"Field Notes"', title="Field Notes")
    text = refusal_messages(CueSet(documents=(notes,))).version
    assert "()" not in text and "arXiv" not in text
