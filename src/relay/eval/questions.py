"""Evaluation-question schema and validation (ARCHITECTURE.md section 11)."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from relay.ingestion.arxiv_fetcher import PAPERS

PaperId = Literal["1706.03762v7", "2410.00037v1", "2507.07955v2"]
Category = Literal["direct_factual", "multi_hop", "numeric_technical", "cross_paper",
                   "unanswerable"]
# Naive-retriever failure modes (ARCHITECTURE.md section 11 candidate list).
TrapType = Literal["bibliography_match", "shared_vocabulary", "garbled_extraction",
                   "version_leak"]

MIN_QUESTIONS: Final = 15
TARGET_QUESTIONS: Final = 18
MIN_UNANSWERABLE: Final = 3
MIN_TRAPS: Final = 3
FACT_ALTERNATIVE_SEPARATOR: Final = "|"
_PLACEHOLDER = re.compile(r"<[^<>]{1,80}>")

_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")
NonEmpty = Annotated[str, Field(min_length=1)]

if {p.paper_id for p in PAPERS} != set(PaperId.__args__):  # type: ignore[attr-defined]
    raise RuntimeError("PaperId literal is out of sync with relay.ingestion.arxiv_fetcher.PAPERS")


def _no_placeholder(value: str) -> str:
    placeholder = _PLACEHOLDER.search(value)
    if placeholder:
        raise ValueError(f"template placeholder left in text: {placeholder.group(0)!r}")
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class GoldCitation(BaseModel):
    model_config = _STRICT

    paper_id: PaperId
    page: Annotated[int, Field(ge=1)]
    section: str | None = None
    quote: NonEmpty

    @field_validator("quote")
    @classmethod
    def _check_quote(cls, value: str) -> str:
        return _no_placeholder(value)


class EvalQuestion(BaseModel):
    model_config = _STRICT

    id: Annotated[str, Field(pattern=r"^q\d{2,3}$")]
    author: NonEmpty
    category: Category
    question: NonEmpty
    answerable: bool
    expected_answer: str | None
    required_facts: tuple[NonEmpty, ...] = ()
    gold_citations: tuple[GoldCitation, ...] = ()
    forbidden_claims: tuple[NonEmpty, ...] = ()
    trap_type: TrapType | None = None
    notes: str | None = None

    @field_validator("question", "author")
    @classmethod
    def _check_text(cls, value: str) -> str:
        return _no_placeholder(value)

    @field_validator("required_facts", "forbidden_claims")
    @classmethod
    def _check_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _no_placeholder(value)
            if any(not part.strip() for part in value.split(FACT_ALTERNATIVE_SEPARATOR)):
                raise ValueError(f"empty alternative in {value!r}")
        return values

    @model_validator(mode="after")
    def _check_consistency(self) -> EvalQuestion:
        problems: list[str] = []
        if (self.category == "unanswerable") == self.answerable:
            problems.append("category 'unanswerable' must be used exactly when answerable=false")
        if self.answerable:
            if not self.expected_answer or not self.expected_answer.strip():
                problems.append("answerable questions need an expected_answer")
            else:
                _no_placeholder(self.expected_answer)
            if not self.required_facts:
                problems.append("answerable questions need at least one required_fact")
            if not self.gold_citations:
                problems.append("answerable questions need at least one gold_citation")
        elif self.required_facts:
            problems.append("unanswerable questions must not list required_facts")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class QuestionSet(BaseModel):
    model_config = _STRICT

    schema_version: Literal[1]
    questions: tuple[EvalQuestion, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> QuestionSet:
        counts = Counter(q.id for q in self.questions)
        duplicates = sorted(i for i, n in counts.items() if n > 1)
        if duplicates:
            raise ValueError(f"duplicate question ids: {duplicates}")
        return self

    def by_id(self) -> dict[str, EvalQuestion]:
        return {q.id: q for q in self.questions}


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    coverage: dict[str, Any]


def coverage(questions: QuestionSet) -> dict[str, Any]:
    traps = [q.trap_type for q in questions.questions if q.trap_type]
    return {
        "questions": len(questions.questions),
        "by_category": dict(sorted(Counter(q.category for q in questions.questions).items())),
        "unanswerable": sum(not q.answerable for q in questions.questions),
        "retriever_traps": len(traps),
        "trap_types": dict(sorted(Counter(traps).items())),
        "papers_cited": dict(sorted(Counter(
            c.paper_id for q in questions.questions for c in q.gold_citations).items())),
    }


def validate_question_data(data: Any, page_counts: dict[str, int],
                           *, strict_coverage: bool = True,
                           ) -> tuple[QuestionSet | None, ValidationReport]:
    """Validate parsed JSON. Coverage minimums are errors when ``strict_coverage``."""
    try:
        questions = QuestionSet.model_validate(data, strict=False)
    except ValidationError as exc:
        messages = tuple(
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}"
            for err in exc.errors()
        )
        return None, ValidationReport(valid=False, errors=messages, warnings=(), coverage={})
    errors: list[str] = []
    warnings: list[str] = []
    for q in questions.questions:
        for index, citation in enumerate(q.gold_citations):
            limit = page_counts.get(citation.paper_id)
            if limit is not None and citation.page > limit:
                errors.append(f"{q.id}.gold_citations[{index}]: page {citation.page} > "
                              f"{citation.paper_id} page count {limit}")
    stats = coverage(questions)
    shortfalls = []
    if stats["questions"] < MIN_QUESTIONS:
        shortfalls.append(f"{stats['questions']} questions (< {MIN_QUESTIONS} required)")
    if stats["unanswerable"] < MIN_UNANSWERABLE:
        shortfalls.append(f"{stats['unanswerable']} unanswerable (< {MIN_UNANSWERABLE})")
    if stats["retriever_traps"] < MIN_TRAPS:
        shortfalls.append(f"{stats['retriever_traps']} retriever traps (< {MIN_TRAPS})")
    if len(stats["trap_types"]) < MIN_TRAPS:
        shortfalls.append(f"{len(stats['trap_types'])} distinct trap types (< {MIN_TRAPS})")
    (errors if strict_coverage else warnings).extend(f"coverage: {s}" for s in shortfalls)
    if stats["questions"] < TARGET_QUESTIONS:
        warnings.append(f"coverage: {stats['questions']} questions (target {TARGET_QUESTIONS})")
    for category in Category.__args__:  # type: ignore[attr-defined]
        if category not in stats["by_category"]:
            warnings.append(f"coverage: no '{category}' question")
    return questions, ValidationReport(valid=not errors, errors=tuple(errors),
                                       warnings=tuple(warnings), coverage=stats)


def load_question_file(path: Path, page_counts: dict[str, int],
                       *, strict_coverage: bool = True) -> tuple[QuestionSet | None,
                                                                 ValidationReport]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, ValidationReport(valid=False, errors=(f"cannot read {path}: "
                                                           f"{type(exc).__name__}",),
                                      warnings=(), coverage={})
    return validate_question_data(data, page_counts, strict_coverage=strict_coverage)


__all__ = [
    "Category",
    "EvalQuestion",
    "GoldCitation",
    "QuestionSet",
    "TrapType",
    "ValidationReport",
    "coverage",
    "load_question_file",
    "validate_question_data",
]
