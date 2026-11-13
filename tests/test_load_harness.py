"""P10 tests: patience, workloads, the client/server join and end-to-end load runs. Offline.

End-to-end tests run the real Relay app and a fast stand-in under uvicorn on 127.0.0.1;
retrieval is a scripted stub. Every rate, limit and latency is an explicit test input.
"""

from __future__ import annotations

import importlib.util
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from relay.control.provider_guard import GuardConfig
from relay.experiments.load_runner import (
    ClientResult,
    LoadSetup,
    join_row,
    parse_sse,
    pct,
    run_load,
)
from relay.experiments.patience import PatienceModel
from relay.experiments.workloads import (
    SCENARIOS,
    Arrival,
    Phase,
    ScenarioSpec,
    build_arrivals,
    load_pools,
    offered_rate_per_min,
)
from relay.providers.http_provider import HttpOpenAIProvider
from relay.standin.profile import LatencyModel
from relay.standin.server import API_PREFIX, create_standin_app, serve_on_loopback
from relay.telemetry.aggregation import build_results
from relay.telemetry.csv_codec import read_rows
from relay.telemetry.run_store import RunHandle, create_run
from tests.test_eval_runner import QUESTIONS
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever
from tests.test_standin import MODEL, profile

RUN_ID = "exp-20261113T090000Z-abc1234"
POOLS = {"p": ("q01", "q02")}


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_load_tests.py"
    spec = importlib.util.spec_from_file_location("run_load_tests_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Patience and workloads ----------------------------------------------------------


def test_patience_is_seeded_clipped_and_has_analytic_quantiles() -> None:
    model = PatienceModel(median_s=20.0, sigma=0.6, min_s=1.0)
    a = [model.sample(random.Random(3)) for _ in range(3)]
    assert a == [model.sample(random.Random(3)) for _ in range(3)]
    assert model.quantile(0.5) == pytest.approx(20.0)
    assert model.quantile(0.1) == pytest.approx(20.0 * 2.718281828 ** (0.6 * -1.2815516), 1e-4)
    assert PatienceModel(kind="fixed", median_s=0.2, min_s=1.0).sample(random.Random()) == 1.0
    with pytest.raises(ValueError):
        model.quantile(1.0)


def test_arrivals_are_deterministic_ordered_and_follow_their_shape() -> None:
    spec = ScenarioSpec(name="t", description="", phases=(
        Phase(shape="burst", pool="p", count=5, window_s=1.0),
        Phase(name="later", shape="fixed_rate", pool="p", count=3, rate_per_min=60.0,
              start_s=10.0, draw="in_order"),
    ))
    first, again = build_arrivals(spec, POOLS), build_arrivals(spec, POOLS)
    assert first == again and [a.index for a in first] == list(range(8))
    assert [a.at_s for a in first] == sorted(a.at_s for a in first)
    burst = [a for a in first if a.phase == "main"]
    later = [a for a in first if a.phase == "later"]
    assert len(burst) == 5 and all(0 <= a.at_s <= 1.0 for a in burst)
    assert [(a.at_s, a.question_id) for a in later] == [(10.0, "q01"), (11.0, "q02"),
                                                        (12.0, "q01")]
    assert offered_rate_per_min(first) is not None
    with pytest.raises(ValueError):
        build_arrivals(spec, {"other": ("q01",)})
    with pytest.raises(ValueError):
        Phase(shape="poisson", pool="p", rate_per_min=1.0)  # no duration


def test_canonical_scenarios_build_from_the_load_question_pools() -> None:
    pools = load_pools(Path("eval/load_questions.json"))
    for name, spec in SCENARIOS.items():
        arrivals = build_arrivals(spec, pools)
        assert arrivals, name
        assert {a.question_id for a in arrivals} <= set(pools["all"])
    real = build_arrivals(SCENARIOS["real_steady"], pools)
    assert len(real) == 10 and len({a.question_id for a in real}) == 10  # distinct
    data = json.loads(Path("eval/load_questions.json").read_text(encoding="utf-8"))
    assert data["paraphrases"] == {}  # no question content was written


# --- Join ----------------------------------------------------------------------------


def arrival(index: int = 0, qid: str = "q01") -> Arrival:
    return Arrival(index=index, at_s=0.0, question_id=qid, patience_s=5.0)


def client(request_id: str, *, gave_up: bool = False, events: dict[str, Any] | None = None,
           first: float | None = None, error: str | None = None) -> ClientResult:
    return ClientResult(arrival=arrival(), request_id=request_id,
                        start_time=datetime(2026, 11, 13, 9, tzinfo=UTC), total_ms=900.0,
                        first_byte_ms=first, gave_up=gave_up, client_error=error,
                        events=events or {})


SPANS = {"server_total_ms": 800.0, "waiting_ms": 100.0, "working_ms": 700.0,
         "retrying_ms": 0.0, "provider_first_token_ms": 500.0}
FINAL = {"request_id": f"{RUN_ID}-000001", "retrieval_setting": "small_fast", "status": "ok",
         "error_code": None, "cache_status": "miss", "verification_verdict": "VERIFIED",
         "provider_called": True, "degraded": False, "retries": 0, "tokens_in": 900,
         "tokens_out": 40, "usage_source": "provider", "model_id": MODEL,
         "retrieval_ms": 30.0, "queue_depth_at_entry": 0, **SPANS}
KW: dict[str, Any] = {"run_id": RUN_ID, "scenario": "steady", "source": "stand-in",
                      "cache_mode": "exact", "retrieval_setting": "small_fast"}


def test_completed_rows_take_server_fields_from_the_final_event() -> None:
    row = join_row(client(f"{RUN_ID}-000001", events={"answer": {}, "final": FINAL},
                          first=890.0), [], **KW)
    assert (row.status, row.first_token_ms, row.total_ms, row.tokens_in) == ("ok", 890.0,
                                                                             900.0, 900)
    assert (row.server_total_ms, row.queue_depth_at_entry) == (800.0, 0)


def test_abandoned_leader_takes_spend_from_its_flight_and_follower_pays_nothing() -> None:
    leader_id, follower_id = f"{RUN_ID}-000001", f"{RUN_ID}-000002"
    snap = {**SPANS, "provider_first_token_ms": None, "cache_status": "miss",
            "retrieval_setting": "small_fast"}
    traces = [
        {"record": "request", "request_id": leader_id, "role": "leader", "abandoned": True,
         "final": snap},
        {"record": "request", "request_id": follower_id, "role": "follower",
         "abandoned": True, "final": {**snap, "cache_status": "coalesced"}},
        {"record": "flight", "leader_request_id": leader_id, "end": "completed",
         "final": FINAL},
    ]
    leader = join_row(client(leader_id, gave_up=True), traces, **KW)
    follower = join_row(client(follower_id, gave_up=True), traces, **KW)
    assert (leader.status, leader.error_code, leader.verification_verdict) == (
        "timeout", "client_gave_up", "NOT_RUN")
    assert leader.first_token_ms is None and leader.answer_grade is None
    assert (leader.provider_called, leader.tokens_in, leader.tokens_out) == (True, 900, 40)
    assert (follower.cache_status, follower.provider_called, follower.tokens_in) == (
        "coalesced", False, 0)
    assert follower.server_total_ms == 800.0


def test_requests_the_server_never_saw_or_rejected_are_still_rows() -> None:
    unseen = join_row(client(f"{RUN_ID}-000003", gave_up=True), [], **KW)
    assert unseen.server_total_ms is None and unseen.cache_status == "miss"
    assert unseen.retrieval_setting == "small_fast"
    broken = join_row(client(f"{RUN_ID}-000004", error="ConnectError"), [], **KW)
    assert (broken.status, broken.error_code) == ("error", "internal_error")


def test_parse_sse_and_percentiles() -> None:
    events = parse_sse(b'event: answer\ndata: {"text": "x"}\n\nevent: final\ndata: {"a": 1}\n\n')
    assert events == {"answer": {"text": "x"}, "final": {"a": 1}}
    assert (pct([1, 2, 3, 4], 0.5), pct([1, 2, 3, 4], 0.99), pct([], 0.5)) == (2, 4, None)


# --- End to end -----------------------------------------------------------------------


def make_run(root: Path, scenario: str) -> RunHandle:
    return create_run(root / "evidence", "experiment", scenarios=[scenario],
                      config_snapshot={}, source="stand-in", cache_mode="exact",
                      git_state=("abc1234", False))


async def run_scenario(tmp_path: Path, spec: ScenarioSpec, standin: Any) -> tuple[
        RunHandle, dict[str, Any]]:
    run = make_run(tmp_path, spec.name)
    async with serve_on_loopback(create_standin_app(standin)) as base:
        admin = base.removesuffix(API_PREFIX)

        async def stats() -> dict[str, Any]:
            async with httpx.AsyncClient(base_url=admin) as c:
                return (await c.get("/admin/stats")).json()

        provider = HttpOpenAIProvider(base_url=base, source="stand-in")
        setup = LoadSetup(
            model_id=MODEL, source="stand-in", corpus_sha256="a" * 64,
            retrievers={"small_fast": StubRetriever(PASSAGES)}, vocabulary=None,
            questions=QUESTIONS.by_id(),
            guard_config=GuardConfig(max_concurrency=2, tpm_limit=100_000),
            tokens_per_answer=1000.0, tokens_per_answer_basis="test input",
            max_completion_tokens=200, standin_stats=stats)
        try:
            summary = await run_load(spec, build_arrivals(spec, POOLS), run=run,
                                     provider=provider, setup=setup)
        finally:
            await provider.aclose()
    run.finalize()
    return run, summary


async def test_ten_request_smoke_run_joins_rows_and_aggregates(tmp_path: Path) -> None:
    spec = ScenarioSpec(name="smoke", description="", cache_mode="exact",
                        patience=PatienceModel(kind="fixed", median_s=10.0),
                        phases=(Phase(shape="fixed_rate", pool="p", count=10,
                                      rate_per_min=600.0),))
    run, summary = await run_scenario(tmp_path, spec, profile())
    rows = read_rows(run.run_dir / "rows.csv")
    assert len(rows) == 10 and {r.status for r in rows} == {"ok"}
    assert all(r.first_token_ms is not None and r.first_token_ms <= r.total_ms for r in rows)
    assert all(r.server_total_ms is not None and r.server_total_ms <= r.total_ms for r in rows)
    assert {r.cache_status for r in rows} <= {"miss", "exact_hit", "coalesced"}
    called = [r for r in rows if r.provider_called]
    assert len(called) == 2  # two distinct questions; everything else hit or coalesced
    assert summary["standin_stats"]["requests"] == 2
    assert summary["first_token_ms_delivered"]["n"] == 10 and summary["given_up"] == 0
    assert summary["capacity"]["capacity"] == 16  # floor(100000/60/1000 x 10 s)
    assert summary["drained"] and summary["relay_trace_failures"] == 0

    canonical = tmp_path / "canonical.json"
    canonical.write_text(json.dumps({"schema_version": 1, "runs": [run.run_id]}),
                         encoding="utf-8")
    built = build_results(tmp_path / "evidence", canonical, tmp_path / "results.csv",
                          check_only=True)
    assert (built.row_count, built.outcome) == (10, "checked")


async def test_abandonment_is_classified_and_spend_is_kept(tmp_path: Path) -> None:
    slow = profile(header_ms=3000.0)
    slow = slow.model_copy(update={"models": {MODEL: slow.models[MODEL].model_copy(update={
        "latency": LatencyModel(header_median_ms=3000.0, header_sigma=0.0,
                                prompt_ms_per_1k_tokens=0.0, inter_token_ms=0.2,
                                reasoning_tokens=0)})}})
    spec = ScenarioSpec(name="abandon", description="", cache_mode="exact",
                        patience=PatienceModel(kind="fixed", median_s=0.5, min_s=0.1),
                        phases=(Phase(shape="burst", pool="p", count=3, window_s=0.05),))
    run, summary = await run_scenario(tmp_path, spec, slow)
    rows = read_rows(run.run_dir / "rows.csv")
    assert {(r.status, r.error_code) for r in rows} == {("timeout", "client_gave_up")}
    assert summary["given_up"] == 3 and summary["delivered"] == 0
    assert all(r.first_token_ms is None and r.verification_verdict == "NOT_RUN" for r in rows)
    leaders = [r for r in rows if r.provider_called]
    assert leaders and all(r.usage_source == "estimated" and r.tokens_in > 0 for r in leaders)
    assert all(r.tokens_in == 0 for r in rows if not r.provider_called)
    assert summary["standin_stats"]["requests"] == len(leaders)
    records = [json.loads(line) for line in
               (run.run_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["end"] for r in records if r["record"] == "flight"} == {"cancelled"}
    assert summary["drained"]


async def test_script_refuses_real_source_except_for_real_steady_and_dry_runs(
        tmp_path: Path) -> None:
    script = _load_script()
    from tests.test_eval_runner import settings
    refused = await script.run(script.parse_args(["--scenario", "steady", "--source", "real",
                                                  "--evidence-dir", str(tmp_path)]),
                               settings=settings())
    dry = await script.run(script.parse_args(["--scenario", "real_steady", "--source", "real",
                                              "--max-real-calls", "12",
                                              "--evidence-dir", str(tmp_path)]),
                           settings=settings())
    assert (refused, dry) == (script.EXIT_REFUSED, script.EXIT_OK)
    assert not (tmp_path / "experiments").exists()
