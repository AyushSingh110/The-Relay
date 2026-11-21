"""V2-12: eval sets, questions, runs, answers and grades in the store; the v1 import; the
three graders side by side; the /v1/eval API (tenant-scoped, admin-only writes). Scripted
provider, :memory: store, a fake entailment scorer; no network, no Groq."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from relay.api.routes.eval import EvalContext, eval_router
from relay.app.bootstrap import DEV_TENANT_ID, V1_COLLECTION_ID, ensure_dev_tenant
from relay.eval.graders import EntailmentGrader, HumanGrader, RubricGrader, cited_passages
from relay.eval.loader import V1_EVAL_RUNS, import_v1
from relay.eval.report import format_summary, summarise
from relay.store import open_store
from relay.store.repositories import InvalidValueError
from tests.test_ask_v1 import answer
from tests.test_ingest_worker import Env
from tests.test_v2_10_tenants import ABOUT, World

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


class FakeScorer:
    """Entailment: 0.9 when the passage mentions the sentence's first word, else 0.1."""

    def score(self, sentence: str, passage: str) -> float:
        first = sentence.split()[0].lower() if sentence.split() else ""
        return 0.9 if first and first in passage.lower() else 0.1


# --- import --------------------------------------------------------------------------------
async def v1_collection(env: Env) -> None:
    await env.store.collections.create(DEV_TENANT_ID, "v1-papers",
                                       collection_id=V1_COLLECTION_ID)


async def test_the_v1_questions_answers_and_grades_are_imported_once(env: Env) -> None:
    await v1_collection(env)
    report = await import_v1(env.store, questions_file=ROOT / "eval" / "questions.json",
                             evidence_dir=ROOT / "evidence", collection_id=V1_COLLECTION_ID)
    assert (report.questions, report.answers, report.rubric_grades) == (21, 80, 80)
    assert len(report.runs) == 6  # 3 models x balanced_rerank, qwen x 3 settings
    questions = await env.store.eval_questions.list_for_set(report.eval_set_id)
    owner = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in owner["questions"]}
    for row in questions:  # stored verbatim: never written or edited by code
        assert row.spec == by_id[row.source_id] and row.question == by_id[row.source_id][
            "question"]
    again = await import_v1(env.store, questions_file=ROOT / "eval" / "questions.json",
                            evidence_dir=ROOT / "evidence", collection_id=V1_COLLECTION_ID)
    assert (again.questions, again.answers, len(again.skipped_runs)) == (0, 0, 6)
    answers = await env.store.eval_answers.list_for_run(report.runs[-1])
    assert answers and all(a.passages and a.passages[0]["text"] for a in answers
                           if not a.refused)
    assert set(V1_EVAL_RUNS) == {r.source for r in
                                 await env.store.eval_runs.list_for_set(report.eval_set_id)}


async def test_the_v1_grades_match_their_evidence(env: Env) -> None:
    await v1_collection(env)
    report = await import_v1(env.store, questions_file=ROOT / "eval" / "questions.json",
                             evidence_dir=ROOT / "evidence", collection_id=V1_COLLECTION_ID)
    expected: dict[str, int] = {}
    for run_id in V1_EVAL_RUNS:
        grades = json.loads((ROOT / "evidence" / "experiments" / run_id / "grades.json")
                            .read_text(encoding="utf-8"))["grades"]
        for grade in grades:
            expected[grade["proposed"]] = expected.get(grade["proposed"], 0) + 1
    got: dict[str, int] = {}
    for run_id in report.runs:
        for verdict, count in (await env.store.grades.get_summary_by_run(run_id))[
                "rubric"].items():
            got[verdict] = got.get(verdict, 0) + count
    assert got == expected


# --- graders -------------------------------------------------------------------------------
SPEC = {"id": "q90", "author": "test-fixture", "category": "direct_factual",
        "question": "What kind of model is Moshi?", "answerable": True,
        "expected_answer": "A speech dialogue model.",
        "required_facts": ["speech dialogue model"],
        "gold_citations": [{"paper_id": "doc-1", "page": 1, "quote": "speech dialogue"}],
        "forbidden_claims": ["text-only model"]}
PASSAGES = [{"paper_id": "doc-1", "page_start": 1, "page_end": 1,
             "text": "Moshi is a speech dialogue model for full duplex conversation."}]


