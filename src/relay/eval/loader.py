"""``relay eval import-v1``: the v1 evaluation into the store (plan V2-12 task 1).

Reads, never writes, the v1 evidence (CLAUDE.md §5.14):

- ``eval/questions.json``: the owner's 21 questions, stored verbatim (``spec``) in one
  eval set (``v1``) of the ``v1-papers`` collection;
- each canonical v1 eval run (``evidence/experiments/<run>/``): ``answers.jsonl`` (the
  answers), ``traces.jsonl`` (each answer's retrieval setting, model and the passages it was
  given) and ``grades.json`` (the rubric's proposed grade, and the owner's final one if
  reviewed). One eval run per (v1 run, model, retrieval setting).

Importing twice changes nothing: the set, its questions and each run are found again.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from relay.eval.graders import RUBRIC_SCORES, EntailmentGrader, v1_final_to_human
from relay.eval.questions import QuestionSet
from relay.orchestrator.verifier import EntailmentScore
from relay.store import Store

V1_EVAL_RUNS: Final = ("exp-20261113T063331Z-e17e9c1", "exp-20261113T064542Z-c482696")
V1_SET_NAME: Final = "v1"


@dataclass
class ImportReport:
    eval_set_id: str
    questions: int = 0
    runs: list[str] = field(default_factory=list)
    answers: int = 0
    rubric_grades: int = 0
    human_grades: int = 0
    entailment_grades: int = 0
    skipped_runs: list[str] = field(default_factory=list)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _passages(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The passages as the review screen and the entailment grader need them."""
    return [{"rank": p.get("rank"), "chunk_id": p.get("chunk_id"),
             "paper_id": p.get("paper_id"), "short_name": p.get("short_name"),
             "page_start": p.get("page_start"), "page_end": p.get("page_end"),
             "section": " ".join(filter(None, [p.get("section_number"),
                                               p.get("section_title")])) or None,
             "text": p.get("text", "")} for p in outcome.get("passages") or []]


async def import_v1(store: Store, *, questions_file: Path, evidence_dir: Path,
                    collection_id: str, runs: Sequence[str] = V1_EVAL_RUNS,
                    scorer: EntailmentScore | None = None) -> ImportReport:
    # the owner's file, validated as v1 did (JSON mode: lists are its tuples)
    question_set = QuestionSet.model_validate_json(questions_file.read_text(encoding="utf-8"))
    eval_set = await store.eval_sets.find(collection_id, V1_SET_NAME) or \
        await store.eval_sets.create(collection_id, V1_SET_NAME)
    report = ImportReport(eval_set.id)
    stored = await store.eval_questions.list_for_set(eval_set.id)
    if not stored:
        stored = await store.eval_questions.create_batch(
            eval_set.id, [q.model_dump(mode="json") for q in question_set.questions])
        report.questions = len(stored)
    by_source = {q.source_id: q for q in stored}
    entailment = None if scorer is None else EntailmentGrader(scorer)
    for run_id in runs:
        folder = evidence_dir / "experiments" / run_id
        answers = _jsonl(folder / "answers.jsonl")
        traces = {t["request_id"]: t for t in _jsonl(folder / "traces.jsonl")
                  if t.get("record") == "eval_request"}  # v1 eval traces
        grades = {g["request_id"]: g for g in json.loads(
            (folder / "grades.json").read_text(encoding="utf-8"))["grades"]}
        prompt = json.loads((folder / "manifest.json").read_text(encoding="utf-8")).get(
            "config_snapshot", {}).get("prompt_version")
        groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
        for answer in answers:
            trace = traces.get(answer["request_id"], {})
            groups.setdefault((trace.get("model_id"), trace.get("retrieval_setting")),
                              []).append(answer)
        for (model, setting), group in sorted(groups.items(), key=lambda kv: str(kv[0])):
            if await store.eval_runs.find_by_source(eval_set.id, run_id, setting, model):
                report.skipped_runs.append(f"{run_id}:{model}:{setting}")
                continue
            run = await store.eval_runs.create(
                eval_set.id, kind="imported", collection_id=collection_id, model_id=model,
                retrieval_setting=setting, prompt_version=prompt, source=run_id,
                status="complete", notes="imported from v1 evidence (read-only)")
            report.runs.append(run.id)
            for answer in group:
                question = by_source.get(answer["question_id"])
                if question is None:
                    continue
                trace = traces.get(answer["request_id"], {})
                outcome = trace.get("outcome", {})
                passages = _passages(outcome)
                row = await store.eval_answers.create(
                    run.id, question.id, answer_text=answer["answer_text"],
                    refused=bool(answer["refused"]), request_id=answer["request_id"],
                    verdict=outcome.get("verification_verdict"),
                    citations=answer.get("citations", []), passages=passages)
                report.answers += 1
                grade = grades.get(answer["request_id"])
                if grade is not None:
                    await store.grades.record_grade(
                        question.id, grader="rubric", verdict=grade["proposed"],
                        score=RUBRIC_SCORES[grade["proposed"]], eval_answer_id=row.id,
                        run_id=run.id, notes=json.dumps(
                            {"reasons": grade.get("reasons", []), "source": "v1 grades.json",
                             "review_status": grade.get("review_status")}))
                    report.rubric_grades += 1
                    human = v1_final_to_human(grade.get("final"))
                    if human is not None:  # the owner already reviewed it in v1
                        await store.grades.record_grade(
                            question.id, grader="human", verdict=human,
                            score=1.0 if human != "INCORRECT" else 0.0,
                            eval_answer_id=row.id, run_id=run.id,
                            notes=grade.get("owner_note"))
                        report.human_grades += 1
                if entailment is not None:
                    scored = entailment.grade(answer_text=row.answer_text, refused=row.refused,
                                              passages=passages)
                    await store.grades.record_grade(
                        question.id, grader="entailment", verdict=scored.verdict,
                        score=scored.score, eval_answer_id=row.id, run_id=run.id,
                        notes=scored.notes)
                    report.entailment_grades += 1
    return report


__all__ = ["V1_EVAL_RUNS", "V1_SET_NAME", "ImportReport", "import_v1"]
