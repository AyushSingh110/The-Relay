"""P07 tests: the evaluation runner, its artifacts and the CLI safeguards. Fully offline.

The model is a ``ScriptedProvider`` and retrieval a scripted stub. Rubric mechanics use a
small synthetic question set defined here (test fixtures, not evaluation content); the
owner's ``eval/questions.json`` is only loaded, never asserted on or modified.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from relay.config import Settings
from relay.control.retry import RetryConfig
from relay.errors import ConfigurationError
from relay.eval.grader import load_grades
from relay.eval.questions import QuestionSet
from relay.experiments import eval_cli
from relay.experiments.eval_runner import (
    COMPARISON_QUESTIONS,
    EvalRunner,
    build_lane,
    format_table,
    guard_config,
    live_problems,
    plan_items,
    write_artifacts,
)
from relay.providers.base import (
    ChatRequest,
    ProviderEvent,
    RealCallBudget,
    ScriptedProvider,
    ScriptedReply,
)
from relay.providers.errors import ProviderAuthError, ProviderServerError
from relay.schemas import ResultRow
from relay.telemetry.csv_codec import read_rows
from relay.telemetry.run_store import RunHandle, create_run, load_manifest
from tests.factories import FAKE_KEY
from tests.test_pipeline_and_verifier import (
    ATTENTION,
    GOOD_ANSWER,
    PASSAGES,
    StubRetriever,
    passage,
    reply,
)

SETTING = "balanced_rerank"
UNGROUNDED = "The encoder uses d_model = 1024 [P1]."
OFF_TOPIC = tuple(passage(f"t{i}", rerank=-9.0 - i, chunk_id=f"X{i}") for i in range(4))

QUESTIONS = QuestionSet.model_validate({
    "schema_version": 1,
    "questions": [
        {"id": "q01", "author": "test", "category": "numeric_technical",
         "question": "How many layers and what d_model does the encoder use?",
         "answerable": True, "expected_answer": "N = 6 layers, d_model = 512.",
         "required_facts": ["N = 6", "512"],
         "gold_citations": [{"paper_id": ATTENTION, "page": 3, "quote": "N = 6"}]},
        {"id": "q02", "author": "test", "category": "direct_factual",
         "question": "How many attention heads does the encoder use?", "answerable": True,
         "expected_answer": "8 heads.", "required_facts": ["h = 8"],
         "gold_citations": [{"paper_id": ATTENTION, "page": 4, "quote": "h = 8"}]},
        {"id": "q03", "author": "test", "category": "unanswerable",
         "question": "Who won the football league last year?", "answerable": False,
         "expected_answer": None},
    ],
}, strict=False)


def make_run(tmp_path: Path, scenario: str = "model_comparison") -> RunHandle:
    return create_run(tmp_path / "evidence", "experiment", scenarios=[scenario],
                      config_snapshot={}, source="stand-in", cache_mode="off",
                      git_state=("abc1234", False))


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def lane_for(model: str, replies: Sequence[ScriptedReply], *results: Sequence[Any],
             rpd: int | None = 1000, fraction: float = 0.75,
             provider: Any = None) -> Any:
    retriever = StubRetriever(*(results or (PASSAGES,)))
    return build_lane(provider or ScriptedProvider(replies), model_id=model,
                      retrievers={SETTING: retriever}, top_k={SETTING: 4},
                      config=guard_config(rpd=rpd, daily_safety_fraction=fraction, rpm=None),
                      retry=RetryConfig(max_attempts=2), max_completion_tokens=300,
                      sleep=_no_sleep)


def rows_of(run: RunHandle) -> list[ResultRow]:
    return read_rows(run.run_dir / "rows.csv")


def traces_of(run: RunHandle) -> list[dict[str, Any]]:
    return [json.loads(line) for line in
            (run.run_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()]


# --- Planning ------------------------------------------------------------------------


def test_plan_items_is_question_major_with_models_innermost() -> None:
    items = plan_items(QUESTIONS, question_ids=("q02", "q01"), models=("a", "b"),
                       settings=("small_fast", SETTING))
    assert [(i.question.id, i.retrieval_setting, i.model_id) for i in items] == [
        ("q02", "small_fast", "a"), ("q02", "small_fast", "b"), ("q02", SETTING, "a"),
        ("q02", SETTING, "b"), ("q01", "small_fast", "a"), ("q01", "small_fast", "b"),
        ("q01", SETTING, "a"), ("q01", SETTING, "b")]
    assert len(plan_items(QUESTIONS, question_ids=None, models=("a",), settings=(SETTING,))) == 3
    for bad in ({"question_ids": ("q99",)}, {"question_ids": ("q01", "q01")},
                {"settings": ("nope",)}):
        kwargs: dict[str, Any] = {"question_ids": ("q01",), "models": ("a",),
                                  "settings": (SETTING,), **bad}
        with pytest.raises(ConfigurationError):
            plan_items(QUESTIONS, **kwargs)
    assert len(COMPARISON_QUESTIONS) == 6


def test_evaluation_lanes_never_degrade_and_run_one_call_at_a_time() -> None:
    lane = lane_for("m", [])
    assert lane.pipeline._budget is None  # type: ignore[attr-defined]
    assert lane.guard.config.max_concurrency == 1


# --- Runner --------------------------------------------------------------------------


async def test_rows_traces_grades_audit_and_summary(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    lanes = {
        "m1": lane_for("m1", [reply(GOOD_ANSWER), reply(GOOD_ANSWER)]),
        "m2": lane_for("m2", [reply(UNGROUNDED), reply("INSUFFICIENT_EVIDENCE")]),
    }
    lanes["m1"].pipeline._gateway._retrievers[SETTING] = StubRetriever(  # type: ignore[attr-defined]
        PASSAGES, PASSAGES, OFF_TOPIC)
    lanes["m2"].pipeline._gateway._retrievers[SETTING] = StubRetriever(  # type: ignore[attr-defined]
        PASSAGES, PASSAGES, OFF_TOPIC)
    items = plan_items(QUESTIONS, question_ids=None, models=("m1", "m2"), settings=(SETTING,))
    result = await EvalRunner(run=run, lanes=lanes, scenario="model_comparison",
                              source="stand-in").run(items)
    summary = write_artifacts(run, result, QUESTIONS)
    run.finalize()

    rows = rows_of(run)
    assert len(rows) == 6  # the CSV round trip rounds start_time, so compare ids
    assert [r.request_id for r in rows] == [r.request_id for r in result.rows]
    for row in rows:
        assert (row.scenario, row.source, row.cache_status) == (
            "model_comparison", "stand-in", "off")
        assert row.answer_grade is None and row.queue_depth_at_entry is None
        assert row.question_id in {"q01", "q02", "q03"} and row.retrieval_setting == SETTING
        assert row.first_token_ms is not None  # buffered: delivered all at once, at the end
        assert 0 <= row.total_ms - row.first_token_ms <= 0.05
        assert abs(row.waiting_ms + row.working_ms + row.retrying_ms  # type: ignore[operator]
                   - row.server_total_ms) <= 1.0  # type: ignore[operator]
    by = {(r.question_id, r.model_id or "none"): r for r in rows}
    assert by[("q01", "m1")].verification_verdict == "VERIFIED"
    assert by[("q01", "m2")].verification_verdict == "BLOCKED"
    assert by[("q02", "m2")].verification_verdict == "REFUSAL_VERIFIED"
    early = [r for r in rows if r.question_id == "q03"]
    assert {r.verification_verdict for r in early} == {"EARLY_REFUSAL"}
    assert all(not r.provider_called and r.tokens_in == 0 for r in early)

    traces = traces_of(run)
    assert [t["record"] for t in traces] == ["eval_request"] * 6
    assert {t["request_id"] for t in traces} == {r.request_id for r in rows}
    first = traces[0]
    assert first["outcome"]["trace"]["raw_answer"] == GOOD_ANSWER
    assert first["guard"]["day_requests"] == 1 and first["spans"]["working_ms"] > 0

    grades = load_grades(run.run_dir / "grades.json")
    assert len(grades.grades) == 6 and {g.review_status for g in grades.grades} == {"pending"}
    proposed = {(result.items[g.request_id].question.id, result.items[g.request_id].model_id):
                g.proposed for g in grades.grades}
    assert proposed == {
        ("q01", "m1"): "correct", ("q01", "m2"): "refused_wrongly",  # safe refusal
        ("q02", "m1"): "correct", ("q02", "m2"): "refused_wrongly",
        ("q03", "m1"): "refused_correctly", ("q03", "m2"): "refused_correctly"}
    answers = (run.run_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(answers) == 6
    audit = json.loads((run.run_dir / "answers_audit.json").read_text(encoding="utf-8"))
    blocked = next(a for a in audit if a["verification_verdict"] == "BLOCKED")
    assert blocked["raw_answer"] == UNGROUNDED and blocked["verifier_failures"]
    assert blocked["proposed_grade"] == "refused_wrongly" and blocked["gold_pages"]
    assert all(a["retrieved"] for a in audit)

    table = {r["model_id"]: r for r in summary["table"]}
    assert table["m1"]["proposed_correct_rate_answerable"] == 1.0
    assert table["m2"]["proposed_correct_rate_answerable"] == 0.0
    assert table["m1"]["proposed_refused_correctly_rate_unanswerable"] == 1.0
    assert (table["m1"]["citation_pass_rate"], table["m2"]["citation_pass_rate"]) == (1.0, 0.0)
    assert table["m1"]["provider_calls"] == 2 and table["m1"]["early_refusals"] == 1
    assert table["m1"]["tokens_in_mean"] == 900.0 and table["m1"]["usage_sources"] == {
        "provider": 2}
    m1 = table["m1"]
    assert m1["provider_ttft_excl_wait_ms"]["n"] == 2 and m1["quota_wait_ms"]["n"] == 2
    assert m1["provider_ttft_excl_wait_ms"]["p50"] <= m1["provider_ttft_ms"]["p50"]
    assert summary["answers_per_day"]["rpd_bound_per_model"] == 750
    assert summary["answers_per_day"]["tpd_bound"] is None
    assert "pending owner review" in summary["grades_status"]
    assert "m1 | balanced_rerank | 3 | 2" in format_table(summary)
    assert json.loads((run.run_dir / "summary.json").read_text(encoding="utf-8")) == summary


async def test_auth_failure_stops_the_whole_run_and_is_ungraded(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    lanes = {"m1": lane_for("m1", [ScriptedReply(error=ProviderAuthError("no"))])}
    items = plan_items(QUESTIONS, question_ids=("q01", "q02"), models=("m1",),
                       settings=(SETTING,))
    result = await EvalRunner(run=run, lanes=lanes, scenario="model_comparison",
                              source="stand-in").run(items)
    write_artifacts(run, result, QUESTIONS)
    assert [(r.status, r.error_code) for r in result.rows] == [("error", "provider_auth_error")]
    assert result.stopped_reason == "provider rejected the credentials"
    assert [n["question_id"] for n in result.not_run] == ["q02"]
    assert load_grades(run.run_dir / "grades.json").grades == ()  # nothing delivered


async def test_call_cap_stops_before_the_next_request(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    budget = RealCallBudget(1)
    lanes = {"m1": lane_for("m1", [reply(GOOD_ANSWER), reply(GOOD_ANSWER)])}
    original = lanes["m1"].pipeline._generator._call._provider  # type: ignore[attr-defined]

    class Counted:
        source = "stand-in"

        async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
            budget.consume()
            async for event in original.stream_chat(request):
                yield event

    lanes["m1"].pipeline._generator._call._provider = Counted()  # type: ignore[attr-defined]
    items = plan_items(QUESTIONS, question_ids=("q01", "q02"), models=("m1",),
                       settings=(SETTING,))
    result = await EvalRunner(run=run, lanes=lanes, scenario="model_comparison",
                              source="stand-in",
                              calls_remaining=lambda: budget.remaining).run(items)
    assert len(result.rows) == 1 and result.stopped_reason == "--max-real-calls reached"
    assert result.not_run[0]["question_id"] == "q02"


async def test_cap_reached_during_a_retry_is_traced_not_rowed(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    budget = RealCallBudget(1)
    scripted = ScriptedProvider([ScriptedReply(error=ProviderServerError("boom")),
                                 reply(GOOD_ANSWER)])

    class Capped:
        source = "stand-in"

        async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
            budget.consume()
            async for event in scripted.stream_chat(request):
                yield event

    lanes = {"m1": lane_for("m1", [], provider=Capped())}
    items = plan_items(QUESTIONS, question_ids=("q01", "q02"), models=("m1",),
                       settings=(SETTING,))
    result = await EvalRunner(run=run, lanes=lanes, scenario="model_comparison",
                              source="stand-in").run(items)
    assert result.rows == [] and result.stopped_reason == "--max-real-calls reached during a retry"
    (trace,) = traces_of(run)
    assert trace["record"] == "eval_stopped" and trace["guard"]["day_requests"] == 2
    assert "RealCallBudgetExceededError" in trace["error"]


async def test_daily_safety_stop_skips_only_that_model(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    lanes = {"m1": lane_for("m1", [reply(GOOD_ANSWER)] * 2, rpd=2, fraction=0.5),
             "m2": lane_for("m2", [reply(GOOD_ANSWER)] * 2)}
    items = plan_items(QUESTIONS, question_ids=("q01", "q02"), models=("m1", "m2"),
                       settings=(SETTING,))
    result = await EvalRunner(run=run, lanes=lanes, scenario="model_comparison",
                              source="stand-in").run(items)
    assert [(r.question_id, r.model_id) for r in result.rows] == [
        ("q01", "m1"), ("q01", "m2"), ("q02", "m2")]
    assert result.lane_stops["m1"].startswith("daily safety stop")
    assert result.not_run == [{"question_id": "q02", "model_id": "m1",
                               "retrieval_setting": SETTING,
                               "reason": result.lane_stops["m1"]}]
    assert result.stopped_reason is None


async def test_runner_rejects_undeclared_scenarios_and_missing_lanes(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    with pytest.raises(ConfigurationError):
        EvalRunner(run=run, lanes={}, scenario="retrieval_eval", source="stand-in")
    runner = EvalRunner(run=run, lanes={}, scenario="model_comparison", source="stand-in")
    with pytest.raises(ConfigurationError):
        await runner.run(plan_items(QUESTIONS, question_ids=("q01",), models=("x",),
                                    settings=(SETTING,)))


# --- CLI safeguards ------------------------------------------------------------------


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"groq_api_key": SecretStr(FAKE_KEY), "max_real_calls": 40,
                              **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("argv", "env", "problem"),
    [
        (["--source", "real"], {}, "dry run"),
        (["--source", "real", "--live", "--max-real-calls", "5"], {}, "plan needs up to 18"),
        (["--source", "real", "--live", "--max-real-calls", "24"], {"max_real_calls": 10},
         "MAX_REAL_CALLS"),
        (["--source", "real", "--live", "--max-real-calls", "24"], {"groq_api_key": None},
         "GROQ_API_KEY"),
        (["--source", "real", "--live", "--max-real-calls", "0"], {}, "at least 1"),
    ],
)
def test_live_safeguards(argv: list[str], env: dict[str, Any], problem: str) -> None:
    args = eval_cli.build_parser("model_comparison", "").parse_args(argv)
    problems = live_problems(source=args.source, live=args.live,
                             max_real_calls=args.max_real_calls, planned_calls=18,
                             settings=settings(**env))
    assert any(problem in p for p in problems)
    assert live_problems(source="stand-in", live=False, max_real_calls=0, planned_calls=18,
                         settings=settings()) == []


async def test_dry_run_and_refusals_create_no_run(tmp_path: Path) -> None:
    lines: list[str] = []
    parser = eval_cli.build_parser("model_comparison", "")
    dry = await eval_cli.execute(parser.parse_args(["--source", "real", "--evidence-dir",
                                                    str(tmp_path)]),
                                 "model_comparison", settings=settings(), out=lines.append)
    refused = await eval_cli.execute(parser.parse_args([
        "--source", "real", "--live", "--max-real-calls", "3", "--evidence-dir", str(tmp_path)]),
        "model_comparison", settings=settings(), out=lines.append)
    assert (dry, refused) == (eval_cli.EXIT_OK, eval_cli.EXIT_REFUSED)
    assert lines[0].startswith("plan: 18 requests = 6 question x setting pairs x 3 model(s)")
    assert any(line.startswith("  would refuse: dry run") for line in lines)
    assert any(line.startswith("  refused: plan needs up to 18") for line in lines)
    assert not (tmp_path / "experiments").exists()


async def test_cli_run_counts_real_calls_and_records_the_manifest(tmp_path: Path) -> None:
    lines: list[str] = []

    def factory(run: RunHandle) -> tuple[Any, Any]:
        budget = RealCallBudget(4, on_consume=lambda: run.record_real_calls(1))
        scripted = ScriptedProvider([reply(GOOD_ANSWER)] * 4, source="real")

        class Real:
            source = "real"

            async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
                budget.consume()
                async for event in scripted.stream_chat(request):
                    yield event

            async def aclose(self) -> None:
                return None

        return Real(), lambda: budget.remaining

    args = eval_cli.build_parser("retrieval_eval", "").parse_args([
        "--model", "m1", "--source", "real", "--live", "--max-real-calls", "4",
        "--questions", "q03,q21", "--settings", f"small_fast,{SETTING}",
        "--evidence-dir", str(tmp_path / "evidence")])
    code = await eval_cli.execute(
        args, "retrieval_eval", settings=settings(), provider_factory=factory,
        retrievers={"small_fast": StubRetriever(PASSAGES), SETTING: StubRetriever(PASSAGES)},
        git_state=("abc1234", True), out=lines.append)
    assert code == eval_cli.EXIT_OK
    (run_dir,) = (tmp_path / "evidence" / "experiments").iterdir()
    manifest = load_manifest(run_dir)
    assert (manifest.kind, manifest.status, manifest.source, manifest.cache_mode) == (
        "experiment", "complete", "real", "off")
    assert manifest.scenarios == ("retrieval_eval",) and manifest.model_id == "m1"
    assert manifest.real_call_count == 4
    assert manifest.retrieval_settings == ("small_fast", SETTING)
    snapshot = manifest.config_snapshot
    assert snapshot["question_ids"] == ["q03", "q21"]
    assert snapshot["guard_limit_basis"]["rpm_limit"].startswith("local pacing cap")
    assert snapshot["prompt_budget"].startswith("none")
    assert "groq_api_key" not in json.dumps(snapshot) or "[REDACTED]" in json.dumps(snapshot)
    assert FAKE_KEY not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    for name in ("rows.csv", "traces.jsonl", "answers.jsonl", "grades.json",
                 "answers_audit.json", "summary.json"):
        assert (run_dir / name).is_file()
    rows = read_rows(run_dir / "rows.csv")
    assert len(rows) == 4 and {r.source for r in rows} == {"real"}
    assert any(line.startswith("run: ") and "real calls recorded: 4" in line for line in lines)


def test_result_rows_stay_valid_after_a_round_trip(tmp_path: Path) -> None:
    del tmp_path
    assert datetime.now(UTC).tzinfo is UTC  # rows use timezone-aware UTC start times
