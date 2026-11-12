"""Offline tests for probe tooling: safeguards, dry run, evidence and result rows.

The "live" paths here run end-to-end against ``httpx.MockTransport``; nothing leaves the
process. Header and body values are synthetic fixtures, not Groq measurements.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from relay.config import Settings
from relay.providers.base import RealCallBudget
from relay.providers.http_provider import HttpOpenAIProvider
from relay.providers.probing import ProbeOptions, build_plan, run_probe
from relay.telemetry.aggregation import build_results
from relay.telemetry.csv_codec import read_rows
from relay.telemetry.run_store import load_manifest
from tests.factories import FAKE_KEY

REPO_ROOT = Path(__file__).resolve().parents[1]
SSE_HEADERS = {
    "content-type": "text/event-stream",
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-remaining-requests": "990",
    "x-ratelimit-reset-requests": "2m",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "7900",
    "x-ratelimit-reset-tokens": "0.5s",
}
USAGE = {"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43}


def _load_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "probe_groq.py"
    spec = importlib.util.spec_from_file_location("probe_groq_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GROQ_API_KEY", "PROVIDER_MODE", "GROQ_MODEL", "MAX_REAL_CALLS",
                 "DAILY_SAFETY_FRACTION"):
        monkeypatch.delenv(name, raising=False)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"groq_api_key": SecretStr(FAKE_KEY), "max_real_calls": 20}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def sse_ok(headers: dict[str, str] | None = None) -> httpx.Response:
    chunks = [
        {"id": "c1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning": "hm"}}]},
        {"id": "c1", "model": "m1",
         "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]},
        {"id": "c1", "model": "m1", "choices": [], "usage": USAGE},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers=headers or SSE_HEADERS, content=body.encode())


def rate_limited() -> httpx.Response:
    message = ("Rate limit reached for model `m1` in organization `org_01abcdefgh` on "
               "requests per minute (RPM): Limit 30, Used 30, Requested 1. "
               "Please try again in 1.5s.")
    return httpx.Response(429, headers={"retry-after": "2"},
                          json={"error": {"message": message, "code": "rate_limit_exceeded"}})


class Transport:
    """Scripted MockTransport: returns the queued responses in order, counting requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.mock = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self._responses.pop(0) if self._responses else sse_ok()
        if isinstance(item, Exception):
            raise item
        return item


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str], **kwargs: Any) -> tuple[int, Any]:
    code = _load_script().main(argv, **kwargs)
    return code, json.loads(capsys.readouterr().out)


def evidence_text(run_dir: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(run_dir.iterdir()))


# --- Safeguards ----------------------------------------------------------------------


def test_default_is_a_dry_run_that_sends_nothing(clean_env, tmp_path: Path, capsys) -> None:
    transport = Transport()
    code, out = run_cli(
        ["baseline", "--model", "m1", "--max-real-calls", "5", "--evidence-root", str(tmp_path)],
        capsys, settings=settings(max_real_calls=0), transport=transport.mock,
    )
    assert code == 0
    assert out["outcome"] == "dry_run"
    assert out["planned_calls"] == 5  # 3 short + 2 prompt sizes
    assert [c["label"] for c in out["plan"]] == ["short"] * 3 + ["prompt_400w", "prompt_1600w"]
    # The CLI cap (5) fits the plan, but MAX_REAL_CALLS=0 in the environment still blocks.
    assert len(out["live_blocked_by"]) == 1
    assert "MAX_REAL_CALLS" in out["live_blocked_by"][0]
    assert transport.requests == []
    assert not any(tmp_path.iterdir())  # no evidence written
    assert FAKE_KEY not in json.dumps(out)


@pytest.mark.parametrize("live", [False, True])
def test_minute_limit_refused_without_owner_flag(clean_env, tmp_path, capsys, live) -> None:
    transport = Transport()
    argv = ["minute-limit", "--model", "m1", "--minute-limit-calls", "5",
            "--max-real-calls", "5", "--evidence-root", str(tmp_path)]
    code, out = run_cli(argv + (["--live"] if live else []), capsys,
                        settings=settings(), transport=transport.mock)
    assert code == 2
    assert out["outcome"] == "refused"
    assert any("--owner-approved-rate-limit-probe" in p for p in out["problems"])
    assert transport.requests == []
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("argv_extra", "overrides", "expected"),
    [
        (["--max-real-calls", "2"], {}, "plan needs 5 calls"),
        (["--max-real-calls", "5"], {"max_real_calls": 4}, "MAX_REAL_CALLS"),
        (["--max-real-calls", "5"], {"groq_api_key": None}, "GROQ_API_KEY"),
        ([], {}, "at least 1"),
    ],
    ids=["plan-exceeds-cap", "env-cap-lower", "no-key", "no-cap"],
)
def test_live_run_refused_by_each_safeguard(
    clean_env, tmp_path, capsys, argv_extra, overrides, expected
) -> None:
    transport = Transport()
    argv = ["baseline", "--model", "m1", "--live", "--evidence-root", str(tmp_path)]
    code, out = run_cli(argv + argv_extra, capsys, settings=settings(**overrides),
                        transport=transport.mock)
    assert code == 2
    assert any(expected in problem for problem in out["problems"])
    assert transport.requests == []


