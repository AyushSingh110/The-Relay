"""``/v1/eval``: evaluation sets, runs, the side-by-side summary and the owner's grades
(plan V2-12; docs/ARCHITECTURE_V2.md §10).

Every route is scoped to the caller's tenant through the eval set's collection (another
tenant's set, run or answer is ``404``). Starting a run and grading need an admin key.

- ``GET  /v1/eval/sets``                     the caller's eval sets
- ``GET  /v1/eval/sets/{id}/questions``      the owner's questions (as imported)
- ``GET  /v1/eval/sets/{id}/runs``           its runs
- ``POST /v1/eval/runs``                     a new run (in the background; ``202``)
- ``GET  /v1/eval/runs/{id}``                its answers, passages and grades
- ``GET  /v1/eval/runs/{id}/summary``        rubric vs entailment vs human
- ``POST /v1/eval/grades``                   the owner's grade of one answer

A run on a lane that calls a real provider is refused (``403 real_runs_disabled``) unless
the service was started with real eval runs allowed: real calls need the owner's approval.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.api.app import RelayService
from relay.api.auth import Authenticator, TenantContext, require_admin
from relay.app.bootstrap import V1_COLLECTION_ID
from relay.app.routes.rejected import Rejected
from relay.errors import sanitize_error
from relay.eval.graders import HumanGrader
from relay.eval.report import summarise
from relay.eval.runner import run_eval
from relay.orchestrator.verifier import EntailmentScore
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.store import Store
from relay.store.eval_repos import EvalRun, EvalSet

logger = logging.getLogger(__name__)
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


class StartRun(BaseModel):
    model_config = _STRICT

    eval_set_id: Annotated[str, Field(min_length=1, max_length=64)]
    lane: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    preset: Annotated[str, Field(max_length=64)] | None = None


class HumanGradeBody(BaseModel):
    model_config = _STRICT

    answer_id: Annotated[str, Field(min_length=1, max_length=64)]
    verdict: Literal["CORRECT", "PARTIAL", "INCORRECT", "UNANSWERABLE_CORRECT"]
    notes: Annotated[str, Field(max_length=4000)] | None = None


@dataclass
class EvalContext:
    store: Store
    service: RelayService
    auth: Authenticator
    scorer: EntailmentScore | None = None
    allow_real_runs: bool = False
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    async def aclose(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    headers = {"www-authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status,
                        headers=headers)


def _run_view(run: EvalRun) -> dict[str, Any]:
    return {"id": run.id, "eval_set_id": run.eval_set_id, "collection_id": run.collection_id,
            "kind": run.kind, "lane": run.lane, "model_id": run.model_id,
            "retrieval_setting": run.retrieval_setting, "prompt_version": run.prompt_version,
            "source": run.source, "status": run.status, "created_at": run.created_at,
            "notes": run.notes}


def eval_router(ctx: EvalContext) -> APIRouter:
    router = APIRouter(prefix="/v1/eval")
    store = ctx.store

    async def caller(request: Request) -> TenantContext:
        return await ctx.auth.authenticate(request)

    async def own_set(set_id: str, who: TenantContext) -> EvalSet:
        found = await store.eval_sets.get(set_id)
        collection = None if found is None else await store.collections.get_by_id(
            found.collection_id)
        if found is None or collection is None or collection.tenant_id != who.tenant_id:
            raise Rejected(404, "eval_set_not_found", "no such eval set")
        return found

    async def own_run(run_id: str, who: TenantContext) -> EvalRun:
        run = await store.eval_runs.get(run_id)
        if run is None:
            raise Rejected(404, "eval_run_not_found", "no such eval run")
        try:
            await own_set(run.eval_set_id, who)
        except Rejected:
            raise Rejected(404, "eval_run_not_found", "no such eval run") from None
        return run

    @router.get("/sets")
    async def list_sets(request: Request) -> JSONResponse:
        try:
            who = await caller(request)
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        sets: list[dict[str, Any]] = []
        for collection in await store.collections.list_for_tenant(who.tenant_id):
            for found in await store.eval_sets.list_for_collection(collection.id):
                questions = await store.eval_questions.list_for_set(found.id)
                sets.append({"id": found.id, "name": found.name,
                             "collection_id": collection.id,
                             "collection_name": collection.name,
                             "questions": len(questions), "created_at": found.created_at})
        return JSONResponse({"sets": sets})

    @router.get("/sets/{set_id}/questions")
    async def list_questions(set_id: str, request: Request) -> JSONResponse:
        try:
            found = await own_set(set_id, await caller(request))
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        return JSONResponse({"questions": [
            {"id": q.id, "source_id": q.source_id, "question": q.question, "spec": q.spec}
            for q in await store.eval_questions.list_for_set(found.id)]})

    @router.get("/sets/{set_id}/runs")
    async def list_runs(set_id: str, request: Request) -> JSONResponse:
        try:
            found = await own_set(set_id, await caller(request))
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        return JSONResponse({"runs": [_run_view(r)
                                      for r in await store.eval_runs.list_for_set(found.id)]})

    @router.post("/runs")
    async def start_run(request: Request) -> JSONResponse:
        try:
            who = await caller(request)
            require_admin(who)
            body = StartRun.model_validate_json(await request.body())
            found = await own_set(body.eval_set_id, who)
            preset = body.preset or "small_fast"
            if preset not in RETRIEVAL_SETTINGS:
                raise Rejected(422, "unknown_preset",
                               f"preset must be one of {sorted(RETRIEVAL_SETTINGS)}")
            router_ = ctx.service.router
            if router_ is None:
                raise Rejected(503, "no_router", "the service has no provider router")
            lane_name = body.lane or router_.primary.name
            try:
                lane = router_.lane(lane_name)
            except KeyError:
                raise Rejected(404, "lane_not_found", f"no lane {lane_name!r}") from None
            if lane.source == "real" and not ctx.allow_real_runs:
                raise Rejected(403, "real_runs_disabled",
                               "this lane calls a real provider; real eval runs need the "
                               "owner's approval (start the service with them allowed)")
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        except ValidationError:
            return _error(422, "invalid_request", "send {eval_set_id, lane?, preset?}")
        from relay.control.router import RoutedGenerator

        pipeline = ctx.service.pipeline.with_generator(
            RoutedGenerator(router_).pinned(lane, "answer"))
        collection = None if found.collection_id == V1_COLLECTION_ID else found.collection_id
        run = await store.eval_runs.create(
            found.id, kind="real" if lane.source == "real" else "standin",
            collection_id=found.collection_id, lane=lane.name, model_id=lane.model,
            retrieval_setting=preset)

        async def work() -> None:
            try:
                await run_eval(store, pipeline, found.id, retrieval_setting=preset,
                               kind=run.kind, collection_id=found.collection_id,
                               pipeline_collection=collection, scorer=ctx.scorer, run=run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # recorded as failed by run_eval; keep the service up
                logger.error("eval run %s failed: %s", run.id, sanitize_error(exc))

        task = asyncio.create_task(work(), name=f"eval-run-{run.id}")
        ctx.tasks.add(task)
        task.add_done_callback(ctx.tasks.discard)
        return JSONResponse({"run_id": run.id, "status": "running"}, status_code=202)

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> JSONResponse:
        try:
            run = await own_run(run_id, await caller(request))
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        questions = {q.id: q for q in await store.eval_questions.list_for_set(run.eval_set_id)}
        latest: dict[tuple[str, str], Any] = {}
        for grade in await store.grades.list_for_run(run.id):
            latest[(grade.eval_answer_id or "", grade.grader)] = grade
        answers = []
        for answer in await store.eval_answers.list_for_run(run.id):
            question = questions.get(answer.eval_question_id)
            grades = {name: None if (g := latest.get((answer.id, name))) is None else
                      {"verdict": g.verdict, "score": g.score, "notes": g.notes,
                       "created_at": g.created_at}
                      for name in ("rubric", "entailment", "human")}
            answers.append({
                "id": answer.id, "question_id": answer.eval_question_id,
                "source_id": None if question is None else question.source_id,
                "question": None if question is None else question.question,
                "answerable": True if question is None
                else bool(question.spec.get("answerable", True)),
                "answer_text": answer.answer_text, "refused": answer.refused,
                "verdict": answer.verdict, "citations": answer.citations,
                "passages": answer.passages, "grades": grades})
        return JSONResponse({"run": _run_view(run), "answers": answers})

    @router.get("/runs/{run_id}/summary")
    async def run_summary(run_id: str, request: Request) -> JSONResponse:
        try:
            run = await own_run(run_id, await caller(request))
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        return JSONResponse(await summarise(store, run.id))

    @router.post("/grades")
    async def human_grade(request: Request) -> JSONResponse:
        try:
            who = await caller(request)
            require_admin(who)
            body = HumanGradeBody.model_validate_json(await request.body())
            answer = await store.eval_answers.get(body.answer_id)
            if answer is None:
                raise Rejected(404, "eval_answer_not_found", "no such answer")
            try:
                run = await own_run(answer.run_id, who)
            except Rejected:
                raise Rejected(404, "eval_answer_not_found", "no such answer") from None
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        except ValidationError:
            return _error(422, "invalid_request",
                          "send {answer_id, verdict: CORRECT|PARTIAL|INCORRECT|"
                          "UNANSWERABLE_CORRECT, notes?}")
        graded = HumanGrader.grade(body.verdict, body.notes)
        grade = await store.grades.record_grade(
            answer.eval_question_id, grader="human", verdict=graded.verdict,
            score=graded.score, eval_answer_id=answer.id, run_id=run.id, notes=graded.notes)
        return JSONResponse({"id": grade.id, "answer_id": answer.id, "verdict": grade.verdict,
                             "score": grade.score, "created_at": grade.created_at},
                            status_code=201)

    return router


__all__ = ["EvalContext", "eval_router"]
