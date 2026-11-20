"""V2-14 load re-validation: v1's P10 load scenarios replayed against v2's ``POST /v1/ask``.

Comparability with v1 is the point, so each scenario is taken from its **canonical v1 run**
in ``evidence/experiments/`` (read only) rather than re-specified:

- the recorded ``arrivals.json`` (the same times, questions and client patience);
- the ProviderGuard limits, the queue capacity and the tokens-per-answer basis;
- the stand-in profile overrides (``tpm_halved`` halves the stand-in's TPM).

The system under test is the real v2 composition (``compose.open_runtime``) in ``sqlite``
mode, answering from the ``v1-papers`` collection (the v1 small_fast index), with the v1
calibrated stand-in on 127.0.0.1. Each run starts a fresh stand-in, store and cache.

Differences that cannot be removed, reported with the results:

- ``/v1/ask`` has no ``deadline_ms``: v2 cannot shed on predicted wait or expire a deadline
  (v1 sent 30 s). The client still gives up at each arrival's patience, exactly as in v1.
- v2 has no "store without answer cache" setting; for the cache-off scenarios the harness
  detaches the answer cache from the composed service (``service.cache = None``), which is
  what ``cache.mode = off`` does in v1 (no cache, no coalescing).
- The ``v1-papers`` retriever is loaded before the clock starts (v1 loaded its retrievers
  before its runs too); this makes no provider call.

Outcomes are classified identically for v1 rows and v2 rows: **delivered** = status ``ok``
(an answer, a partial answer or a refusal reached the user); **refused** = ``rate_limited``
(the service declined: queue full, budget, upstream 429); **dropped** = ``timeout`` or
``error`` (the user gave up, or it failed). Percentiles: nearest rank (``load_runner.pct``)
over delivered requests.
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict

from relay.app import compose
from relay.app.bootstrap import V1_COLLECTION_ID
from relay.app.settings import RelaySettings, with_overrides
from relay.config import Settings
from relay.experiments.load_runner import pct
from relay.experiments.streaming_load import serve_app_on_loopback, stream_one
from relay.experiments.workloads import Arrival, ScenarioSpec
from relay.standin.profile import StandinProfile
from relay.standin.server import API_PREFIX, create_standin_app, serve_on_loopback

# The canonical v1 run of each scenario (evidence/canonical_runs.json).
V1_RUNS: Final[dict[str, str]] = {
    "steady": "exp-20261113T074251Z-c26f18c",
    "burst_cache_on": "exp-20261113T074759Z-c26f18c",
    "burst_cache_off": "exp-20261113T074807Z-c26f18c",
    "tpm_halved": "exp-20261113T080532Z-c26f18c",
    "overload": "exp-20261113T074816Z-c26f18c",
}
AUTH: Final = "Bearer relay-load-harness"  # any Bearer key is the dev tenant until V2-10
DRAIN_TIMEOUT_S: Final = 180.0
_FROZEN = ConfigDict(frozen=True, extra="forbid")

__all__ = ["V1_RUNS", "Arrival", "V1Reference", "V2Row", "check_output_dir",
           "comparison_table", "load_v1_reference", "metrics", "run_scenario",
           "send_with_patience", "v2_settings"]


# --- v1 reference (read only) ---------------------------------------------------------


@dataclass(frozen=True)
class V1Reference:
    key: str
    run_id: str
    spec: ScenarioSpec
    arrivals: list[Arrival]
    guard: dict[str, Any]
    capacity: int
    tokens_per_answer: float
    standin_overrides: dict[str, Any]
    rows: list[dict[str, Any]]
    summary: dict[str, Any]

    @property
    def provider_calls(self) -> int:
        """Calls the stand-in received (every provider HTTP call, retries included)."""
        return int(self.summary["standin_stats"]["requests"])


def load_v1_reference(evidence_dir: Path, key: str) -> V1Reference:
    run_dir = evidence_dir / "experiments" / V1_RUNS[key]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    config = manifest["config_snapshot"]
    arrivals = [Arrival.model_validate(a) for a in
                json.loads((run_dir / "arrivals.json").read_text(encoding="utf-8"))]
    with (run_dir / "rows.csv").open(encoding="utf-8", newline="") as handle:
        rows = [_v1_row(r) for r in csv.DictReader(handle)]
    return V1Reference(
        key=key, run_id=manifest["run_id"], spec=ScenarioSpec.model_validate(config["scenario"]),
        arrivals=arrivals, guard=config["guard"], capacity=int(summary["capacity"]["capacity"]),
        tokens_per_answer=float(config["tokens_per_answer"]),
        standin_overrides=(config.get("standin_profile") or {}).get("overrides") or {},
        rows=rows, summary=summary)


def _v1_row(raw: Mapping[str, str]) -> dict[str, Any]:
    return {"status": raw["status"], "error_code": raw["error_code"] or None,
            "cache_status": raw["cache_status"], "total_ms": float(raw["total_ms"]),
            "first_sentence_ms": None, "partial_answer": False}


# --- metrics (identical for v1 and v2 rows) ------------------------------------------------


def _share(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def metrics(rows: Sequence[Mapping[str, Any]], *, provider_calls: int) -> dict[str, Any]:
    total = len(rows)
    delivered = [r for r in rows if r["status"] == "ok"]
    refused = sum(r["status"] == "rate_limited" for r in rows)
    dropped = total - len(delivered) - refused
    first = [r["first_sentence_ms"] for r in delivered if r.get("first_sentence_ms") is not None]
    times = [float(r["total_ms"]) for r in delivered]
    cache = Counter(r["cache_status"] for r in rows)
    return {
        "requests": total, "delivered": len(delivered), "refused": refused, "dropped": dropped,
        "delivered_pct": _share(len(delivered), total), "refused_pct": _share(refused, total),
        "dropped_pct": _share(dropped, total),
        "total_ms_p50": pct(times, 0.5), "total_ms_p95": pct(times, 0.95),
        "provider_calls": provider_calls, "cache_hits": cache.get("exact_hit", 0),
        "coalesced": cache.get("coalesced", 0),
        "first_sentence_ms_p50": pct(first, 0.5), "first_sentence_ms_p95": pct(first, 0.95),
        "partial_answers": sum(bool(r.get("partial_answer")) for r in rows),
        "status": dict(sorted(Counter(r["status"] for r in rows).items())),
        "error_codes": dict(sorted(Counter(r["error_code"] for r in rows
                                           if r["error_code"]).items())),
    }


TABLE_ROWS: Final = (
    ("Requests delivered (%)", "delivered_pct"), ("Requests refused (%)", "refused_pct"),
    ("Requests dropped (%)", "dropped_pct"), ("Median total time (ms)", "total_ms_p50"),
    ("p95 total time (ms)", "total_ms_p95"), ("Provider calls made", "provider_calls"),
    ("Cache hits", "cache_hits"), ("Coalesced (SingleFlight)", "coalesced"),
    ("First sentence p50 (ms)", "first_sentence_ms_p50"),
    ("First sentence p95 (ms)", "first_sentence_ms_p95"),
)


def _change(old: Any, new: Any) -> str:
    if old is None or new is None:
        return "n/a"
    delta = round(float(new) - float(old), 1)
    if isinstance(old, int) and isinstance(new, int):
        return f"{int(delta):+d}"
    return f"{delta:+.1f}"


def comparison_table(v1: Mapping[str, Any], v2: Mapping[str, Any]) -> str:
    lines = ["| Metric | V1 result | V2 result | Change |", "|---|---|---|---|"]
    for label, key in TABLE_ROWS:
        old, new = v1.get(key), v2.get(key)
        shown_old = "V2 only" if key.startswith("first_sentence") else _cell(old)
        lines.append(f"| {label} | {shown_old} | {_cell(new)} | {_change(old, new)} |")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return "n/a" if value is None else str(value)


# --- v2 settings for one v1 scenario ----------------------------------------------------


def v2_settings(base: RelaySettings, ref: V1Reference, *, run_dir: Path,
                standin_url: str) -> RelaySettings:
    """The v2 service configured like the v1 run: same guard, queue capacity, model and
    completion limit; store and collections inside ``run_dir``; stand-in at ``standin_url``."""
    g = ref.guard
    return with_overrides(base, {
        "cache": {"mode": "sqlite"},
        "lanes": [{"name": "standin", "kind": "standin", "model": ref.summary["model_id"],
                   "base_url": standin_url, "embedded": False, "profile": None,
                   "max_completion_tokens": 512, "reasoning_effort": None}],
        "guard": {"max_concurrency": g["max_concurrency"], "rpm": g["rpm_limit"],
                  "tpm": g["tpm_limit"], "rpd": g["rpd_limit"], "tpd": g["tpd_limit"],
                  "daily_safety_fraction": g["daily_safety_fraction"]},
        "queue": {"capacity": ref.capacity, "tokens_per_answer": ref.tokens_per_answer,
                  "max_active": None},
        "paths": {"store_path": str(run_dir / "store.sqlite3"),
                  "uploads_dir": str(run_dir / "uploads"),
                  "collections_dir": str(run_dir / "collections")},
    })


def check_output_dir(path: Path) -> Path:
    if "evidence" in path.resolve().parts or "evidence" in path.parts:
        raise ValueError(f"{path}: v2 load results never go under evidence/ (v1 is frozen)")
    return path


# --- one request -------------------------------------------------------------------------


class V2Row(BaseModel):
    """One request as v1 recorded it (where v2 reports the field), plus the v2 timings."""

    model_config = _FROZEN

    run_id: str
    scenario: str
    arrival_index: int
    question_id: str
    at_s: float
    patience_s: float
    start_time: str
    request_id: str | None
    http_status: int | None
    status: str
    error_code: str | None
    cache_status: str
    verification_verdict: str
    provider_called: bool
    tokens_in: int
    tokens_out: int
    usage_source: str
    model_id: str | None
    retrieval_setting: str | None
    queue_depth_at_entry: int | None
    coalesced: bool
    refused: bool
    total_ms: float
    provider_first_token_ms: float | None
    first_status_ms: float | None  # the "accepted" event
    first_sentence_ms: float | None  # the first verified "sentence" event
    partial_answer: bool  # the answer stopped after some verified sentences
    sentences: int
    stopped_reason: str | None
    keepalives: int
    max_gap_ms: float | None
    client_error: str | None


async def send_with_patience(client: httpx.AsyncClient, arrival: Arrival, question: str, *,
                             run_id: str, scenario: str, collection_id: str) -> V2Row:
    """One request as a user sees it, abandoned at ``arrival.patience_s`` (as in v1)."""
    start_time = datetime.now(UTC).isoformat(timespec="milliseconds")
    started = time.perf_counter()
    common: dict[str, Any] = {"run_id": run_id, "scenario": scenario,
                              "arrival_index": arrival.index, "question_id": arrival.question_id,
                              "at_s": arrival.at_s, "patience_s": arrival.patience_s,
                              "start_time": start_time}
    try:
        result = await asyncio.wait_for(
            stream_one(client, arrival.question_id, question, collection_id=collection_id,
                       auth=AUTH), timeout=arrival.patience_s)
    except TimeoutError:
        return V2Row(**common, request_id=None, http_status=None, status="timeout",
                     error_code="client_gave_up", cache_status="unknown",
                     verification_verdict="NOT_RUN", provider_called=False, tokens_in=0,
                     tokens_out=0, usage_source="none", model_id=None, retrieval_setting=None,
                     queue_depth_at_entry=None, coalesced=False, refused=False,
                     total_ms=round((time.perf_counter() - started) * 1000, 3),
                     provider_first_token_ms=None, first_status_ms=None,
                     first_sentence_ms=None, partial_answer=False, sentences=0,
                     stopped_reason=None, keepalives=0, max_gap_ms=None, client_error=None)
    final = result.final or {}
    has_final = result.final is not None
    return V2Row(
        **common, request_id=result.request_id, http_status=result.http_status,
        status=final.get("status", "error"),
        error_code=final.get("error_code") if has_final else (result.error_code
                                                               or "internal_error"),
        cache_status=final.get("cache_status", "unknown"),
        verification_verdict=final.get("verdict", "NOT_RUN"),
        provider_called=bool(final.get("provider_called", False)),
        tokens_in=int(final.get("tokens_in", 0)), tokens_out=int(final.get("tokens_out", 0)),
        usage_source=final.get("usage_source", "none"), model_id=final.get("model_id"),
        retrieval_setting=final.get("retrieval_setting"),
        queue_depth_at_entry=final.get("queue_depth_at_entry"),
        coalesced=bool(final.get("coalesced", False)), refused=bool(final.get("refused", False)),
        total_ms=result.total_ms if result.total_ms is not None else round(
            (time.perf_counter() - started) * 1000, 3),
        provider_first_token_ms=final.get("provider_first_token_ms"),
        first_status_ms=result.first_status_ms, first_sentence_ms=result.first_sentence_ms,
        partial_answer=bool(final.get("partial", False)), sentences=result.sentences,
        stopped_reason=result.stopped, keepalives=result.keepalives,
        max_gap_ms=result.max_gap_ms,
        client_error=None if has_final else result.error_code)


# --- one scenario run ----------------------------------------------------------------------


async def _standin_stats(admin: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=admin) as client:
        return dict((await client.get("/admin/stats")).json())


async def _drain(runtime: compose.Runtime, timeout_s: float = DRAIN_TIMEOUT_S) -> bool:
    service = runtime.app.state.relay
    flights = runtime.ask_v1.flights if runtime.ask_v1 is not None else {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while flights or service.queue.active or service.queue.depth:
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def run_scenario(ref: V1Reference, *, run_dir: Path, base: RelaySettings,
                       profile: StandinProfile, questions: Mapping[str, str]) -> dict[str, Any]:
    """Run ``ref``'s arrivals once against a fresh v2 service; write rows and summary."""
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = "/".join(run_dir.parts[-3:])  # v2-load-<UTC>/<scenario>/run-<n>: unique
    standin_profile = profile.with_overrides(ref.standin_overrides) \
        if ref.standin_overrides else profile
    env = Settings(_env_file=None)  # type: ignore[call-arg]  # no .env: no key, no real calls
    async with serve_on_loopback(create_standin_app(standin_profile)) as standin_url:
        settings = v2_settings(base, ref, run_dir=run_dir, standin_url=standin_url)
        async with compose.open_runtime(settings, env) as runtime:
            if runtime.source != "stand-in" or runtime.real_call_cap is not None:
                raise RuntimeError("the v2 load harness only runs against the stand-in")
            service = runtime.app.state.relay
            if ref.spec.cache_mode == "off":
                service.cache = None  # v1 cache=off: no answer cache, no coalescing
            await _warm(runtime)
            rows = await _replay(runtime, ref, run_id=run_id, questions=questions)
            drained = await _drain(runtime)
        stats = await _standin_stats(standin_url.removesuffix(API_PREFIX))
    _write_rows(run_dir, rows)
    summary = {
        "run_id": run_id, "scenario": ref.key, "v1_reference_run": ref.run_id,
        "spec": ref.spec.model_dump(mode="json"), "requests_sent": len(rows),
        "drained": drained, "standin_stats": stats,
        "metrics": metrics([r.model_dump() for r in rows],
                           provider_calls=int(stats["requests"])),
        "first_status_ms_p50": pct([r.first_status_ms for r in rows
                                    if r.first_status_ms is not None], 0.5),
        "first_status_ms_p95": pct([r.first_status_ms for r in rows
                                    if r.first_status_ms is not None], 0.95),
        "settings": settings.model_dump(mode="json"),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    return summary


async def _warm(runtime: compose.Runtime) -> None:
    """Load the v1-papers index and embedding model (no provider call), as v1 did up front."""
    if runtime.collections is None:
        raise RuntimeError("the v2 load harness needs sqlite mode (collections)")
    resources = await runtime.collections.resolve(V1_COLLECTION_ID)
    await asyncio.to_thread(resources.retriever.retrieve, "warm-up query")


async def _replay(runtime: compose.Runtime, ref: V1Reference, *, run_id: str,
                  questions: Mapping[str, str]) -> list[V2Row]:
    sequential = {p.name for p in ref.spec.phases if p.sequential}
    loop = asyncio.get_running_loop()
    rows: list[V2Row] = []
    async with serve_app_on_loopback(runtime.app) as base_url:
        limits = httpx.Limits(max_connections=500, max_keepalive_connections=0)
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(None),
                                     limits=limits) as client:
            started = loop.time()
            tasks: list[asyncio.Task[V2Row]] = []
            for arrival in ref.arrivals:
                delay = started + arrival.at_s - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                coro = send_with_patience(client, arrival, questions[arrival.question_id],
                                          run_id=run_id, scenario=ref.key,
                                          collection_id=V1_COLLECTION_ID)
                if arrival.phase in sequential:
                    rows.append(await coro)
                else:
                    tasks.append(asyncio.create_task(coro))
            rows.extend(await asyncio.gather(*tasks))
    return sorted(rows, key=lambda r: r.arrival_index)


def _write_rows(run_dir: Path, rows: Sequence[V2Row]) -> None:
    with (run_dir / "rows.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.model_dump_json() + "\n")