def test_baseline_option_validation(clean_env, capsys) -> None:
    code, out = run_cli(
        ["baseline", "--model", "a", "--model", "b", "--model", "c", "--model", "d"],
        capsys, settings=settings(),
    )
    assert code == 1 and out["outcome"] == "invalid_options"


# --- Live path against MockTransport ------------------------------------------------


def test_baseline_writes_rows_traces_summary_and_manifest(clean_env, tmp_path, capsys) -> None:
    transport = Transport()
    code, out = run_cli(
        ["baseline", "--model", "m1", "--calls-per-model", "2", "--prompt-words", "50",
         "--max-real-calls", "3", "--live", "--evidence-root", str(tmp_path)],
        capsys, settings=settings(), transport=transport.mock,
    )
    assert code == 0, out
    assert out["outcome"] == "completed"
    assert (out["planned_calls"], out["real_calls_sent"], out["rows_written"]) == (3, 3, 3)
    assert out["stopped_reason"] is None
    assert len(transport.requests) == 3

    run_dir = Path(out["run_dir"])
    assert run_dir.parent == tmp_path / "groq_probes"
    manifest = load_manifest(run_dir)
    assert (manifest.status, manifest.real_call_count, manifest.kind) == ("complete", 3,
                                                                          "groq_probe")
    assert manifest.scenarios == ("probe_baseline",) and manifest.model_id == "m1"

    rows = read_rows(run_dir / "rows.csv")
    assert len(rows) == 3
    for row in rows:
        assert (row.scenario, row.source, row.retrieval_setting) == ("probe_baseline", "real",
                                                                     "none")
        assert (row.status, row.usage_source, row.tokens_in, row.tokens_out) == ("ok", "provider",
                                                                                 40, 3)
        assert row.answer_grade is None and row.server_total_ms is None
        assert row.first_token_ms is not None and row.provider_first_token_ms is not None

    traces = [json.loads(line) for line in (run_dir / "traces.jsonl").read_text().splitlines()]
    assert [t["label"] for t in traces] == ["short", "short", "prompt_50w"]
    first = traces[0]
    assert first["response"]["headers"]["x-ratelimit-reset-requests"] == "2m"
    assert first["response"]["rate_limits"]["reset_requests_s"] == 120.0
    assert first["response"]["rate_limits"]["semantics"] == "documented_unverified"
    assert first["first_token_basis"] == "content"
    assert first["usage"]["prompt_tokens"] == 40 and first["reasoning_chars"] == 2

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["per_model"]["m1"]["ok"] == 3
    assert summary["per_model"]["m1"]["total_ms_ok"]["n"] == 3
    assert summary["real_calls_sent"] == 3

    payload = json.loads(transport.requests[2].content)
    assert payload["stream"] is True and payload["stream_options"] == {"include_usage": True}
    assert len(payload["messages"][1]["content"].split()) >= 50

    text = evidence_text(run_dir)
    assert FAKE_KEY not in text and "authorization" not in text.lower()


def test_minute_limit_stops_at_first_429_and_redacts_org(clean_env, tmp_path, capsys) -> None:
    transport = Transport(sse_ok(), sse_ok(), rate_limited())
    code, out = run_cli(
        ["minute-limit", "--model", "m1", "--minute-limit-calls", "10", "--max-real-calls", "10",
         "--live", "--owner-approved-rate-limit-probe", "--evidence-root", str(tmp_path)],
        capsys, settings=settings(), transport=transport.mock,
    )
    assert code == 0, out
    assert len(transport.requests) == 3  # stopped at the first 429, cap not used up
    assert "first 429" in out["stopped_reason"]
    run_dir = Path(out["run_dir"])
    rows = read_rows(run_dir / "rows.csv")
    assert [r.status for r in rows] == ["ok", "ok", "rate_limited"]
    limited = rows[2]
    assert (limited.error_code, limited.usage_source, limited.tokens_in) == (
        "upstream_rate_limited", "none", 0)
    trace = json.loads((run_dir / "traces.jsonl").read_text().splitlines()[2])
    assert trace["error"]["metric"] == "RPM" and trace["error"]["retry_after_s"] == 2.0
    payload = json.loads(transport.requests[0].content)
    assert payload["max_completion_tokens"] == 8  # tiny requests only
    text = evidence_text(run_dir)
    assert "org_01abcdefgh" not in text and "[REDACTED_ORG]" in text


