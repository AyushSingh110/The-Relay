"""V2-14 part 2-3: the v1 load scenarios replayed against POST /v1/ask, and the
before/after metrics. Reads v1 evidence read-only; no network beyond in-process ASGI."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from relay.app.settings import RelaySettings
from relay.experiments import v2_load
from relay.experiments.workloads import SCENARIOS, build_arrivals, load_pools

EVIDENCE = Path("evidence")


def test_every_v1_reference_is_read_from_its_canonical_run() -> None:
    refs = {key: v2_load.load_v1_reference(EVIDENCE, key) for key in v2_load.V1_RUNS}
    assert {k: (r.spec.name, r.spec.cache_mode, len(r.arrivals), r.capacity)
            for k, r in refs.items()} == {
        "steady": ("steady", "off", 19, 1),
        "burst_cache_on": ("burst", "exact", 30, 3),
        "burst_cache_off": ("burst", "off", 30, 3),
        "tpm_halved": ("tpm_halved", "off", 40, 3),
        "overload": ("overload", "off", 86, 3),
    }
    assert refs["tpm_halved"].standin_overrides == {"limits": {"tpm": 4000}}
    # the recorded schedule is the one v1's seeded generator produces
    steady = refs["steady"]
    rebuilt = build_arrivals(SCENARIOS["steady"], load_pools(Path("eval/load_questions.json")))
    assert steady.arrivals == rebuilt


def test_v1_metrics_match_the_v1_run_summary() -> None:
    ref = v2_load.load_v1_reference(EVIDENCE, "burst_cache_on")
    m = v2_load.metrics(ref.rows, provider_calls=ref.provider_calls)
    assert (m["requests"], m["delivered"], m["refused"], m["dropped"]) == (30, 29, 1, 0)
    assert (m["cache_hits"], m["coalesced"], m["provider_calls"]) == (7, 16, 6)
    assert m["delivered_pct"] == 96.7 and m["refused_pct"] == 3.3
    assert m["total_ms_p50"] == ref.summary["total_ms_delivered"]["p50"]  # 614.2
    assert m["total_ms_p95"] == ref.summary["total_ms_delivered"]["p95"]
    assert m["first_sentence_ms_p50"] is None  # v1 has no sentence events


def test_metrics_classify_delivered_refused_and_dropped() -> None:
    rows = [
        {"status": "ok", "error_code": None, "cache_status": "miss", "total_ms": 100.0,
         "first_sentence_ms": 40.0, "partial_answer": False},
        {"status": "ok", "error_code": "citation_verification_failed", "cache_status": "miss",
         "total_ms": 300.0, "first_sentence_ms": 60.0, "partial_answer": True},
        {"status": "rate_limited", "error_code": "queue_full", "cache_status": "miss",
         "total_ms": 5.0, "first_sentence_ms": None, "partial_answer": False},
        {"status": "timeout", "error_code": "client_gave_up", "cache_status": "miss",
         "total_ms": 9000.0, "first_sentence_ms": None, "partial_answer": False},
    ]
    m = v2_load.metrics(rows, provider_calls=3)
    assert (m["delivered"], m["refused"], m["dropped"], m["partial_answers"]) == (2, 1, 1, 1)
    assert (m["delivered_pct"], m["refused_pct"], m["dropped_pct"]) == (50.0, 25.0, 25.0)
    assert (m["total_ms_p50"], m["total_ms_p95"]) == (100.0, 300.0)  # delivered only
    assert (m["first_sentence_ms_p50"], m["first_sentence_ms_p95"]) == (40.0, 60.0)


def test_v2_settings_copy_the_v1_run_and_stay_in_the_run_directory(tmp_path: Path) -> None:
    ref = v2_load.load_v1_reference(EVIDENCE, "tpm_halved")
    settings = v2_load.v2_settings(RelaySettings(), ref, run_dir=tmp_path,
                                   standin_url="http://127.0.0.1:9/openai/v1")
    assert settings.cache.mode == "sqlite"
    assert (settings.guard.max_concurrency, settings.guard.tpm, settings.guard.rpd) == (2, 8000,
                                                                                      1000)
    assert (settings.queue.capacity, settings.queue.tokens_per_answer) == (3, 809.1)
    lane = settings.primary_lane
    assert (lane.kind, lane.embedded, lane.base_url, lane.max_completion_tokens) == (
        "standin", False, "http://127.0.0.1:9/openai/v1", 512)
    for path in (settings.paths.store_path, settings.paths.uploads_dir,
                 settings.paths.collections_dir):
        assert tmp_path in path.parents


def _sse_app(delay_s: float) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/ask")
    async def ask() -> StreamingResponse:
        async def body():  # type: ignore[no-untyped-def]
            await asyncio.sleep(delay_s)
            yield b'event: accepted\ndata: {"request_id": "r1", "queue_position": 0}\n\n'
            yield b'event: sentence\ndata: {"text": "A [P1].", "citations": ["P1"]}\n\n'
            yield (b'event: final\ndata: {"status": "ok", "verdict": "VERIFIED", '
                   b'"error_code": null, "cache_status": "miss", "partial": false, '
                   b'"refused": false, "provider_called": true, "tokens_in": 10, '
                   b'"tokens_out": 5, "usage_source": "provider", "model_id": "m", '
                   b'"retrieval_setting": "small_fast", "queue_depth_at_entry": 0, '
                   b'"coalesced": false, "provider_first_token_ms": 12.5}\n\n')
        return StreamingResponse(body(), media_type="text/event-stream")

    return app


async def test_a_request_records_v1_fields_and_first_status_and_sentence_times() -> None:
    arrival = v2_load.Arrival(index=0, at_s=0.0, question_id="q01", patience_s=5.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_sse_app(0.0)),
                                 base_url="http://test") as client:
        row = await v2_load.send_with_patience(client, arrival, "What?", run_id="r",
                                               scenario="steady", collection_id="v1-papers")
    assert (row.status, row.cache_status, row.verification_verdict, row.request_id) == (
        "ok", "miss", "VERIFIED", "r1")
    assert (row.provider_called, row.tokens_in, row.tokens_out, row.sentences) == (True, 10, 5, 1)
    assert row.first_status_ms is not None and row.first_sentence_ms is not None
    assert row.first_status_ms <= row.first_sentence_ms <= row.total_ms
    assert row.partial_answer is False and row.provider_first_token_ms == 12.5


async def test_a_request_past_its_patience_is_given_up_as_in_v1() -> None:
    arrival = v2_load.Arrival(index=3, at_s=1.0, question_id="q02", patience_s=0.2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_sse_app(2.0)),
                                 base_url="http://test") as client:
        row = await v2_load.send_with_patience(client, arrival, "What?", run_id="r",
                                               scenario="overload", collection_id="v1-papers")
    assert (row.status, row.error_code, row.first_sentence_ms) == ("timeout", "client_gave_up",
                                                                   None)
    # gave up at its patience (0.2 s; Windows timers are ~15 ms coarse), not at the
    # server's 2 s reply
    assert 150.0 <= row.total_ms < 1000.0 and row.arrival_index == 3


def test_the_comparison_table_marks_v2_only_rows() -> None:
    v1 = {"delivered_pct": 96.7, "refused_pct": 3.3, "dropped_pct": 0.0, "total_ms_p50": 614.2,
          "total_ms_p95": 1497.6, "provider_calls": 6, "cache_hits": 7, "coalesced": 16,
          "first_sentence_ms_p50": None, "first_sentence_ms_p95": None}
    v2 = {**v1, "delivered_pct": 100.0, "refused_pct": 0.0, "total_ms_p50": 500.0,
          "first_sentence_ms_p50": 420.0, "first_sentence_ms_p95": 800.0}
    table = v2_load.comparison_table(v1, v2)
    assert "| Requests delivered (%) | 96.7 | 100.0 | +3.3 |" in table
    assert "| Median total time (ms) | 614.2 | 500.0 | -114.2 |" in table
    assert "| First sentence p50 (ms) | V2 only | 420.0 | n/a |" in table


def test_results_never_go_to_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="evidence"):
        v2_load.check_output_dir(Path("evidence") / "x")
    assert v2_load.check_output_dir(tmp_path / "runs") == tmp_path / "runs"


async def test_each_request_carries_the_v1_scenario_deadline() -> None:
    seen: list[dict[str, object]] = []
    app = FastAPI()

    @app.post("/v1/ask")
    async def ask(body: dict[str, object]) -> StreamingResponse:
        seen.append(body)
        return StreamingResponse(iter([b'event: final\ndata: {"status": "ok"}\n\n']),
                                 media_type="text/event-stream")

    arrival = v2_load.Arrival(index=0, at_s=0.0, question_id="q01", patience_s=5.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        await v2_load.send_with_patience(client, arrival, "What?", run_id="r", scenario="s",
                                         collection_id="v1-papers", deadline_ms=30_000)
    assert seen == [{"question": "What?", "collection_id": "v1-papers", "deadline_ms": 30_000}]


def test_the_runner_names_rerun_directories_apart() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_v2_load", "scripts/run_v2_load.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.parse_args([]).prefix == "v2-load"
    args = runner.parse_args(["--prefix", "v2-load-rerun", "--scenario", "overload",
                              "--scenario", "tpm_halved", "--repeat", "1"])
    assert (args.prefix, args.scenario, args.repeat) == ("v2-load-rerun",
                                                         ["overload", "tpm_halved"], 1)
