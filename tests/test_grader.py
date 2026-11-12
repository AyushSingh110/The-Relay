"""Question-schema validation and grading tests.

The questions below are synthetic test fixtures, not evaluation content: the owner
writes the real question set.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from relay.eval.grader import (
    AnswerCitation,
    AnswerRecord,
    load_grades,
    propose_grade,
    propose_grades,
    review,
    summary,
    write_grades,
)
from relay.eval.questions import EvalQuestion, QuestionSet, validate_question_data

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES = {"1706.03762v7": 15, "2410.00037v1": 66, "2507.07955v2": 39}


def answerable(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": "q01", "author": "fixture", "category": "direct_factual",
        "question": "Fixture question about value X?", "answerable": True,
        "expected_answer": "X is 42.",
        "required_facts": ["42|forty-two"],
        "gold_citations": [{"paper_id": "1706.03762v7", "page": 3, "section": None,
                            "quote": "fixture quote"}],
        "forbidden_claims": ["43"], "trap_type": None, "notes": None,
    }
    values.update(overrides)
    return values


def unanswerable(**overrides: Any) -> dict[str, Any]:
    values = answerable(id="q02", category="unanswerable", answerable=False,
                        expected_answer=None, required_facts=[], gold_citations=[],
                        forbidden_claims=["invented number 7"], trap_type="version_leak")
    values.update(overrides)
    return values


def question_set(questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "questions": questions}


def full_set() -> list[dict[str, Any]]:
    """15 synthetic questions meeting the coverage minimums."""
    traps = ["bibliography_match", "shared_vocabulary", "garbled_extraction"]
    items = [answerable(id=f"q{i:02d}", trap_type=traps[i - 1] if i <= 3 else None)
             for i in range(1, 13)]
    items += [unanswerable(id=f"q{i:02d}") for i in range(13, 16)]
    return items


# --- Validation ---------------------------------------------------------------------


def test_valid_full_set_passes_strict_coverage() -> None:
    questions, report = validate_question_data(question_set(full_set()), PAGES)
    assert report.valid, report.errors
    assert questions is not None and len(questions.questions) == 15
    assert report.coverage["unanswerable"] == 3 and report.coverage["retriever_traps"] == 6
    assert any("target 18" in w for w in report.warnings)


def test_coverage_shortfall_is_error_in_strict_mode_only() -> None:
    data = question_set([answerable(), unanswerable()])
    _, strict = validate_question_data(data, PAGES)
    assert not strict.valid and any("required" in e for e in strict.errors)
    _, lenient = validate_question_data(data, PAGES, strict_coverage=False)
    assert lenient.valid and any("coverage" in w for w in lenient.warnings)


@pytest.mark.parametrize(
    ("question", "message"),
    [
        (answerable(category="nonsense"), "category"),
        (answerable(trap_type="made_up"), "trap_type"),
        (answerable(answerable=False), "unanswerable"),
        (answerable(required_facts=[]), "required_fact"),
        (answerable(gold_citations=[]), "gold_citation"),
        (answerable(expected_answer=None), "expected_answer"),
        (answerable(id="Q1"), "id"),
        (answerable(extra_field=1), "extra"),
        (answerable(gold_citations=[{"paper_id": "1706.03762v6", "page": 3, "quote": "q"}]),
         "paper_id"),
        (answerable(question="<the question exactly as a user would ask it>"), "placeholder"),
        (answerable(required_facts=["a||b"]), "empty alternative"),
        (unanswerable(required_facts=["x"]), "must not list required_facts"),
    ],
)
def test_malformed_questions_rejected(question: dict[str, Any], message: str) -> None:
    questions, report = validate_question_data(question_set([question]), PAGES,
                                               strict_coverage=False)
    assert questions is None and not report.valid
    assert any(message in e for e in report.errors), report.errors


def test_duplicate_ids_and_out_of_range_pages() -> None:
    _, dup = validate_question_data(question_set([answerable(), answerable()]), PAGES,
                                    strict_coverage=False)
    assert any("duplicate" in e for e in dup.errors)
    far = answerable(gold_citations=[{"paper_id": "1706.03762v7", "page": 16, "quote": "q"}])
    _, report = validate_question_data(question_set([far]), PAGES, strict_coverage=False)
    assert any("page 16 > 1706.03762v7 page count 15" in e for e in report.errors)


def test_template_is_rejected_until_filled_in() -> None:
    data = json.loads((REPO_ROOT / "eval" / "questions.template.json").read_text("utf-8"))
    questions, report = validate_question_data(data, PAGES, strict_coverage=False)
    assert questions is None
    assert any("placeholder" in e for e in report.errors)


def test_unknown_grade_labels_are_not_accepted_anywhere() -> None:
    q = EvalQuestion.model_validate(answerable(), strict=False)
    grade = propose_grade(q, AnswerRecord(request_id="r1", question_id="q01", answer_text="x",
                                          refused=True))
    with pytest.raises(ValueError):
        review(propose_grades("run", QuestionSet(schema_version=1, questions=(q,)),
                              [AnswerRecord(request_id="r1", question_id="q01",
                                            answer_text="x", refused=True)]),
               "r1", final="partially_correct", note="n")  # type: ignore[arg-type]
    assert grade.proposed in ("correct", "wrong", "refused_correctly", "refused_wrongly")


# --- Grading rules ------------------------------------------------------------------


def q(**overrides: Any) -> EvalQuestion:
    return EvalQuestion.model_validate(answerable(**overrides), strict=False)


def answer(text: str, *, refused: bool = False, pages: tuple[tuple[str, int, int], ...] = (),
           question_id: str = "q01") -> AnswerRecord:
    return AnswerRecord(request_id="r1", question_id=question_id, answer_text=text,
                        refused=refused,
                        citations=tuple(AnswerCitation(paper_id=p, page_start=a, page_end=b)
                                        for p, a, b in pages))


GOLD = (("1706.03762v7", 3, 3),)


def test_correct_requires_facts_gold_citation_and_no_forbidden() -> None:
    grade = propose_grade(q(), answer("X equals **42** [P1].", pages=GOLD))
    assert grade.proposed == "correct" and grade.gold_page_hits == ("1706.03762v7 p.3",)


def test_alternative_wording_counts() -> None:
    assert propose_grade(q(), answer("It is Forty-Two.", pages=GOLD)).proposed == "correct"


def test_correct_fact_with_wrong_citation_is_wrong() -> None:
    wrong_page = propose_grade(q(), answer("X is 42.", pages=(("1706.03762v7", 5, 6),)))
    assert wrong_page.proposed == "wrong"
    assert any("gold page" in r for r in wrong_page.reasons)
    wrong_paper = propose_grade(q(), answer("X is 42.", pages=(("2410.00037v1", 3, 3),)))
    assert wrong_paper.proposed == "wrong"
    no_citation = propose_grade(q(), answer("X is 42."))
    assert no_citation.proposed == "wrong" and "no citations" in no_citation.reasons[0]


def test_page_range_citation_covering_gold_page_counts() -> None:
    assert propose_grade(q(), answer("42", pages=(("1706.03762v7", 2, 4),))).proposed == "correct"


def test_missing_fact_or_forbidden_claim_is_wrong() -> None:
    missing = propose_grade(q(), answer("X is large.", pages=GOLD))
    assert missing.proposed == "wrong" and missing.facts_missing == ("42|forty-two",)
    forbidden = propose_grade(q(), answer("X is 42, or maybe 43.", pages=GOLD))
    assert forbidden.proposed == "wrong" and forbidden.forbidden_found == ("43",)


def test_refusal_labels() -> None:
    assert propose_grade(q(), answer("I cannot verify this.", refused=True)).proposed == \
        "refused_wrongly"
    un = EvalQuestion.model_validate(unanswerable(), strict=False)
    assert propose_grade(un, answer("Not in the papers.", refused=True,
                                    question_id="q02")).proposed == "refused_correctly"
    assert propose_grade(un, answer("It is 7.", question_id="q02")).proposed == "wrong"


def test_answer_for_other_question_rejected() -> None:
    with pytest.raises(ValueError):
        propose_grade(q(), answer("42", question_id="q09"))


# --- Owner review workflow ----------------------------------------------------------


def grades_for(*records: AnswerRecord):  # type: ignore[no-untyped-def]
    questions = QuestionSet(schema_version=1, questions=(q(),))
    return propose_grades("exp-20261112T000000Z-abc1234", questions, records)


def test_proposals_are_pending_and_not_final() -> None:
    grades = grades_for(answer("42", pages=GOLD))
    assert grades.grades[0].review_status == "pending"
    assert grades.final_grades() == {}


def test_confirm_and_override() -> None:
    grades = grades_for(answer("42", pages=GOLD))
    confirmed = review(grades, "r1")
    assert confirmed.final_grades() == {"r1": "correct"}
    assert confirmed.grades[0].review_status == "confirmed"
    with pytest.raises(ValueError, match="owner_note"):
        review(grades, "r1", final="wrong")
    overridden = review(grades, "r1", final="wrong", note="number is from the wrong table")
    assert overridden.grades[0].review_status == "overridden"
    assert overridden.final_grades() == {"r1": "wrong"}
    with pytest.raises(KeyError):
        review(grades, "missing")


def test_grades_file_round_trip_and_summary(tmp_path: Path) -> None:
    grades = review(grades_for(answer("42", pages=GOLD)), "r1")
    path = tmp_path / "grades.json"
    write_grades(path, grades)
    assert load_grades(path) == grades
    assert summary(grades) == {"run_id": grades.run_id, "total": 1, "pending": 0,
                               "overridden": 0, "proposed": {"correct": 1},
                               "final": {"correct": 1}}


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_grading_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "manifest.json").write_text(
        (REPO_ROOT / "data" / "papers" / "manifest.json").read_text("utf-8"), encoding="utf-8")
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps(question_set(full_set())), encoding="utf-8")
    answers = tmp_path / "answers.jsonl"
    answers.write_text(answer("42", pages=GOLD).model_dump_json() + "\n", encoding="utf-8")
    grades = tmp_path / "grades.json"
    cli = _script("grade_answers")
    base = ["--questions", str(questions), "--papers-dir", str(papers)]
    assert cli.main(["propose", "--run-id", "exp-x", "--answers", str(answers),
                     "--out", str(grades), *base]) == 0
    assert cli.main(["propose", "--run-id", "exp-x", "--answers", str(answers),
                     "--out", str(grades), *base]) == 2  # never overwrites reviews
    assert cli.main(["review", "--grades", str(grades), "--request-id", "r1",
                     "--final", "wrong", "--note", "owner decision"]) == 0
    assert load_grades(grades).final_grades() == {"r1": "wrong"}
    validator = _script("validate_questions")
    capsys.readouterr()
    assert validator.main(["--file", str(questions), "--papers-dir", str(papers)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
