"""Load harness: drive the Relay over HTTP, measure as a client, join with server traces.

System under test: the P08 Relay app (``create_app``: admission queue, exact cache +
SingleFlight, pipeline, ProviderGuard) served by uvicorn on 127.0.0.1, talking to the
calibrated stand-in (or, for the owner-approved real run, to Groq).

Client side, per arrival:

- sends ``POST /ask`` at its scheduled time with the scenario's fixed ``deadline_ms``;
- ``first_token_ms`` = first byte of a delivered ``answer`` event (answers are buffered
  and verified, so this is also roughly the end of the response);
- gives up at its sampled patience: the request is cancelled and the connection closed,
  which the server sees as a disconnect.

After the last client finishes, the harness waits for the server to drain, then joins each
client result with the server's ``traces.jsonl`` by ``request_id``:

- completed requests take server fields from the response's ``final`` event;
- abandoned requests take spans from the server's ``request`` record; an abandoned
  *leader* takes its provider spend from the ``flight`` record it led (so token sums stay
  exact even when the work finished for coalesced followers);
- an abandoned request is ``status=timeout``, ``error_code=client_gave_up``, verdict
  ``NOT_RUN`` (nothing reached that client).
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from relay.api.app import RelayService, create_app
from relay.control.admission import AdmissionQueue, CapacityInputs, derive_capacity
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.control.retry import RetryBudget, RetryBudgetConfig, RetryConfig, RetryPolicy
from relay.eval.questions import EvalQuestion
from relay.experiments.workloads import Arrival, ScenarioSpec, offered_rate_per_min
from relay.orchestrator.pipeline import PipelineConfig
from relay.orchestrator.tools import PassageRetriever
from relay.providers.base import ChatProvider
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.schemas import CacheStatus, ResultRow, Source
from relay.standin.server import API_PREFIX, serve_on_loopback
from relay.telemetry.run_store import TRACES_FILE, RunHandle
from relay.telemetry.spans import MonotonicClock, ns_to_ms

SPAN_KEYS: Final = ("server_total_ms", "waiting_ms", "working_ms", "retrying_ms",
                    "provider_first_token_ms")
PROVIDER_KEYS: Final = ("provider_called", "retries", "tokens_in", "tokens_out",
                        "usage_source", "model_id")
# P07 Table 3, qwen/qwen3.8-27b, small_fast: mean tokens_in 1406.7 + tokens_out 106.2.
REAL_TOKENS_PER_ANSWER: Final = 1513.0
DRAIN_TIMEOUT_S: Final = 180.0


# --- Client ----------------------------------------------------------------------------


@dataclass
class ClientResult:
    arrival: Arrival
    request_id: str
    start_time: datetime
    total_ms: float
    http_status: int | None = None
    first_byte_ms: float | None = None
    gave_up: bool = False
    client_error: str | None = None
    events: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_sse(body: bytes) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for block in body.decode("utf-8", errors="replace").strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) == 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "):
            events[lines[0][7:]] = json.loads(lines[1][6:])
    return events


async def send_one(client: httpx.AsyncClient, arrival: Arrival, request_id: str,
                   question: str, *, retrieval_setting: str, deadline_ms: int,
                   clock: MonotonicClock = time.perf_counter_ns) -> ClientResult:
    """One request as a user sees it, abandoned at ``arrival.patience_s``."""
    start_time = datetime.now(UTC)
    start = clock()
    status: list[int] = []
    first: list[int] = []
    body = bytearray()

    async def go() -> None:
        payload = {"request_id": request_id, "question": question,
                   "retrieval_setting": retrieval_setting, "deadline_ms": deadline_ms}
        async with client.stream("POST", "/ask", json=payload) as response:
            status.append(response.status_code)
            async for chunk in response.aiter_bytes():
                if chunk and not first:
                    first.append(clock())
                body.extend(chunk)

    result = ClientResult(arrival=arrival, request_id=request_id, start_time=start_time,
                          total_ms=0.0)
    try:
        await asyncio.wait_for(go(), timeout=arrival.patience_s)
    except TimeoutError:
        result.gave_up = True
    except httpx.HTTPError as exc:
        result.client_error = type(exc).__name__
    result.total_ms = ns_to_ms(clock() - start)
    result.http_status = status[0] if status else None
    if not result.gave_up and result.client_error is None:
        result.events = parse_sse(bytes(body))
        if first and "answer" in result.events:
            result.first_byte_ms = ns_to_ms(first[0] - start)
    return result


# --- Join ------------------------------------------------------------------------------


def read_traces(run: RunHandle) -> list[dict[str, Any]]:
    path = run.run_dir / TRACES_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _spans(final: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: None if final is None else final.get(key) for key in SPAN_KEYS}


def _no_spend() -> dict[str, Any]:
    return {"provider_called": False, "retries": 0, "tokens_in": 0, "tokens_out": 0,
            "usage_source": "none", "model_id": None}


def join_row(result: ClientResult, traces: Sequence[Mapping[str, Any]], *, run_id: str,
             scenario: str, source: Source, cache_mode: str,
             retrieval_setting: str) -> ResultRow:
    requests = {t["request_id"]: t for t in traces if t.get("record") == "request"}
    flights = {t["leader_request_id"]: t for t in traces if t.get("record") == "flight"}
    common: dict[str, Any] = {
        "request_id": result.request_id, "scenario": scenario, "source": source,
        "start_time": result.start_time, "total_ms": result.total_ms, "answer_grade": None,
        "run_id": run_id, "question_id": result.arrival.question_id, "cost_usd": None,
    }
    final = result.events.get("final")
    if not result.gave_up and final is not None:
        delivered = final["status"] == "ok" and result.first_byte_ms is not None
        return ResultRow(
            **common, first_token_ms=result.first_byte_ms if delivered else None,
            **{k: final[k] for k in ("status", "error_code", "cache_status",
                                     "verification_verdict", "degraded", "retrieval_ms",
                                     "queue_depth_at_entry", "retrieval_setting")},
            **{k: final[k] for k in PROVIDER_KEYS}, **_spans(final),
        )
    default_cache: CacheStatus = "off" if cache_mode == "off" else "miss"
    record = requests.get(result.request_id)
    server = None if record is None else record.get("final", {})
    spend = _no_spend()
    extra: dict[str, Any] = {"retrieval_ms": None, "queue_depth_at_entry": None,
                             "degraded": False}
    if record is not None and record.get("role") == "leader":
        flight = flights.get(result.request_id)
        if flight is not None:
            ff = flight["final"]
            spend = {k: ff.get(k, default) for k, default in _no_spend().items()}
            extra.update(retrieval_ms=ff.get("retrieval_ms"),
                         queue_depth_at_entry=ff.get("queue_depth_at_entry"),
                         degraded=bool(ff.get("degraded", False)))
    spans = _spans(server)
    if spend["provider_called"] is False:
        spans["provider_first_token_ms"] = None
    if result.gave_up:
        status, error = "timeout", "client_gave_up"
    else:  # transport failure or a non-SSE reply (e.g. 422) seen by the client
        status, error = "error", "internal_error"
    return ResultRow(
        **common, first_token_ms=None, status=status, error_code=error,
        retrieval_setting=(server or {}).get("retrieval_setting") or retrieval_setting,
        cache_status=(server or {}).get("cache_status", default_cache),
        verification_verdict="NOT_RUN", **spend, **spans, **extra,
    )


# --- Summary ---------------------------------------------------------------------------


def pct(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (None for no values)."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(1, math.ceil(q * len(ordered))) - 1], 1)


def _dist(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": pct(values, 0.5), "p95": pct(values, 0.95),
            "p99": pct(values, 0.99), "max": pct(values, 1.0)}


def slowest_breakdown(rows: Sequence[ResultRow]) -> dict[str, Any] | None:
    """The slowest delivered request split into waiting / working / retrying."""
    delivered = [r for r in rows if r.status == "ok" and r.server_total_ms is not None]
    if not delivered:
        return None
    r = max(delivered, key=lambda row: row.total_ms)
    before_first = r.provider_first_token_ms
    return {
        "request_id": r.request_id, "question_id": r.question_id, "total_ms": r.total_ms,
        "first_token_ms": r.first_token_ms, "server_total_ms": r.server_total_ms,
        "waiting_ms": r.waiting_ms, "working_ms": r.working_ms, "retrying_ms": r.retrying_ms,
        "retrieval_ms": r.retrieval_ms, "provider_first_token_ms": before_first,
        "cache_status": r.cache_status, "retries": r.retries,
        "client_overhead_ms": round(r.total_ms - (r.server_total_ms or 0.0), 3),
        "note": "first_token_ms is the client's first delivered byte; answers are buffered "
                "until verified, so all server time precedes it",
    }


def summarise(spec: ScenarioSpec, arrivals: Sequence[Arrival], rows: Sequence[ResultRow], *,
              wall_s: float, extra: Mapping[str, Any]) -> dict[str, Any]:
    ok = [r for r in rows if r.status == "ok"]
    phases = sorted({a.phase for a in arrivals})
    by_phase = {}
    for phase in phases:
        ids = {a.index for a in arrivals if a.phase == phase}
        rs = [r for r, a in zip(rows, arrivals, strict=True) if a.index in ids]
        by_phase[phase] = {"requests": len(rs),
                           "status": dict(Counter(r.status for r in rs)),
                           "error_codes": dict(Counter(r.error_code for r in rs if r.error_code)),
                           "cache_status": dict(Counter(r.cache_status for r in rs)),
                           "provider_calls_rows": sum(r.provider_called for r in rs)}
    called = [r for r in rows if r.provider_called]
    return {
        "scenario": spec.name, "description": spec.description,
        "requests_sent": len(rows), "offered_rate_per_min": offered_rate_per_min(list(arrivals)),
        "schedule_span_s": round(arrivals[-1].at_s - arrivals[0].at_s, 3) if arrivals else 0,
        "wall_duration_s": round(wall_s, 3),
        "status": dict(sorted(Counter(r.status for r in rows).items())),
        "error_codes": dict(sorted(Counter(r.error_code for r in rows if r.error_code).items())),
        "given_up": sum(r.error_code == "client_gave_up" for r in rows),
        "delivered": len(ok),
        "first_token_ms_delivered": _dist([r.first_token_ms for r in ok
                                           if r.first_token_ms is not None]),
        "total_ms_delivered": _dist([r.total_ms for r in ok]),
        "total_ms_all": _dist([r.total_ms for r in rows]),
        "cache_status": dict(sorted(Counter(r.cache_status for r in rows).items())),
        "verdicts": dict(sorted(Counter(r.verification_verdict for r in rows).items())),
        "rows_with_provider_call": len(called), "retries_total": sum(r.retries for r in rows),
        "tokens_in_total": sum(r.tokens_in for r in rows),
        "tokens_out_total": sum(r.tokens_out for r in rows),
        "tokens_per_called_row": None if not called else round(
            statistics.fmean(r.tokens_in + r.tokens_out for r in called), 1),
        "usage_sources": dict(sorted(Counter(r.usage_source for r in called).items())),
        "mean_spans_ms_delivered": None if not ok else {
            k: round(statistics.fmean(getattr(r, k) or 0.0 for r in ok), 1)
            for k in ("waiting_ms", "working_ms", "retrying_ms")},
        "slowest_delivered": slowest_breakdown(rows),
        "by_phase": by_phase,
        "percentile_method": "nearest rank; p95/p99 equal the maximum when n < 20 / n < 100",
        **extra,
    }


# --- Harness ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadSetup:
    model_id: str
    source: Source
    corpus_sha256: str
    retrievers: Mapping[str, PassageRetriever]
    vocabulary: frozenset[str] | None
    questions: Mapping[str, EvalQuestion]
    guard_config: GuardConfig
    tokens_per_answer: float
    tokens_per_answer_basis: str
    max_completion_tokens: int = 512
    standin_stats: Callable[[], Awaitable[dict[str, Any]]] | None = None
    real_calls: Callable[[], int] | None = None


def capacity_for(spec: ScenarioSpec, setup: LoadSetup) -> Any:
    """Little's law: capacity = floor(service_rate x patience median); rate from TPM/RPM."""
    config = setup.guard_config
    return derive_capacity(CapacityInputs(
        patience_quantile_s=spec.patience.quantile(0.5),
        tpm=None if config.tpm_limit is None else float(config.tpm_limit),
        rpm=None if config.rpm_limit is None else float(config.rpm_limit),
        tokens_per_answer=setup.tokens_per_answer,
    ))


