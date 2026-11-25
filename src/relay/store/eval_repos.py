"""Evaluation sets, questions, runs, answers and grades in the metadata store (plan V2-12;
docs/ARCHITECTURE_V2.md §10).

- ``eval_set`` (per collection) holds ``eval_question`` rows. A question's full owner-written
  spec (``relay.eval.questions.EvalQuestion`` as JSON) is kept in ``spec``; the questions
  themselves are never written here by code, only imported (project rule: questions are
  written by people).
- ``eval_run`` is one evaluation of a set: imported from v1 evidence, a stand-in run (live
  retrieval or a recorded replay of v1's passages) or, with the owner's approval, a real one.
- ``eval_answer`` is one answer of a run, with the citations and passages it was given.
- ``grade`` holds every grader's grade of an answer (``rubric``, ``entailment``, ``human``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from relay.store.repositories import (
    InvalidValueError,
    NotFoundError,
    _Repo,
)

GRADERS: Final = ("rubric", "entailment", "human")
RUN_KINDS: Final = ("imported", "standin", "replay", "real")
HUMAN_VERDICTS: Final = ("CORRECT", "PARTIAL", "INCORRECT", "UNANSWERABLE_CORRECT")


@dataclass(frozen=True, slots=True)
class EvalSet:
    id: str
    collection_id: str
    name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class EvalQuestionRow:
    id: str
    eval_set_id: str
    source_id: str | None  # the owner's id, e.g. "q01"
    question: str
    spec: dict[str, Any]  # the full owner-written question (EvalQuestion fields)


@dataclass(frozen=True, slots=True)
class EvalRun:
    id: str
    eval_set_id: str
    collection_id: str | None
    kind: str
    lane: str | None
    model_id: str | None
    retrieval_setting: str | None
    prompt_version: str | None
    source: str | None  # an imported v1 run id, or the replayed one
    status: str  # running | complete | failed
    created_at: str
    notes: str | None


@dataclass(frozen=True, slots=True)
class EvalAnswer:
    id: str
    run_id: str
    eval_question_id: str
    request_id: str | None
    answer_text: str
    refused: bool
    verdict: str | None  # the verifier's verdict for the answer
    citations: list[dict[str, Any]]
    passages: list[dict[str, Any]]
    created_at: str


@dataclass(frozen=True, slots=True)
class Grade:
    id: str
    eval_question_id: str
    eval_answer_id: str | None
    run_id: str | None
    grader: str
    verdict: str | None
    score: float | None
    notes: str | None
    created_at: str


def _loads(text: str | None, default: Any) -> Any:
    return default if not text else json.loads(text)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvalSetRepo(_Repo):
    async def create(self, collection_id: str, name: str, *,
                     set_id: str | None = None) -> EvalSet:
        row = EvalSet(set_id or self._ids(), collection_id, name, self._now())
        await self._write("INSERT INTO eval_set (id, collection_id, name, created_at) "
                          "VALUES (?, ?, ?, ?)", (row.id, row.collection_id, row.name,
                                                  row.created_at),
                          context=f"create eval set: collection {collection_id!r} not found")
        return row

    async def get(self, set_id: str) -> EvalSet | None:
        row = await self._one("SELECT id, collection_id, name, created_at FROM eval_set "
                              "WHERE id = ?", (set_id,))
        return None if row is None else EvalSet(*row)

    async def find(self, collection_id: str, name: str) -> EvalSet | None:
        row = await self._one("SELECT id, collection_id, name, created_at FROM eval_set "
                              "WHERE collection_id = ? AND name = ?", (collection_id, name))
        return None if row is None else EvalSet(*row)

    async def list_for_collection(self, collection_id: str) -> list[EvalSet]:
        rows = await self._all("SELECT id, collection_id, name, created_at FROM eval_set "
                               "WHERE collection_id = ? ORDER BY created_at, rowid",
                               (collection_id,))
        return [EvalSet(*row) for row in rows]


_Q_COLUMNS: Final = "id, eval_set_id, source_id, question, spec"


def _question(row: tuple[Any, ...]) -> EvalQuestionRow:
    return EvalQuestionRow(row[0], row[1], row[2], row[3], _loads(row[4], {}))


class EvalQuestionRepo(_Repo):
    async def create_batch(self, eval_set_id: str,
                           specs: Sequence[dict[str, Any]]) -> list[EvalQuestionRow]:
        """Store owner-written questions as they are (one transaction)."""
        rows = [EvalQuestionRow(self._ids(), eval_set_id, spec.get("id"), str(spec["question"]),
                                dict(spec)) for spec in specs]
        sql = ("INSERT INTO eval_question (id, eval_set_id, source_id, question, "
               "required_facts, gold_citations, forbidden_claims, trap_type, spec) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        await self._write_many([(sql, (r.id, eval_set_id, r.source_id, r.question,
                                        _dumps(r.spec.get("required_facts", [])),
                                        _dumps(r.spec.get("gold_citations", [])),
                                        _dumps(r.spec.get("forbidden_claims", [])),
                                        r.spec.get("trap_type"), _dumps(r.spec)))
                                 for r in rows],
                               context=f"add questions: eval set {eval_set_id!r} not found")
        return rows

    async def list_for_set(self, eval_set_id: str) -> list[EvalQuestionRow]:
        rows = await self._all(f"SELECT {_Q_COLUMNS} FROM eval_question WHERE eval_set_id = ? "
                               "ORDER BY source_id, rowid", (eval_set_id,))
        return [_question(row) for row in rows]

    async def get(self, question_id: str) -> EvalQuestionRow | None:
        row = await self._one(f"SELECT {_Q_COLUMNS} FROM eval_question WHERE id = ?",
                              (question_id,))
        return None if row is None else _question(row)


_RUN_COLUMNS: Final = ("id, eval_set_id, collection_id, kind, lane, model_id, "
                       "retrieval_setting, prompt_version, source, status, created_at, notes")


class EvalRunRepo(_Repo):
    async def create(self, eval_set_id: str, *, kind: str, collection_id: str | None = None,
                     lane: str | None = None, model_id: str | None = None,
                     retrieval_setting: str | None = None, prompt_version: str | None = None,
                     source: str | None = None, status: str = "running",
                     notes: str | None = None, run_id: str | None = None) -> EvalRun:
        if kind not in RUN_KINDS:
            raise InvalidValueError(f"run kind must be one of {RUN_KINDS}")
        run = EvalRun(run_id or self._ids(), eval_set_id, collection_id, kind, lane, model_id,
                      retrieval_setting, prompt_version, source, status, self._now(), notes)
        await self._write(f"INSERT INTO eval_run ({_RUN_COLUMNS}) VALUES "
                          "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (run.id, run.eval_set_id, run.collection_id, run.kind, run.lane,
                           run.model_id, run.retrieval_setting, run.prompt_version,
                           run.source, run.status, run.created_at, run.notes),
                          context=f"create eval run: eval set {eval_set_id!r} not found")
        return run

    async def set_status(self, run_id: str, status: str, notes: str | None = None) -> None:
        changed = await self._write("UPDATE eval_run SET status = ?, notes = COALESCE(?, notes) "
                                    "WHERE id = ?", (status, notes, run_id),
                                    context="update eval run")
        if changed == 0:
            raise NotFoundError(f"eval run {run_id!r} does not exist")

    async def get(self, run_id: str) -> EvalRun | None:
        row = await self._one(f"SELECT {_RUN_COLUMNS} FROM eval_run WHERE id = ?", (run_id,))
        return None if row is None else EvalRun(*row)

    async def find_by_source(self, eval_set_id: str, source: str,
                             retrieval_setting: str | None,
                             model_id: str | None) -> EvalRun | None:
        row = await self._one(f"SELECT {_RUN_COLUMNS} FROM eval_run WHERE eval_set_id = ? AND "
                              "source = ? AND kind = 'imported' AND "
                              "COALESCE(retrieval_setting, '') = ? AND "
                              "COALESCE(model_id, '') = ?",
                              (eval_set_id, source, retrieval_setting or "", model_id or ""))
        return None if row is None else EvalRun(*row)

    async def list_for_set(self, eval_set_id: str) -> list[EvalRun]:
        rows = await self._all(f"SELECT {_RUN_COLUMNS} FROM eval_run WHERE eval_set_id = ? "
                               "ORDER BY created_at, rowid", (eval_set_id,))
        return [EvalRun(*row) for row in rows]


_ANSWER_COLUMNS: Final = ("id, run_id, eval_question_id, request_id, answer_text, refused, "
                          "verdict, citations, passages, created_at")


def _answer(row: tuple[Any, ...]) -> EvalAnswer:
    return EvalAnswer(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6],
                      _loads(row[7], []), _loads(row[8], []), row[9])


class EvalAnswerRepo(_Repo):
    async def create(self, run_id: str, eval_question_id: str, *, answer_text: str,
                     refused: bool, request_id: str | None = None, verdict: str | None = None,
                     citations: Sequence[dict[str, Any]] = (),
                     passages: Sequence[dict[str, Any]] = ()) -> EvalAnswer:
        answer = EvalAnswer(self._ids(), run_id, eval_question_id, request_id, answer_text,
                            refused, verdict, list(citations), list(passages), self._now())
        await self._write(f"INSERT INTO eval_answer ({_ANSWER_COLUMNS}) VALUES "
                          "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (answer.id, run_id, eval_question_id, request_id, answer_text,
                           int(refused), verdict, _dumps(answer.citations),
                           _dumps(answer.passages), answer.created_at),
                          context="add eval answer: run or question not found")
        return answer

    async def get(self, answer_id: str) -> EvalAnswer | None:
        row = await self._one(f"SELECT {_ANSWER_COLUMNS} FROM eval_answer WHERE id = ?",
                              (answer_id,))
        return None if row is None else _answer(row)

    async def list_for_run(self, run_id: str) -> list[EvalAnswer]:
        rows = await self._all(f"SELECT {_ANSWER_COLUMNS} FROM eval_answer WHERE run_id = ? "
                               "ORDER BY created_at, rowid", (run_id,))
        return [_answer(row) for row in rows]


_GRADE_COLUMNS: Final = ("id, eval_question_id, eval_answer_id, run_id, grader, verdict, "
                         "score, notes, created_at")


class GradeRepo(_Repo):
    async def record_grade(self, eval_question_id: str, *, grader: str,
                           verdict: str | None, score: float | None,
                           eval_answer_id: str | None = None, run_id: str | None = None,
                           notes: str | None = None) -> Grade:
        if grader not in GRADERS:
            raise InvalidValueError(f"grader must be one of {GRADERS}")
        if grader == "human" and verdict not in HUMAN_VERDICTS:
            raise InvalidValueError(f"a human verdict is one of {HUMAN_VERDICTS}")
        grade = Grade(self._ids(), eval_question_id, eval_answer_id, run_id, grader, verdict,
                      score, notes, self._now())
        await self._write(f"INSERT INTO grade ({_GRADE_COLUMNS}) VALUES "
                          "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (grade.id, eval_question_id, eval_answer_id, run_id, grader,
                           verdict, score, notes, grade.created_at),
                          context="record grade: question not found")
        return grade

    async def list_for_question(self, eval_question_id: str) -> list[Grade]:
        rows = await self._all(f"SELECT {_GRADE_COLUMNS} FROM grade WHERE eval_question_id = ? "
                               "ORDER BY created_at, rowid", (eval_question_id,))
        return [Grade(*row) for row in rows]

    async def list_for_run(self, run_id: str) -> list[Grade]:
        rows = await self._all(f"SELECT {_GRADE_COLUMNS} FROM grade WHERE run_id = ? "
                               "ORDER BY created_at, rowid", (run_id,))
        return [Grade(*row) for row in rows]

    async def get_summary_by_run(self, run_id: str) -> dict[str, dict[str, int]]:
        """Per grader, how many answers got each verdict (the newest grade per answer and
        grader counts: a human may change their mind)."""
        latest: dict[tuple[str, str], Grade] = {}
        for grade in await self.list_for_run(run_id):
            latest[(grade.grader, grade.eval_answer_id or grade.eval_question_id)] = grade
        summary: dict[str, dict[str, int]] = {}
        for (grader, _), grade in latest.items():
            bucket = summary.setdefault(grader, {})
            key = grade.verdict or "none"
            bucket[key] = bucket.get(key, 0) + 1
        return summary


__all__ = ["GRADERS", "HUMAN_VERDICTS", "RUN_KINDS", "EvalAnswer", "EvalAnswerRepo",
           "EvalQuestionRepo", "EvalQuestionRow", "EvalRun", "EvalRunRepo", "EvalSet",
           "EvalSetRepo", "Grade", "GradeRepo"]