def test_the_rubric_grades_any_collection_with_v1_rules() -> None:
    rubric = RubricGrader()
    good = rubric.grade(SPEC, answer_text="Moshi is a speech dialogue model [P1].",
                        refused=False, citations=[{"paper_id": "doc-1", "page_start": 1,
                                                   "page_end": 1}])
    assert (good.verdict, good.score) == ("correct", 1.0)
    bad = rubric.grade(SPEC, answer_text="Moshi is a text-only model [P1].", refused=False,
                       citations=[{"paper_id": "doc-1", "page_start": 1, "page_end": 1}])
    assert bad.verdict == "wrong" and "forbidden" in (bad.notes or "")
    refused = rubric.grade(SPEC, answer_text="", refused=True, citations=[])
    assert refused.verdict == "refused_wrongly"


def test_entailment_scores_each_sentence_against_its_cited_passages() -> None:
    grader = EntailmentGrader(FakeScorer())
    result = grader.grade(answer_text="Moshi is a speech model [P1]. Zebras run [P1].",
                          refused=False, passages=PASSAGES)
    assert result.score == pytest.approx((0.9 + 0.1) / 2)
    assert grader.grade(answer_text="", refused=True, passages=PASSAGES).score is None
    assert cited_passages("No tag here.", PASSAGES) == []


def test_human_verdicts_are_a_closed_set() -> None:
    assert HumanGrader.grade("PARTIAL").score == 0.5
    with pytest.raises(ValueError):
        HumanGrader.grade("MAYBE")


async def test_a_human_verdict_outside_the_set_is_refused_by_the_store(env: Env) -> None:
    await v1_collection(env)
    eval_set = await env.store.eval_sets.create(V1_COLLECTION_ID, "s")
    (question,) = await env.store.eval_questions.create_batch(eval_set.id, [SPEC])
    with pytest.raises(InvalidValueError):
        await env.store.grades.record_grade(question.id, grader="human", verdict="GOOD",
                                            score=1.0)


# --- the API -------------------------------------------------------------------------------
class EvalWorld(World):
    def __init__(self, env: Env, replies: list[Any]) -> None:
        super().__init__(env, replies)
        self.evaluation = EvalContext(store=env.store, service=self.service, auth=self.auth,
                                      scorer=FakeScorer())
        self.h.app.include_router(eval_router(self.evaluation))

    def post(self, key: str, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})


async def eval_set_for(w: World, tenant: str) -> tuple[str, str]:
    cid = await w.collection(tenant)
    (doc,) = await w.env.store.documents.list_for_collection(cid)
    spec = {**SPEC, "gold_citations": [{"paper_id": doc.id, "page": 1, "quote": "speech"}]}
    eval_set = await w.env.store.eval_sets.create(cid, "smoke")
    await w.env.store.eval_questions.create_batch(eval_set.id, [spec])
    return cid, eval_set.id


