"""Evaluation runs (plan V2-12): every question of a set through the pipeline, then graded.

``run_eval`` asks each question with the given pipeline (whose generator is the chosen lane)
and stores the answer, its citations and the passages it was given, with its rubric grade
(and its entailment grade when the L4 model is available).

``ReplayRetriever`` serves the passages v1 actually retrieved for each question, recorded in
the tracked v1 evidence. With it a stand-in run needs no index, no embedding model and no
network: the CI regression gate runs anywhere, deterministically (the stand-in's answers are
extractive, so the same passages give the same answers).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from relay.control.cache import normalise_question
from relay.eval.graders import EntailmentGrader, RubricGrader
from relay.orchestrator.pipeline import Pipeline, PipelineRequest
from relay.orchestrator.verifier import EntailmentScore
from relay.retrieval.hybrid_index import RetrievalResult, RetrievedPassage
from relay.store import Store
from relay.store.eval_repos import EvalRun
from relay.telemetry.spans import ServerSpanTracker


class ReplayRetriever:
    """v1's recorded passages, per question (one retrieval setting of one v1 run)."""

    def __init__(self, setting: str, passages: Mapping[str, Sequence[RetrievedPassage]]) -> None:
        self.setting = setting
        self._by_question = {normalise_question(q): tuple(p) for q, p in passages.items()}
        self._last: tuple[RetrievedPassage, ...] = ()
        self.misses = 0

    @classmethod
    def from_evidence(cls, evidence_dir: Path, run_id: str, setting: str,
                      questions: Mapping[str, str]) -> ReplayRetriever:
        """``questions`` maps the owner's question ids (``q01``) to their text."""
        passages: dict[str, list[RetrievedPassage]] = {}
        path = evidence_dir / "experiments" / run_id / "traces.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            trace = json.loads(line)
            if trace.get("record") != "eval_request" or trace.get("retrieval_setting") != setting:
                continue
            text = questions.get(trace.get("question_id", ""))
            if text is None or text in passages:
                continue
            passages[text] = [RetrievedPassage.model_validate_json(json.dumps(p))
                              for p in trace["outcome"].get("passages") or []]
        if not passages:
            raise ValueError(f"run {run_id} recorded no {setting} passages")
        return cls(setting, passages)

    def retrieve(self, query: str, *, paper_filter: Collection[str] | None = None,
                 include_references: bool = False) -> RetrievalResult:
        found = self._by_question.get(normalise_question(query))
        if found is None:  # the gate's paper-scoped re-query: the question's own passages
            self.misses += 1
            found = tuple(p for p in self._last
                          if paper_filter is None or p.paper_id in paper_filter)
        else:
            self._last = found
        return RetrievalResult(query=query, setting=self.setting,
                               paper_filter=None if paper_filter is None
                               else tuple(sorted(paper_filter)),
                               include_references=include_references, passages=found)


def passage_records(passages: Sequence[RetrievedPassage]) -> list[dict[str, Any]]:
    return [{"rank": p.rank, "chunk_id": p.chunk_id, "paper_id": p.paper_id,
             "short_name": p.short_name, "page_start": p.page_start, "page_end": p.page_end,
             "section": " ".join(filter(None, [p.section_number, p.section_title])) or None,
             "text": p.text} for p in passages]


async def run_eval(store: Store, pipeline: Pipeline, eval_set_id: str, *,
                   retrieval_setting: str, kind: str, collection_id: str | None = None,
                   pipeline_collection: str | None = None, lane: str | None = None,
                   model_id: str | None = None, prompt_version: str | None = None,
                   source: str | None = None, scorer: EntailmentScore | None = None,
                   run: EvalRun | None = None) -> EvalRun:
    """Answer and grade every question of the set. ``pipeline_collection`` is what the
    pipeline answers from (None: the v1 three-paper path, e.g. a replay)."""
    run = run or await store.eval_runs.create(
        eval_set_id, kind=kind, collection_id=collection_id, lane=lane, model_id=model_id,
        retrieval_setting=retrieval_setting, prompt_version=prompt_version, source=source)
    rubric = RubricGrader()
    entailment = None if scorer is None else EntailmentGrader(scorer)
    try:
        for question in await store.eval_questions.list_for_set(eval_set_id):
            outcome = await pipeline.run(PipelineRequest(
                request_id=f"eval-{run.id}-{question.source_id or question.id}"[:128],
                question=question.question, retrieval_setting=retrieval_setting,
                collection_id=pipeline_collection), ServerSpanTracker())
            citations = [{"paper_id": c.paper_id, "page_start": c.page_start,
                          "page_end": c.page_end, "tag": c.tag} for c in outcome.citations]
            passages = passage_records(outcome.passages)
            text = outcome.delivered_text or ""
            answer = await store.eval_answers.create(
                run.id, question.id, answer_text=text, refused=outcome.refused,
                request_id=outcome.request_id, verdict=outcome.verification_verdict,
                citations=citations, passages=passages)
            graded = rubric.grade(question.spec, answer_text=text, refused=outcome.refused,
                                  citations=citations, request_id=outcome.request_id)
            await store.grades.record_grade(question.id, grader="rubric",
                                            verdict=graded.verdict, score=graded.score,
                                            eval_answer_id=answer.id, run_id=run.id,
                                            notes=graded.notes)
            if entailment is not None:
                scored = entailment.grade(answer_text=text, refused=outcome.refused,
                                          passages=passages)
                await store.grades.record_grade(question.id, grader="entailment",
                                                verdict=None, score=scored.score,
                                                eval_answer_id=answer.id, run_id=run.id,
                                                notes=scored.notes)
    except BaseException:
        await store.eval_runs.set_status(run.id, "failed")
        raise
    await store.eval_runs.set_status(run.id, "complete")
    done = await store.eval_runs.get(run.id)
    return done or run


__all__ = ["ReplayRetriever", "passage_records", "run_eval"]
