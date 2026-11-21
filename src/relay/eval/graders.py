"""The three graders of plan V2-12 (docs/ARCHITECTURE_V2.md §10), reported side by side.

- ``RubricGrader``: v1's deterministic first pass (``relay.eval.grader.propose_grade``,
  unchanged): required facts present, a gold page cited, no forbidden claim; for an
  unanswerable question, a refusal. Verdicts are v1's ``correct``, ``wrong``,
  ``refused_correctly`` and ``refused_wrongly``.
- ``EntailmentGrader``: the L4 cross-encoder (V2-07/08 two-sentence windows). Each
  sentence's score is the best entailment probability over the passages it cites; the
  answer's score is the mean over its sentences (0.0 to 1.0). A refusal is not scored. No
  verdict: no threshold is chosen until the owner's grades calibrate one.
- ``HumanGrader``: the owner's verdict, ``CORRECT``, ``PARTIAL``, ``INCORRECT`` or
  ``UNANSWERABLE_CORRECT``, with notes. Only these grades are "reviewed".
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from relay.eval.grader import AnswerCitation, AnswerRecord, propose_grade
from relay.eval.questions import EvalQuestion
from relay.orchestrator.verifier import TAG_PATTERN, EntailmentScore, split_sentences

RUBRIC_SCORES: Final = {"correct": 1.0, "refused_correctly": 1.0, "wrong": 0.0,
                        "refused_wrongly": 0.0}
HUMAN_SCORES: Final = {"CORRECT": 1.0, "UNANSWERABLE_CORRECT": 1.0, "PARTIAL": 0.5,
                       "INCORRECT": 0.0}
_NUMBER: Final = re.compile(r"\d+")


class _AnyGold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    paper_id: str
    page: int
    section: str | None = None
    quote: str = ""


class AnyCollectionQuestion(BaseModel):
    """The rubric's view of a question from any collection: v1's ``EvalQuestion`` fields,
    without v1's three-paper list (a new collection's gold citations name its own papers)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    question: str
    answerable: bool
    required_facts: tuple[str, ...] = ()
    gold_citations: tuple[_AnyGold, ...] = ()
    forbidden_claims: tuple[str, ...] = ()


def rubric_question(spec: Mapping[str, Any]) -> Any:
    """``EvalQuestion`` for a v1 question (with all of v1's checks), else the same fields
    for another collection."""
    text = json.dumps(dict(spec))
    try:
        return EvalQuestion.model_validate_json(text)
    except ValidationError:
        return AnyCollectionQuestion.model_validate_json(text)


@dataclass(frozen=True)
class GradeResult:
    verdict: str | None
    score: float | None
    notes: str | None = None


class RubricGrader:
    name: Final = "rubric"

    def grade(self, spec: Mapping[str, Any], *, answer_text: str, refused: bool,
              citations: Sequence[Mapping[str, Any]], request_id: str = "answer",
              ) -> GradeResult:
        question = rubric_question(spec)
        record = AnswerRecord(
            request_id=request_id, question_id=question.id, answer_text=answer_text,
            refused=refused,
            citations=tuple(AnswerCitation(paper_id=str(c["paper_id"]),
                                           page_start=int(c["page_start"]),
                                           page_end=int(c["page_end"]))
                            for c in citations if c.get("paper_id")))
        proposed = propose_grade(question, record)
        notes = json.dumps({"reasons": list(proposed.reasons),
                            "facts_missing": list(proposed.facts_missing),
                            "forbidden_found": list(proposed.forbidden_found),
                            "gold_page_hits": list(proposed.gold_page_hits)},
                           ensure_ascii=False)
        return GradeResult(proposed.proposed, RUBRIC_SCORES[proposed.proposed], notes)


def cited_passages(sentence: str, passages: Sequence[Mapping[str, Any]]) -> list[str]:
    """The texts of the passages a sentence cites by tag (``[P2]`` is the second)."""
    texts: list[str] = []
    for tag in TAG_PATTERN.finditer(sentence):
        for number in _NUMBER.findall(tag.group()):
            index = int(number) - 1
            if 0 <= index < len(passages) and passages[index].get("text"):
                texts.append(str(passages[index]["text"]))
    return texts


class EntailmentGrader:
    name: Final = "entailment"

    def __init__(self, scorer: EntailmentScore) -> None:
        self._scorer = scorer

    def grade(self, *, answer_text: str, refused: bool,
              passages: Sequence[Mapping[str, Any]]) -> GradeResult:
        if refused or not answer_text.strip():
            return GradeResult(None, None, "not scored: a refusal")
        scores: list[float] = []
        for sentence in split_sentences(answer_text):
            cited = cited_passages(sentence, passages)
            plain = TAG_PATTERN.sub(" ", sentence).strip()
            scores.append(max((self._scorer.score(plain, text) for text in cited), default=0.0))
        if not scores:
            return GradeResult(None, None, "not scored: no sentence")
        return GradeResult(None, round(sum(scores) / len(scores), 4),
                           json.dumps({"sentences": [round(s, 4) for s in scores]}))


class HumanGrader:
    name: Final = "human"

    @staticmethod
    def grade(verdict: str, notes: str | None = None) -> GradeResult:
        if verdict not in HUMAN_SCORES:
            raise ValueError(f"verdict must be one of {sorted(HUMAN_SCORES)}")
        return GradeResult(verdict, HUMAN_SCORES[verdict], notes)


def v1_final_to_human(final: str | None) -> str | None:
    """A v1 owner-reviewed grade (``final``), as a human verdict."""
    return {"correct": "CORRECT", "wrong": "INCORRECT", "refused_correctly":
            "UNANSWERABLE_CORRECT", "refused_wrongly": "INCORRECT"}.get(final or "")


__all__ = ["HUMAN_SCORES", "RUBRIC_SCORES", "AnyCollectionQuestion", "EntailmentGrader",
           "GradeResult", "HumanGrader", "RubricGrader", "cited_passages", "rubric_question",
           "v1_final_to_human"]