async def wait_done(w: EvalWorld, key: str, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        run = (await w.get(key, f"/v1/eval/runs/{run_id}")).json()
        if run["run"]["status"] != "running":
            return run
        await asyncio.sleep(0.02)
    raise AssertionError("the eval run did not finish")


async def test_a_run_is_graded_side_by_side_and_the_owner_grades_it(env: Env) -> None:
    w = EvalWorld(env, [answer(ABOUT)])
    tenant, key = await w.tenant("acme")
    _, set_id = await eval_set_for(w, tenant)
    sets = (await w.get(key, "/v1/eval/sets")).json()["sets"]
    assert [(s["id"], s["questions"]) for s in sets] == [(set_id, 1)]
    started = await w.post(key, "/v1/eval/runs", {"eval_set_id": set_id})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    run = await wait_done(w, key, run_id)
    assert run["run"]["status"] == "complete"
    (item,) = run["answers"]
    assert item["answer_text"].startswith("Moshi is a speech dialogue model")
    assert item["passages"] and item["passages"][0]["text"]
    assert item["grades"]["rubric"]["verdict"] == "correct"
    assert item["grades"]["entailment"]["score"] == pytest.approx(0.9)
    assert item["grades"]["human"] is None
    graded = await w.post(key, "/v1/eval/grades", {"answer_id": item["id"],
                                                   "verdict": "CORRECT", "notes": "ok"})
    assert graded.status_code == 201
    summary = (await w.get(key, f"/v1/eval/runs/{run_id}/summary")).json()
    assert summary["rubric"]["correctness"] == 1.0
    assert summary["entailment"]["mean"] == pytest.approx(0.9)
    assert summary["human"] == {"graded": 1, "verdicts": {"CORRECT": 1}, "correct_share": 1.0}
    assert summary["rubric_vs_human"] == {"both": 1, "agree": 1}
    calibration = summary["l4_calibration"]
    assert isinstance(calibration, list) and calibration[0]["tp"] == 1
    text = format_summary(await summarise(env.store, run_id))
    assert "q90" in text and "CORRECT" in text


async def test_eval_routes_are_tenant_scoped_and_writes_need_an_admin(env: Env) -> None:
    w = EvalWorld(env, [answer(ABOUT)])
    a, key_a = await w.tenant("a")
    _, key_b = await w.tenant("b")
    _, member = await w.tenant("m", role="member")
    _, set_id = await eval_set_for(w, a)
    run = await env.store.eval_runs.create(set_id, kind="standin", status="complete")
    assert (await w.get(key_b, f"/v1/eval/sets/{set_id}/questions")).status_code == 404
    assert (await w.get(key_b, f"/v1/eval/runs/{run.id}")).status_code == 404
    assert (await w.get(key_b, f"/v1/eval/runs/{run.id}/summary")).status_code == 404
    assert (await w.get(key_b, "/v1/eval/sets")).json()["sets"] == []
    assert (await w.post(key_b, "/v1/eval/runs", {"eval_set_id": set_id})).status_code == 404
    assert (await w.post(member, "/v1/eval/runs", {"eval_set_id": set_id})).status_code == 403
    assert (await w.post(key_a, "/v1/eval/grades",
                         {"answer_id": "nope", "verdict": "CORRECT"})).status_code == 404
    assert (await w.post(key_a, "/v1/eval/grades",
                         {"answer_id": "nope", "verdict": "GREAT"})).status_code == 422
    assert (await w.client.get("/v1/eval/sets")).status_code == 401


async def test_a_real_provider_lane_is_refused_without_approval(env: Env,
                                                               monkeypatch: Any) -> None:
    w = EvalWorld(env, [])
    tenant, key = await w.tenant("acme")
    _, set_id = await eval_set_for(w, tenant)
    lane = w.service.router.primary
    monkeypatch.setattr(type(lane.provider), "source", property(lambda self: "real"))
    refused = await w.post(key, "/v1/eval/runs", {"eval_set_id": set_id})
    assert refused.status_code == 403 and refused.json()["error"] == "real_runs_disabled"


# --- the CLI -------------------------------------------------------------------------------
def test_relay_eval_import_run_and_report(tmp_path: Path, monkeypatch: Any,
                                          capsys: Any) -> None:
    from relay.cli import main

    monkeypatch.setenv("RELAY_PATHS__STORE_PATH", str(tmp_path / "relay.sqlite3"))
    monkeypatch.chdir(tmp_path)  # no relay.toml: defaults; files given explicitly
    files = ["--questions", str(ROOT / "eval" / "questions.json"),
             "--evidence", str(ROOT / "evidence")]
    assert main(["eval", "import-v1", *files]) == 0
    assert "21 questions added, 6 runs, 80 answers, 80 rubric grades" in capsys.readouterr().out
    summary = tmp_path / "summary.json"
    assert main(["eval", "run", "--replay", *files, "--summary-out", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "q01" in out and "grounded" in out
    run_id = json.loads(summary.read_text(encoding="utf-8"))["gate"]["run"]
    assert main(["eval", "report", "--run", run_id]) == 0
    assert "refusal precision" in capsys.readouterr().out
    assert main(["eval", "report", "--run", "nope"]) == 1
    assert main(["eval", "gate", "--baseline", run_id, "--candidate", str(summary)]) == 0


async def test_a_collection_with_eval_runs_can_still_be_deleted(env: Env) -> None:
    """Review fix: V2-10's delete must also remove the collection's eval runs, answers and
    their grades (migration 9's tables reference its sets and questions)."""
    w = EvalWorld(env, [])
    tenant, key = await w.tenant("acme")
    cid, set_id = await eval_set_for(w, tenant)
    (question,) = await env.store.eval_questions.list_for_set(set_id)
    run = await env.store.eval_runs.create(set_id, kind="standin", status="complete")
    answer_row = await env.store.eval_answers.create(run.id, question.id, answer_text="x",
                                                     refused=False)
    await env.store.grades.record_grade(question.id, grader="human", verdict="CORRECT",
                                        score=1.0, eval_answer_id=answer_row.id, run_id=run.id)
    deleted = await w.client.delete(f"/v1/collections/{cid}",
                                    headers={"Authorization": f"Bearer {key}"})
    assert deleted.status_code == 200
    async with env.store.conn.execute(
            "SELECT (SELECT COUNT(*) FROM eval_run), (SELECT COUNT(*) FROM eval_answer), "
            "(SELECT COUNT(*) FROM grade), (SELECT COUNT(*) FROM eval_set)") as cursor:
        assert tuple(await cursor.fetchone()) == (0, 0, 0, 0)  # type: ignore[arg-type]
