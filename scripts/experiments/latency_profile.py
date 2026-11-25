"""F-02b: where do /v1/ask's extra ~180 ms over /ask go? Offline, stand-in only, 0 real calls.

The real composition (``compose.open_runtime``, as F-02 ran it, configured from v1's overload
run), the F-01-fixed stand-in on loopback, answer cache off. After a warm-up, 10 identical
sequential requests on each path (one at a time, so the numbers are per-request costs, not
queueing):
  ask      POST /ask              (v1 buffered path)
  v1ask    POST /v1/ask           (v2 streaming path, L4 on: as F-02 ran it)
  v1ask_l4off  POST /v1/ask       (the same, service.entailment = None)

Sub-stages are timed by wrapping the functions that implement them (wall time, summed per
request): auth, trace creation/finish, IncrementalSentenceSplitter.feed/flush,
StreamingVerifier.verify_sentence, L4 (score_sentence, and EntailmentScorer.score in its
thread), SSE framing, request-log enqueue. Server spans come from each reply's final event
(/ask) or the request log breakdown (/v1/ask).

Run: python scripts/experiments/latency_profile.py [--n 10] [--question q03] [--concurrency 1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from relay.api import auth as auth_mod
from relay.api.routes import ask_v1 as ask_v1_mod
from relay.app import compose
from relay.app import request_log as request_log_mod
from relay.app.bootstrap import V1_COLLECTION_ID
from relay.app.settings import RelaySettings, with_overrides
from relay.config import Settings
from relay.experiments import v2_load
from relay.experiments.eval_runner import load_corpus_and_questions
from relay.experiments.load_runner import send_one
from relay.experiments.streaming_load import serve_app_on_loopback, stream_one
from relay.experiments.workloads import Arrival
from relay.orchestrator import streaming as streaming_mod
from relay.orchestrator import verifier as verifier_mod
from relay.standin.server import create_standin_app, load_profile, serve_on_loopback
from relay.telemetry import tracing as tracing_mod

PROFILE = Path("evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json")
TIMES: dict[str, float] = defaultdict(float)  # stage -> ms, reset per request
COUNTS: dict[str, int] = defaultdict(int)


def _sync(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter_ns()
        try:
            return fn(*args, **kwargs)
        finally:
            TIMES[name] += (time.perf_counter_ns() - start) / 1e6
            COUNTS[name] += 1
    return wrapped


def _async(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter_ns()
        try:
            return await fn(*args, **kwargs)
        finally:
            TIMES[name] += (time.perf_counter_ns() - start) / 1e6
            COUNTS[name] += 1
    return wrapped


def instrument() -> None:
    a = auth_mod.Authenticator
    a.authenticate = _async("a_auth", a.authenticate)  # type: ignore[method-assign]
    t = tracing_mod.Tracing
    t.request = _sync("b_trace_create", t.request)  # type: ignore[method-assign]
    rt = tracing_mod.RequestTrace
    rt.finish = _sync("b_trace_finish", rt.finish)  # type: ignore[method-assign]
    rt.start = _sync("b_trace_span_start", rt.start)  # type: ignore[method-assign]
    rt.end = _sync("b_trace_span_end", rt.end)  # type: ignore[method-assign]
    s = streaming_mod.IncrementalSentenceSplitter
    s.feed = _sync("e_split_feed", s.feed)  # type: ignore[method-assign]
    s.flush = _sync("e_split_flush", s.flush)  # type: ignore[method-assign]
    v = streaming_mod.StreamingVerifier
    v.verify_sentence = _sync("f_verify_sentence", v.verify_sentence)  # type: ignore[method-assign]
    wv = verifier_mod.Verifier
    wv.verify = _sync("f_verify_whole_answer", wv.verify)  # type: ignore[method-assign]
    scored = _async("l4_score_sentence", streaming_mod.score_sentence)
    streaming_mod.score_sentence = scored  # used by SentenceStream._scored
    ask_v1_mod.score_sentence = scored  # used by the buffered /v1/ask mode
    es = verifier_mod.EntailmentScorer
    es.score = _sync("l4_model_call_in_thread", es.score)  # type: ignore[method-assign]
    ask_v1_mod.sse = _sync("g_sse_frame", ask_v1_mod.sse)
    sink = request_log_mod.RequestLogSink
    sink.append_trace = _sync("h_request_log_enqueue", sink.append_trace)  # type: ignore[method-assign]


def _reset() -> None:
    TIMES.clear()
    COUNTS.clear()


async def _breakdown(runtime: compose.Runtime, request_id: str | None) -> dict[str, Any]:
    """/v1/ask: the request log's span breakdown for ``request_id`` (written asynchronously)."""
    if request_id is None:
        return {}
    store = runtime.app.state.relay.store if hasattr(runtime.app.state.relay, "store") else None
    for _ in range(50):
        await asyncio.sleep(0.02)
        rows = await store.request_log.list_recent(limit=200) if store is not None else []
        for row in rows:
            if getattr(row, "request_id", None) == request_id and getattr(row, "breakdown", None):
                spans = row.breakdown.get("spans", [])
                return {s["name"]: round(s["duration_ms"], 3) for s in spans} | {
                    k: v for s in spans if s["name"] == "request"
                    for k, v in s["attributes"].items() if k.endswith("_ms")}
    return {}


