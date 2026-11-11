"""Run-directory management and canonical results aggregation tests."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from relay.errors import (
    CanonicalRunsError,
    DuplicateRequestIdError,
    DuplicateRunSelectionError,
    IncompleteRunError,
    ResultsOverwriteRefusedError,
    RunAlreadyExistsError,
    RunClosedError,
    RunNotFoundError,
    RunStoreError,
)
from relay.schemas import ResultRow
from relay.telemetry.aggregation import build_results
from relay.telemetry.csv_codec import COLUMNS, header_line, read_rows
from relay.telemetry.run_store import (
    MANIFEST_FILE,
    ROWS_FILE,
    TRACES_FILE,
    RunHandle,
    create_run,
    load_manifest,
)
from tests.factories import FAKE_KEY, failed_values, probe_values, row_values

REPO_ROOT = Path(__file__).resolve().parents[1]
GIT = ("abc1234", False)
DAY = datetime(2026, 11, 11, tzinfo=UTC)


def _start(hour: int) -> datetime:
    return DAY + timedelta(hours=hour)


def _experiment(root: Path, hour: int = 8, **kwargs) -> RunHandle:
    params = {
        "scenarios": ("steady", "burst"),
        "source": "stand-in",
        "config_snapshot": {"provider_mode": "standin"},
        "started_at": _start(hour),
        "git_state": GIT,
    }
    params.update(kwargs)
    return create_run(root, "experiment", **params)


def _row_for(handle: RunHandle, **overrides) -> ResultRow:
    values = row_values(request_id=handle.new_request_id(), run_id=handle.run_id)
    values.update(overrides)
    return ResultRow(**values)


def _complete_experiment(root: Path, hour: int, n_rows: int = 2) -> str:
    handle = _experiment(root, hour)
    for _ in range(n_rows):
        handle.append_row(_row_for(handle))
    handle.finalize("complete", ended_at=_start(hour) + timedelta(minutes=30))
    return handle.run_id


def _write_canonical(root: Path, runs: list[str]) -> Path:
    path = root / "canonical_runs.json"
    path.write_text(json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8")
    return path


# --- Run store ----------------------------------------------------------------------


def test_run_directories_per_kind(tmp_path: Path) -> None:
    exp = _experiment(tmp_path)
    probe = create_run(
        tmp_path, "groq_probe", scenarios=("probe_baseline",), source="real",
        config_snapshot={}, started_at=_start(7), git_state=GIT,
    )
    cal = create_run(
        tmp_path, "standin_calibration", scenarios=(), config_snapshot={},
        started_at=_start(6), git_state=GIT,
    )
    assert exp.run_dir == tmp_path / "experiments" / "exp-20261111T080000Z-abc1234"
    assert probe.run_dir == tmp_path / "groq_probes" / "probe-20261111T070000Z-abc1234"
    assert cal.run_dir == tmp_path / "standin_calibration" / "cal-20261111T060000Z-abc1234"
    for handle in (exp, probe):
        assert (handle.run_dir / ROWS_FILE).read_text(encoding="utf-8") == header_line()
    assert not (cal.run_dir / ROWS_FILE).exists()
    assert load_manifest(exp.run_dir).status == "in_progress"


def test_run_directory_is_never_reused(tmp_path: Path) -> None:
    _experiment(tmp_path)
    with pytest.raises(RunAlreadyExistsError):
        _experiment(tmp_path)


def test_request_ids_are_sequential_and_run_prefixed(tmp_path: Path) -> None:
    handle = _experiment(tmp_path)
    assert handle.new_request_id() == f"{handle.run_id}-000001"
    assert handle.new_request_id() == f"{handle.run_id}-000002"


def test_append_row_guards(tmp_path: Path) -> None:
    handle = _experiment(tmp_path)
    row = _row_for(handle)
    handle.append_row(row)
    with pytest.raises(RunStoreError, match="duplicate request_id"):
        handle.append_row(row)
    with pytest.raises(RunStoreError, match="not declared"):
        handle.append_row(_row_for(handle, scenario="sustained_overload"))
    with pytest.raises(RunStoreError, match="source"):
        handle.append_row(_row_for(handle, source="real"))
    other = _experiment(tmp_path, hour=9)
    with pytest.raises(RunStoreError, match="does not match run"):
        other.append_row(_row_for(handle))
    assert read_rows(handle.run_dir / ROWS_FILE) == [row]


def test_calibration_runs_do_not_accept_rows(tmp_path: Path) -> None:
    cal = create_run(
        tmp_path, "standin_calibration", scenarios=(), config_snapshot={},
        started_at=_start(6), git_state=GIT,
    )
    with pytest.raises(RunStoreError, match="do not record result rows"):
        cal.append_row(ResultRow(**row_values()))


def test_finalize_closes_run(tmp_path: Path) -> None:
    handle = _experiment(tmp_path)
    manifest = handle.finalize("complete", ended_at=_start(9))
    assert manifest.status == "complete"
    assert load_manifest(handle.run_dir) == manifest
    with pytest.raises(RunClosedError):
        handle.append_row(_row_for(handle))
    with pytest.raises(RunClosedError):
        handle.finalize("complete", ended_at=_start(10))


def test_context_manager_marks_aborted_on_exception(tmp_path: Path) -> None:
    past = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ZeroDivisionError), _experiment(tmp_path, started_at=past) as handle:
        _ = 1 / 0
    manifest = load_manifest(handle.run_dir)
    assert manifest.status == "aborted"
    assert manifest.notes is not None and "ZeroDivisionError" in manifest.notes


def test_real_call_count_is_persisted(tmp_path: Path) -> None:
    handle = create_run(
        tmp_path, "groq_probe", scenarios=("probe_baseline",), source="real",
        config_snapshot={}, started_at=_start(7), git_state=GIT,
    )
    handle.record_real_calls()
    handle.record_real_calls(2)
    assert load_manifest(handle.run_dir).real_call_count == 3


def test_secrets_never_reach_manifest_or_traces(tmp_path: Path) -> None:
    handle = _experiment(
        tmp_path, config_snapshot={"groq_api_key": FAKE_KEY, "note": f"key={FAKE_KEY}"}
    )
    handle.append_trace(
        {"headers": {"authorization": f"Bearer {FAKE_KEY}"}, "body": "org_01abcdefgh limited"}
    )
    manifest_text = (handle.run_dir / MANIFEST_FILE).read_text(encoding="utf-8")
    trace_text = (handle.run_dir / TRACES_FILE).read_text(encoding="utf-8")
    assert FAKE_KEY not in manifest_text
    assert FAKE_KEY not in trace_text
    assert "org_01abcdefgh" not in trace_text


# --- Aggregation --------------------------------------------------------------------


def test_build_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    run_a = _complete_experiment(tmp_path, 8)
    run_b = _complete_experiment(tmp_path, 9, n_rows=3)
    out_1, out_2 = tmp_path / "r1.csv", tmp_path / "r2.csv"

    first = build_results(tmp_path, _write_canonical(tmp_path, [run_b, run_a]), out_1)
    build_results(tmp_path, _write_canonical(tmp_path, [run_a, run_b]), out_2)

    assert (first.outcome, first.run_count, first.row_count) == ("written", 2, 5)
    assert out_1.read_bytes() == out_2.read_bytes()
    rows = read_rows(out_1)
    assert [row.run_id for row in rows] == [run_a] * 2 + [run_b] * 3
    assert out_1.read_text(encoding="utf-8").splitlines()[0] == ",".join(COLUMNS)


def test_rebuild_with_same_inputs_is_unchanged(tmp_path: Path) -> None:
    canonical = _write_canonical(tmp_path, [_complete_experiment(tmp_path, 8)])
    out = tmp_path / "results.csv"
    build_results(tmp_path, canonical, out)
    assert build_results(tmp_path, canonical, out).outcome == "unchanged"


def test_existing_different_output_is_not_replaced_without_flag(tmp_path: Path) -> None:
    canonical = _write_canonical(tmp_path, [_complete_experiment(tmp_path, 8)])
    out = tmp_path / "results.csv"
    out.write_text("hand-edited\n", encoding="utf-8")
    with pytest.raises(ResultsOverwriteRefusedError):
        build_results(tmp_path, canonical, out)
    assert out.read_text(encoding="utf-8") == "hand-edited\n"
    assert build_results(tmp_path, canonical, out, replace=True).outcome == "written"


def test_check_only_writes_nothing(tmp_path: Path) -> None:
    canonical = _write_canonical(tmp_path, [_complete_experiment(tmp_path, 8)])
    out = tmp_path / "results.csv"
    summary = build_results(tmp_path, canonical, out, check_only=True)
    assert (summary.outcome, summary.would_change) == ("checked", True)
    assert not out.exists()


def test_empty_selection_gives_header_only(tmp_path: Path) -> None:
    out = tmp_path / "results.csv"
    build_results(tmp_path, _write_canonical(tmp_path, []), out)
    assert out.read_text(encoding="utf-8") == header_line()


def test_duplicate_run_selection_rejected(tmp_path: Path) -> None:
    run_a = _complete_experiment(tmp_path, 8)
    with pytest.raises(DuplicateRunSelectionError):
        build_results(tmp_path, _write_canonical(tmp_path, [run_a, run_a]), tmp_path / "o.csv")


def test_malformed_canonical_list_rejected(tmp_path: Path) -> None:
    with pytest.raises(CanonicalRunsError):
        build_results(tmp_path, _write_canonical(tmp_path, ["not-a-run"]), tmp_path / "o.csv")
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 2, "runs": []}', encoding="utf-8")
    with pytest.raises(CanonicalRunsError):
        build_results(tmp_path, bad, tmp_path / "o.csv")


def test_missing_run_rejected(tmp_path: Path) -> None:
    canonical = _write_canonical(tmp_path, ["exp-20261111T080000Z-abc1234"])
    with pytest.raises(RunNotFoundError):
        build_results(tmp_path, canonical, tmp_path / "o.csv")


def test_incomplete_and_calibration_runs_rejected(tmp_path: Path) -> None:
    in_progress = _experiment(tmp_path, 8)
    canonical = _write_canonical(tmp_path, [in_progress.run_id])
    with pytest.raises(IncompleteRunError, match="in_progress"):
        build_results(tmp_path, canonical, tmp_path / "o.csv")
    cal = create_run(
        tmp_path, "standin_calibration", scenarios=(), config_snapshot={},
        started_at=_start(6), git_state=GIT,
    )
    cal.finalize("complete", ended_at=_start(7))
    with pytest.raises(IncompleteRunError, match="no result rows"):
        build_results(tmp_path, _write_canonical(tmp_path, [cal.run_id]), tmp_path / "o.csv")


def test_duplicate_request_id_rejected(tmp_path: Path) -> None:
    run_a = _complete_experiment(tmp_path, 8, n_rows=1)
    rows_path = tmp_path / "experiments" / run_a / ROWS_FILE
    lines = rows_path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows_path.write_text("".join(lines + lines[1:]), encoding="utf-8")  # duplicate data row
    with pytest.raises(DuplicateRequestIdError):
        build_results(tmp_path, _write_canonical(tmp_path, [run_a]), tmp_path / "o.csv")


def test_row_from_another_run_rejected(tmp_path: Path) -> None:
    run_a = _complete_experiment(tmp_path, 8, n_rows=1)
    run_b = _complete_experiment(tmp_path, 9, n_rows=1)
    a_rows = (tmp_path / "experiments" / run_a / ROWS_FILE).read_text(encoding="utf-8")
    b_path = tmp_path / "experiments" / run_b / ROWS_FILE
    b_path.write_text(b_path.read_text(encoding="utf-8") + a_rows.splitlines()[1] + "\n",
                      encoding="utf-8")
    with pytest.raises(IncompleteRunError, match="has run_id"):
        build_results(tmp_path, _write_canonical(tmp_path, [run_b]), tmp_path / "o.csv")


def test_probe_rows_are_aggregated(tmp_path: Path) -> None:
    probe = create_run(
        tmp_path, "groq_probe", scenarios=("probe_baseline",), source="real",
        config_snapshot={}, started_at=_start(7), git_state=GIT,
    )
    probe.append_row(
        ResultRow(**probe_values(request_id=probe.new_request_id(), run_id=probe.run_id))
    )
    probe.append_row(
        ResultRow(
            **probe_values(
                request_id=probe.new_request_id(),
                run_id=probe.run_id,
                status="rate_limited",
                error_code="upstream_rate_limited",
                first_token_ms=None,
                provider_first_token_ms=None,
                tokens_in=0,
                tokens_out=0,
                usage_source="none",
            )
        )
    )
    probe.finalize("complete", ended_at=_start(8))
    out = tmp_path / "results.csv"
    build_results(tmp_path, _write_canonical(tmp_path, [probe.run_id]), out)
    rows = read_rows(out)
    assert [r.scenario for r in rows] == ["probe_baseline", "probe_baseline"]
    assert {r.retrieval_setting for r in rows} == {"none"}
    assert {r.answer_grade for r in rows} == {None}


def test_failed_rows_survive_aggregation(tmp_path: Path) -> None:
    handle = _experiment(tmp_path, 8)
    handle.append_row(
        ResultRow(**failed_values(request_id=handle.new_request_id(), run_id=handle.run_id))
    )
    handle.finalize("complete", ended_at=_start(9))
    out = tmp_path / "results.csv"
    build_results(tmp_path, _write_canonical(tmp_path, [handle.run_id]), out)
    assert read_rows(out)[0].status == "rate_limited"


# --- CLI -------------------------------------------------------------------------------


def _load_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "build_results.py"
    spec = importlib.util.spec_from_file_location("build_results_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()
    run_a = _complete_experiment(tmp_path, 8)
    canonical = _write_canonical(tmp_path, [run_a])
    out = tmp_path / "results.csv"
    common = [
        "--evidence-root", str(tmp_path), "--canonical", str(canonical), "--output", str(out)
    ]

    assert script.main([*common, "--check"]) == 0
    assert not out.exists()
    assert script.main(common) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["rows"] == 2

    out.write_text("different\n", encoding="utf-8")
    assert script.main(common) == 2
    assert script.main([*common, "--replace"]) == 0

    _write_canonical(tmp_path, [run_a, run_a])
    assert script.main(common) == 1
