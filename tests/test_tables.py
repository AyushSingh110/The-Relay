"""P11 tests: table computation from a synthetic canonical results.csv. Offline.

Synthetic runs are built with the real run store and aggregated with the real
``build_results``, so the tables are tested on exactly the inputs they will see.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from relay.experiments.tables import (
    NOT_MEASURED,
    fmt_pct,
    linear_percentile,
    load_inputs,
    markdown_table,
    percentiles,
    render,
    table2a,
    table2b,
    table3,
)
from relay.schemas import ResultRow
from relay.telemetry.aggregation import build_results
from relay.telemetry.run_store import RunHandle, create_run
from tests.factories import row_values

T0 = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "compute_tables.py"
    spec = importlib.util.spec_from_file_location("compute_tables_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Formatting ----------------------------------------------------------------------


def test_linear_percentile_matches_the_type7_definition() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert linear_percentile(values, 0.5) == 25.0
    assert linear_percentile(values, 0.95) == pytest.approx(38.5)
    assert linear_percentile(values, 0.0) == 10.0 and linear_percentile(values, 1.0) == 40.0
    assert linear_percentile([7.0], 0.99) == 7.0 and linear_percentile([], 0.5) is None
    with pytest.raises(ValueError):
        linear_percentile(values, 1.5)


def test_formatting_states_n_one_decimal_and_not_measured() -> None:
    assert percentiles([10.0, 20.0, 30.0, 40.0]) == "25.0 / 38.5 / 39.7 (N=4)"
    assert percentiles([]) == f"{NOT_MEASURED} (N=0)"
    assert fmt_pct(1, 3) == "33.3% (1/3)" and fmt_pct(0, 0) == NOT_MEASURED
    assert markdown_table(["a", "b"], [[1, 2]]).splitlines() == [
        "| a | b |", "| :-- | :-- |", "| 1 | 2 |"]


# --- Synthetic evidence --------------------------------------------------------------


def row(run: RunHandle, n: int, **overrides: Any) -> ResultRow:
    values = row_values(request_id=f"{run.run_id}-{n:06d}", run_id=run.run_id,
                        start_time=T0 + timedelta(seconds=n), **overrides)
    return ResultRow(**values)


def eval_run(root: Path) -> RunHandle:
    run = create_run(root, "experiment", scenarios=["retrieval_eval"], source="real",
                     model_id="m1", retrieval_settings=["small_fast"], cache_mode="off",
                     config_snapshot={}, git_state=("abc1234", False),
                     started_at=T0)
    rows = [
        row(run, 1, scenario="retrieval_eval", source="real", retrieval_setting="small_fast",
            question_id="q01", model_id="m1"),
        row(run, 2, scenario="retrieval_eval", source="real", retrieval_setting="small_fast",
            question_id="q02", model_id="m1", verification_verdict="BLOCKED",
            error_code="citation_verification_failed"),
        row(run, 3, scenario="retrieval_eval", source="real", retrieval_setting="small_fast",
            question_id="q16", model_id=None, provider_called=False, tokens_in=0,
            tokens_out=0, usage_source="none", provider_first_token_ms=None,
            verification_verdict="EARLY_REFUSAL"),
    ]
    for r in rows:
        run.append_row(r)
    grades = {"schema_version": 1, "run_id": run.run_id, "grades": [
        {"request_id": rows[0].request_id, "proposed": "correct", "final": None},
        {"request_id": rows[1].request_id, "proposed": "refused_wrongly", "final": None},
        {"request_id": rows[2].request_id, "proposed": "refused_correctly",
         "final": "refused_correctly"},
    ]}
    (run.run_dir / "grades.json").write_text(json.dumps(grades), encoding="utf-8")
    run.finalize()
    return run


def load_run(root: Path) -> RunHandle:
    run = create_run(root, "experiment", scenarios=["overload"], source="stand-in",
                     model_id="m1", retrieval_settings=["small_fast"], cache_mode="off",
                     config_snapshot={"scenario": {"retry_budget_enabled": False}},
                     git_state=("abc1234", False), started_at=T0 + timedelta(seconds=5))
    slow = row(run, 1, scenario="overload", total_ms=5000.0, first_token_ms=4990.0,
               server_total_ms=4980.0, waiting_ms=4000.0, working_ms=900.0,
               retrying_ms=80.0, provider_first_token_ms=4500.0, retrieval_ms=20.0)
    fast = row(run, 2, scenario="overload")
    shed = row(run, 3, scenario="overload", status="rate_limited", error_code="queue_full",
               first_token_ms=None, provider_called=False, tokens_in=0, tokens_out=0,
               usage_source="none", provider_first_token_ms=None,
               verification_verdict="NOT_RUN", answer_grade=None)
    for r in (slow, fast, shed):
        run.append_row(r)
    summary = {"wall_duration_s": 60.0, "offered_rate_per_min": 3.0,
               "capacity": {"capacity": 2, "service_rate_per_s": 0.1},
               "queue_peak_depth": 2, "standin_stats": {"requests": 3}}
    (run.run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run.run_dir / "traces.jsonl").write_text(json.dumps({
        "record": "flight", "leader_request_id": slow.request_id,
        "guard": {"tpm_available": 12, "tokens_last_minute": 7988,
                  "requests_last_minute": 9}}) + "\n", encoding="utf-8")
    run.finalize()
    return run


def build(tmp_path: Path) -> tuple[Path, Path, RunHandle, RunHandle]:
    evidence = tmp_path / "evidence"
    a, b = eval_run(evidence), load_run(evidence)
    canonical = evidence / "canonical_runs.json"
    canonical.write_text(json.dumps({"schema_version": 1, "runs": [a.run_id, b.run_id]}),
                         encoding="utf-8")
    results = tmp_path / "results.csv"
    build_results(evidence, canonical, results)
    return results, evidence, a, b


# --- Tables --------------------------------------------------------------------------


def test_table3_uses_reviewed_grades_first_and_labels_proposals(tmp_path: Path) -> None:
    results, evidence, _, _ = build(tmp_path)
    text = table3(load_inputs(results, evidence))
    line = next(x for x in text.splitlines() if x.startswith("| small_fast"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[2:4] == ["3", "2"]  # N, provider calls
    assert cells[4] == "50.0% (1/2)"  # VERIFIED / (VERIFIED + BLOCKED)
    assert cells[5] == "50.0% (1/2)"  # q01 correct, q02 refused_wrongly
    assert cells[6] == "100.0% (1/1)"  # q16, owner-reviewed
    assert cells[13] == "1"  # early refusal, no call
    assert "{'proposed': 2, 'reviewed': 1}" in text and "PROPOSED" in text


def test_table2_capacity_and_slow_request_forensics(tmp_path: Path) -> None:
    results, evidence, _, load = build(tmp_path)
    runs = load_inputs(results, evidence)
    a = table2a(runs)
    line = next(x for x in a.splitlines() if x.startswith("| overload"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[1] == "cache=off, retry budget=off" and cells[4] == "3"
    assert cells[7] == "2.0"  # 2 delivered in 60 s
    assert cells[8] == "66.7% (2/3)" and cells[9] == "33.3% (1/3)"
    assert cells[16] == "1.50"  # 3 stand-in attempts / 2 first attempts
    assert cells[17:20] == ["2", "6.0", "2"]
    b = table2b(runs, evidence)
    slow = next(x for x in b.splitlines() if x.startswith("| overload") and "000001" in x)
    cells = [c.strip() for c in slow.strip("|").split("|")]
    assert cells[4:12] == ["5000.0", "4000.0", "900.0", "80.0", "20.0", "4500.0", "480.0",
                           "20.0"]  # total, waiting, working, retrying, retrieval, before,
    #                                 after (4980 - 4500), client overhead (5000 - 4980)
    assert "tpm_available 12" in cells[14]
    assert load.run_id in b


def test_render_is_deterministic_and_cites_the_calibration(tmp_path: Path) -> None:
    results, evidence, _, _ = build(tmp_path)
    first = render(results, evidence, calibration_run="cal-x")
    assert first == render(results, evidence, calibration_run="cal-x")
    assert "calibration run `cal-x`" in first and "## Table 3" in first
    assert "(6 rows, 2 runs" in first


def test_script_writes_then_checks_byte_identical_output(tmp_path: Path) -> None:
    results, evidence, _, _ = build(tmp_path)
    script = _load_script()
    out = tmp_path / "tables.md"
    argv = ["--results", str(results), "--evidence-root", str(evidence), "--output", str(out)]
    assert script.main(argv) == 0 and out.is_file()
    assert script.main([*argv, "--check"]) == 0
    out.write_text("tampered", encoding="utf-8")
    assert script.main([*argv, "--check"]) == 1