def test_daily_safety_fraction_stops_the_run(clean_env, tmp_path, capsys) -> None:
    near_limit = {**SSE_HEADERS, "x-ratelimit-remaining-requests": "200"}  # 80% used
    transport = Transport(sse_ok(near_limit))
    code, out = run_cli(
        ["baseline", "--model", "m1", "--max-real-calls", "5", "--live",
         "--evidence-root", str(tmp_path)],
        capsys, settings=settings(daily_safety_fraction=0.75), transport=transport.mock,
    )
    assert code == 0
    assert len(transport.requests) == 1
    assert "daily safety fraction" in out["stopped_reason"]
    assert load_manifest(Path(out["run_dir"])).status == "complete"


def test_discover_records_models_without_rows(clean_env, tmp_path, capsys) -> None:
    listing = {"data": [{"id": "m2", "context_window": 8192}, {"id": "m1"}]}
    transport = Transport(httpx.Response(200, json=listing, headers=SSE_HEADERS))
    code, out = run_cli(
        ["discover", "--max-real-calls", "1", "--live", "--evidence-root", str(tmp_path)],
        capsys, settings=settings(), transport=transport.mock,
    )
    assert code == 0, out
    assert transport.requests[0].method == "GET"
    run_dir = Path(out["run_dir"])
    assert read_rows(run_dir / "rows.csv") == []
    trace = json.loads((run_dir / "traces.jsonl").read_text())
    assert trace["model_ids"] == ["m1", "m2"]
    assert trace["response"]["rate_limits"]["limit_requests"] == 1000


def test_failed_calls_become_valid_rows(clean_env, tmp_path, capsys) -> None:
    incomplete = httpx.Response(
        200, headers=SSE_HEADERS,
        content=b'data: {"choices": [{"index": 0, "delta": {"content": "partial"}}]}\n\n',
    )
    transport = Transport(httpx.ReadTimeout("slow"), incomplete, httpx.Response(503))
    code, out = run_cli(
        ["baseline", "--model", "m1", "--calls-per-model", "3", "--prompt-words", "",
         "--max-real-calls", "3", "--live", "--evidence-root", str(tmp_path)],
        capsys, settings=settings(), transport=transport.mock,
    )
    assert code == 0, out
    rows = read_rows(Path(out["run_dir"]) / "rows.csv")
    assert [(r.status, r.error_code) for r in rows] == [
        ("timeout", "provider_stalled"), ("error", "upstream_error"), ("error", "upstream_error")
    ]
    assert rows[0].usage_source == "none"
    # Stream opened but ended without usage: tokens are a conservative estimate.
    assert rows[1].usage_source == "estimated" and rows[1].tokens_in > 0
    assert all(r.first_token_ms is None for r in rows)


async def test_budget_stops_execution_even_without_preflight(clean_env, tmp_path) -> None:
    transport = Transport()
    options = ProbeOptions(mode="baseline", models=("m1",), calls_per_model=4, prompt_words=())
    active = settings()

    def factory(budget: RealCallBudget) -> HttpOpenAIProvider:
        return HttpOpenAIProvider.from_settings(
            active, target="groq", call_budget=budget, transport=transport.mock
        )

    outcome = await run_probe(
        options, settings=active, max_real_calls=2, evidence_root=tmp_path,
        provider_factory=factory, git_state=("abc1234", False),
    )
    assert len(build_plan(options)) == 4
    assert len(transport.requests) == 2
    assert outcome.stopped_reason == "real-call cap reached"
    assert load_manifest(Path(outcome.run_dir)).real_call_count == 2


def test_probe_rows_aggregate_into_results(clean_env, tmp_path, capsys) -> None:
    code, out = run_cli(
        ["baseline", "--model", "m1", "--calls-per-model", "1", "--prompt-words", "",
         "--max-real-calls", "1", "--live", "--evidence-root", str(tmp_path)],
        capsys, settings=settings(), transport=Transport().mock,
    )
    assert code == 0
    canonical = tmp_path / "canonical_runs.json"
    canonical.write_text(json.dumps({"schema_version": 1, "runs": [out["run_id"]]}))
    summary = build_results(tmp_path, canonical, tmp_path / "results.csv")
    assert summary.row_count == 1
    assert read_rows(tmp_path / "results.csv")[0].scenario == "probe_baseline"