async def _drain(service: RelayService, timeout_s: float = DRAIN_TIMEOUT_S) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while len(service.flights) or service.queue.active or service.queue.depth:
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)  # let cancelled flights write their trace
    return True


async def run_load(spec: ScenarioSpec, arrivals: Sequence[Arrival], *, run: RunHandle,
                   provider: ChatProvider, setup: LoadSetup) -> dict[str, Any]:
    """Run one scenario end to end; append joined rows to ``run``; return the summary."""
    derivation = capacity_for(spec, setup)
    queue = AdmissionQueue.from_derivation(derivation,
                                           max_active=setup.guard_config.max_concurrency)
    guard = ProviderGuard(setup.guard_config)
    retry = RetryPolicy(RetryConfig(),
                        RetryBudget(RetryBudgetConfig(enabled=spec.retry_budget_enabled)),
                        rng=random.Random(f"retry:{spec.seed}"))
    setting = spec.retrieval_setting
    app = create_app(
        provider=provider, retrievers=setup.retrievers,
        top_k={name: RETRIEVAL_SETTINGS[name].top_k for name in setup.retrievers},
        pipeline_config=PipelineConfig(model_id=setup.model_id,
                                       max_completion_tokens=setup.max_completion_tokens),
        guard=guard, queue=queue, corpus_sha256=setup.corpus_sha256,
        cache_mode=spec.cache_mode, retry=retry, vocabulary=setup.vocabulary, run=run,
    )
    service: RelayService = app.state.relay
    sequential = {p.name for p in spec.phases if p.sequential}
    loop = asyncio.get_running_loop()
    started = loop.time()
    results: list[ClientResult] = []
    async with serve_on_loopback(app) as base:
        root = base.removesuffix(API_PREFIX)
        limits = httpx.Limits(max_connections=500, max_keepalive_connections=0)
        async with httpx.AsyncClient(base_url=root, timeout=httpx.Timeout(None),
                                     limits=limits) as client:
            tasks: list[asyncio.Task[ClientResult]] = []
            for arrival in arrivals:
                delay = started + arrival.at_s - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                coro = send_one(client, arrival, run.new_request_id(),
                                setup.questions[arrival.question_id].question,
                                retrieval_setting=setting, deadline_ms=spec.deadline_ms)
                if arrival.phase in sequential:
                    results.append(await coro)
                else:
                    tasks.append(asyncio.create_task(coro))
            results.extend(await asyncio.gather(*tasks))
        drained = await _drain(service)
    wall_s = loop.time() - started
    results.sort(key=lambda r: r.arrival.index)
    traces = read_traces(run)
    rows = [join_row(r, traces, run_id=run.run_id, scenario=spec.name, source=setup.source,
                     cache_mode=spec.cache_mode, retrieval_setting=setting) for r in results]
    for row in rows:
        run.append_row(row)
    extra: dict[str, Any] = {
        "drained": drained, "model_id": setup.model_id, "source": setup.source,
        "cache_mode": spec.cache_mode, "retrieval_setting": setting,
        "deadline_ms": spec.deadline_ms, "patience": spec.patience.model_dump(),
        "retry_budget_enabled": spec.retry_budget_enabled,
        "capacity": derivation.model_dump(),
        "tokens_per_answer_basis": setup.tokens_per_answer_basis,
        "queue_peak_depth": queue.peak_depth,
        "guard_final": guard.snapshot().model_dump(mode="json"),
        "server_trace_records": dict(Counter(t.get("record") for t in traces)),
        "client_errors": dict(Counter(r.client_error for r in results if r.client_error)),
        "relay_trace_failures": service.trace_failures,
    }
    if setup.standin_stats is not None:
        extra["standin_stats"] = await setup.standin_stats()
    if setup.real_calls is not None:
        extra["real_calls"] = setup.real_calls()
    return summarise(spec, arrivals, rows, wall_s=wall_s, extra=extra)


__all__ = ["REAL_TOKENS_PER_ANSWER", "ClientResult", "LoadSetup", "capacity_for", "join_row",
           "parse_sse", "pct", "read_traces", "run_load", "send_one", "slowest_breakdown",
           "summarise"]
