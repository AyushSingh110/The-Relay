"""Deterministic first-pass grading plus owner review (ARCHITECTURE.md section 11).

The rubric *proposes* one of the four labels with reasons; it never finalises a grade.
Every grade starts ``pending`` and only becomes final when the owner confirms or
overrides it. No LLM-as-judge.

Rules:
- answerable + refusal (including a verifier safe refusal) -> ``refused_wrongly``
- unanswerable + refusal -> ``refused_correctly``; unanswerable + answer -> ``wrong``
- answerable + answer -> ``correct`` only if every required fact is present, at least one
  answer citation lands on a gold page, and no forbidden claim appears; otherwise
  ``wrong``. A correct fact with a wrong or missing citation is ``wrong``.

Fact matching is a normalised substring test (case-, width- and whitespace-insensitive).
A required fact may list alternatives separated by ``|`` (e.g. ``"512|d_model = 512"``).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.eval.questions import FACT_ALTERNATIVE_SEPARATOR, EvalQuestion, QuestionSet
from relay.schemas import AnswerGrade

_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")
ReviewStatus = Literal["pending", "confirmed", "overridden"]


class AnswerCitation(BaseModel):
    model_config = _STRICT

    paper_id: str
    page_start: Annotated[int, Field(ge=1)]
    page_end: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _order(self) -> AnswerCitation:
        if self.page_end < self.page_start:
            raise ValueError("page_end < page_start")
        return self


class AnswerRecord(BaseModel):
    """One delivered (or refused) answer, as the experiment run records it (P05/P07)."""

    model_config = _STRICT

    request_id: Annotated[str, Field(min_length=1)]
    question_id: Annotated[str, Field(min_length=1)]
    answer_text: str
    refused: bool
    citations: tuple[AnswerCitation, ...] = ()


class GradeRecord(BaseModel):
    model_config = _STRICT

    request_id: str
    question_id: str
    proposed: AnswerGrade
    reasons: tuple[str, ...]
    facts_found: tuple[str, ...]
    facts_missing: tuple[str, ...]
    forbidden_found: tuple[str, ...]
    gold_page_hits: tuple[str, ...]
    review_status: ReviewStatus = "pending"
    final: AnswerGrade | None = None
    owner_note: str | None = None

    @model_validator(mode="after")
    def _review_consistency(self) -> GradeRecord:
        if self.review_status == "pending" and self.final is not None:
            raise ValueError("pending grades have no final label")
        if self.review_status != "pending" and self.final is None:
            raise ValueError("reviewed grades need a final label")
        if self.review_status == "confirmed" and self.final != self.proposed:
            raise ValueError("a confirmed grade must equal the proposal; use an override")
        if self.review_status == "overridden" and not (self.owner_note or "").strip():
            raise ValueError("an override needs an owner_note explaining it")
        return self


class GradesFile(BaseModel):
    """``grades.json`` stored with the experiment run evidence."""

    model_config = _STRICT

    schema_version: Literal[1] = 1
    run_id: str
    grades: tuple[GradeRecord, ...]

    @model_validator(mode="after")
    def _unique(self) -> GradesFile:
        counts = Counter(g.request_id for g in self.grades)
        dupes = sorted(r for r, n in counts.items() if n > 1)
        if dupes:
            raise ValueError(f"duplicate request_ids in grades: {dupes}")
        return self

    def final_grades(self) -> dict[str, AnswerGrade]:
        """Owner-reviewed grades only; pending proposals never reach results rows."""
        return {g.request_id: g.final for g in self.grades if g.final is not None}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"[*`_\"'“”‘’]", " ", text)  # markdown emphasis and quotes
    return " ".join(text.split())


def _contains(answer: str, fact: str) -> bool:
    return any(normalise(alt) in answer for alt in fact.split(FACT_ALTERNATIVE_SEPARATOR)
               if alt.strip())


def propose_grade(question: EvalQuestion, answer: AnswerRecord) -> GradeRecord:
    if answer.question_id != question.id:
        raise ValueError(f"answer is for {answer.question_id}, not {question.id}")
    text = normalise(answer.answer_text)
    found = tuple(f for f in question.required_facts if _contains(text, f))
    missing = tuple(f for f in question.required_facts if f not in found)
    forbidden = tuple(c for c in question.forbidden_claims if _contains(text, c))
    hits = tuple(
        f"{c.paper_id} p.{g.page}"
        for g in question.gold_citations
        for c in answer.citations
        if c.paper_id == g.paper_id and c.page_start <= g.page <= c.page_end
    )
    hits = tuple(dict.fromkeys(hits))
    reasons: list[str] = []
    grade: AnswerGrade
    if answer.refused:
        grade = "refused_wrongly" if question.answerable else "refused_correctly"
        reasons.append("answer is a refusal; question is "
                       + ("answerable" if question.answerable else "unanswerable"))
    elif not question.answerable:
        grade = "wrong"
        reasons.append("question is unanswerable but an answer was given")
    else:
        if missing:
            reasons.append(f"missing required facts: {list(missing)}")
        if not hits:
            reasons.append("no citation lands on a gold page"
                           + ("" if answer.citations else " (answer has no citations)"))
        if forbidden:
            reasons.append(f"contains forbidden claims: {list(forbidden)}")
        grade = "wrong" if reasons else "correct"
        if grade == "correct":
            reasons.append("all required facts present, gold page cited, no forbidden claims")
    return GradeRecord(
        request_id=answer.request_id, question_id=question.id, proposed=grade,
        reasons=tuple(reasons), facts_found=found, facts_missing=missing,
        forbidden_found=forbidden, gold_page_hits=hits,
    )


def propose_grades(run_id: str, questions: QuestionSet,
                   answers: Iterable[AnswerRecord]) -> GradesFile:
    by_id = questions.by_id()
    grades = []
    for answer in answers:
        question = by_id.get(answer.question_id)
        if question is None:
            raise ValueError(f"answer {answer.request_id} refers to unknown question "
                             f"{answer.question_id}")
        grades.append(propose_grade(question, answer))
    return GradesFile(run_id=run_id, grades=tuple(sorted(grades, key=lambda g: g.request_id)))


def review(grades: GradesFile, request_id: str, *, final: AnswerGrade | None = None,
           note: str | None = None) -> GradesFile:
    """Owner review: ``final=None`` confirms the proposal; a different label overrides it."""
    updated: list[GradeRecord] = []
    found = False
    for grade in grades.grades:
        if grade.request_id != request_id:
            updated.append(grade)
            continue
        found = True
        label = grade.proposed if final is None else final
        status: ReviewStatus = "confirmed" if label == grade.proposed else "overridden"
        updated.append(grade.model_copy(update={"final": label, "review_status": status,
                                                "owner_note": note}))
        GradeRecord.model_validate(updated[-1].model_dump())  # re-check invariants
    if not found:
        raise KeyError(f"no grade for request_id {request_id}")
    return grades.model_copy(update={"grades": tuple(updated)})


def summary(grades: GradesFile) -> dict[str, object]:
    return {
        "run_id": grades.run_id,
        "total": len(grades.grades),
        "pending": sum(g.review_status == "pending" for g in grades.grades),
        "overridden": sum(g.review_status == "overridden" for g in grades.grades),
        "proposed": dict(sorted(Counter(g.proposed for g in grades.grades).items())),
        "final": dict(sorted(Counter(g.final for g in grades.grades if g.final).items())),
    }


def load_grades(path: Path) -> GradesFile:
    return GradesFile.model_validate_json(path.read_text(encoding="utf-8"), strict=False)


def write_grades(path: Path, grades: GradesFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(grades.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_answers(path: Path) -> Sequence[AnswerRecord]:
    return [AnswerRecord.model_validate_json(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


__all__ = [
    "AnswerCitation",
    "AnswerRecord",
    "GradeRecord",
    "GradesFile",
    "load_answers",
    "load_grades",
    "normalise",
    "propose_grade",
    "propose_grades",
    "review",
    "summary",
    "write_grades",
]