async def one(path: str, client: httpx.AsyncClient, runtime: compose.Runtime, question: str,
              n: int, service: Any) -> dict[str, Any]:
    _reset()
    started = time.perf_counter()
    if path == "ask":
        arrival = Arrival(index=n, at_s=0.0, question_id="q", patience_s=120.0)
        r = await send_one(client, arrival, f"f02b-{path}-{n:03d}", question,
                           retrieval_setting="small_fast", deadline_ms=30_000)
        final = r.events.get("final", {})
        row = {"total_ms": r.total_ms, "first_text_ms": r.first_byte_ms,
               "status": final.get("status"), "verdict": final.get("verification_verdict"),
               "server": {k: final.get(k) for k in (
                   "waiting_ms", "working_ms", "retrying_ms", "server_total_ms",
                   "provider_first_token_ms", "retrieval_ms")},
               "tokens_in": final.get("tokens_in"), "tokens_out": final.get("tokens_out")}
    else:
        r = await stream_one(client, "q", question, collection_id=V1_COLLECTION_ID,
                             auth=v2_load.AUTH, deadline_ms=30_000)
        final = r.final or {}
        row = {"total_ms": r.total_ms, "first_text_ms": r.first_sentence_ms,
               "accepted_ms": r.first_status_ms, "status": final.get("status"),
               "verdict": final.get("verdict"), "sentences": r.sentences,
               "server": {k: final.get(k) for k in ("provider_first_token_ms",)},
               "tokens_in": final.get("tokens_in"), "tokens_out": final.get("tokens_out")}
    row["client_wall_ms"] = round((time.perf_counter() - started) * 1000, 3)
    await asyncio.sleep(0.3)  # let background work (request log, trace export) finish
    row["stages_ms"] = {k: round(v, 3) for k, v in sorted(TIMES.items())}
    row["stage_calls"] = dict(sorted(COUNTS.items()))
    return row


def _median(rows: list[dict[str, Any]], getter: Callable[[dict[str, Any]], Any]) -> float | None:
    values = [v for v in (getter(r) for r in rows) if v is not None]
    return round(statistics.median(values), 2) if values else None


GENEROUS_TPM = 1_000_000


async def main(n: int, question_id: str, concurrency: int, generous: bool) -> int:
    instrument()
    corpus = load_corpus_and_questions(Path("data/papers"), Path("eval/questions.json"))
    question = corpus.questions.by_id()[question_id].question
    ref = v2_load.load_v1_reference(Path("evidence"), "overload")
    out_dir = Path("scratch/f02b") / time.strftime("%Y%m%dT%H%M%S")
    out: dict[str, Any] = {"n": n, "question_id": question_id, "concurrency": concurrency,
                           "generous_limits_tpm": GENEROUS_TPM if generous else None,
                           "paths": {}}
    env = Settings(_env_file=None)  # type: ignore[call-arg]
    for path in ("ask", "v1ask", "v1ask_l4off"):
        run_dir = out_dir / path
        run_dir.mkdir(parents=True)
        profile = load_profile(PROFILE)
        if generous:  # profiling per-request cost: the token quota must not interfere
            profile = profile.with_overrides({"limits": {"tpm": GENEROUS_TPM}})
        async with serve_on_loopback(create_standin_app(profile)) as standin_url:
            settings = v2_load.v2_settings(RelaySettings(), ref, run_dir=run_dir,
                                           standin_url=standin_url)
            if generous:
                settings = with_overrides(settings, {"guard": {"tpm": GENEROUS_TPM}})
            async with compose.open_runtime(settings, env) as runtime:
                assert runtime.source == "stand-in" and runtime.real_call_cap is None
                service = runtime.app.state.relay
                service.cache = None  # cache off, as in the overload runs
                if path == "v1ask_l4off":
                    service.entailment = None
                l4 = service.entailment is not None
                await v2_load._warm(runtime)  # noqa: SLF001
                async with serve_app_on_loopback(runtime.app) as base_url:  # noqa: SIM117
                    async with httpx.AsyncClient(base_url=base_url, timeout=None) as client:
                        for w in range(3):  # warm-up: model sessions, connections, caches
                            await one(path, client, runtime, question, 900 + w, service)
                        rows = []
                        for batch in range(0, n, concurrency):
                            rows += await asyncio.gather(*(
                                one(path, client, runtime, question, batch + i, service)
                                for i in range(min(concurrency, n - batch))))
        all_rows, rows = rows, [r for r in rows if r["status"] == "ok"]
        stages = sorted({k for r in rows for k in r["stages_ms"]})
        summary = {
            "ok_rows": len(rows), "all_rows": len(all_rows),
            "l4_on": l4,
            "status": [r["status"] for r in rows], "verdict": [r["verdict"] for r in rows],
            "total_ms_median": _median(rows, lambda r: r["total_ms"]),
            "first_text_ms_median": _median(rows, lambda r: r["first_text_ms"]),
            "accepted_ms_median": _median(rows, lambda r: r.get("accepted_ms")),
            "provider_first_token_ms_median": _median(
                rows, lambda r: r["server"].get("provider_first_token_ms")),
            "tokens_out_median": _median(rows, lambda r: r["tokens_out"]),
            "stages_ms_median": {s: _median(rows, lambda r, s=s: r["stages_ms"].get(s, 0.0))
                                 for s in stages},
            "stage_calls_median": {s: _median(rows, lambda r, s=s: r["stage_calls"].get(s, 0))
                                   for s in stages},
        }
        if path == "ask":
            for k in ("waiting_ms", "working_ms", "server_total_ms", "retrieval_ms"):
                summary[f"server_{k}_median"] = _median(rows, lambda r, k=k: r["server"].get(k))
        out["paths"][path] = {"summary": summary, "rows": all_rows}
        print(f"\n== {path} (L4 {'on' if l4 else 'off'}) ==")
        print(json.dumps(summary, indent=1))
    (out_dir / "profile.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten: {out_dir / 'profile.json'}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--question", default="q03")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--generous-limits", action="store_true",
                   help="lift TPM on the stand-in and the guard (per-request cost only)")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.n, a.question, a.concurrency, a.generous_limits)))
