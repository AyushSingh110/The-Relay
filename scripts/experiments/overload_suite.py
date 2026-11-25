"""F-02: the v1 overload schedule, 5 runs through v1's buffered POST /ask and 5 through v2's
streaming POST /v1/ask, on the current code with the F-01-fixed stand-in. Stand-in only,
0 real calls, nothing under evidence/ is written.

Both paths run in the same v2 composition (``compose.open_runtime``), configured from v1's
canonical overload run (evidence/experiments/exp-20261113T074816Z-c26f18c, read only): the
same arrivals (times, questions, patience), guard limits, queue capacity, tokens-per-answer
basis, 30 s deadline and cache mode (off). The only difference is the endpoint:
- v1_ask:    POST /ask, the v1 path (buffered: the whole answer is verified, then sent;
             the model stream is never cancelled by the verifier);
- v2_stream: POST /v1/ask on the v1-papers collection (per-sentence verification; a failed
             sentence cancels the model stream).
Runs alternate v1, v2, v1, v2 ... so drift in machine load hits both paths alike.

Run: python scripts/experiments/overload_suite.py [--repeat 5]
Writes runs/f02-overload-<UTC>/{manifest.json, v1_ask/run-N/, v2_stream/run-N/}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relay.app import compose
from relay.app.settings import RelaySettings, with_overrides
from relay.config import Settings
from relay.experiments import v2_load
from relay.experiments.eval_runner import load_corpus_and_questions
from relay.experiments.load_runner import send_one
from relay.experiments.streaming_load import serve_app_on_loopback
from relay.standin.server import API_PREFIX, create_standin_app, load_profile, serve_on_loopback

PROFILE = Path("evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json")
SCENARIO = "overload"


def _git() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=False).stdout.strip()
    return {"commit": run("rev-parse", "--short", "HEAD"),
            "dirty": bool(run("status", "--porcelain")),
            "dirty_files": run("status", "--porcelain").splitlines()}


# --- v1 path: POST /ask -------------------------------------------------------------------


def _v1_row(result: Any, *, run_id: str) -> dict[str, Any]:
    """One /ask request in the fields ``v2_load.metrics`` reads (plus what F-02 reports).
    The whole verified answer arrives in one event, so the time to the first verified text
    the user sees (``first_sentence_ms``) is the time of that event."""
    arrival = result.arrival
    base = {"run_id": run_id, "arrival_index": arrival.index,
            "question_id": arrival.question_id, "at_s": arrival.at_s,
            "patience_s": arrival.patience_s, "request_id": result.request_id,
            "http_status": result.http_status, "total_ms": round(result.total_ms, 3),
            "partial_answer": False, "stopped_reason": None, "sentences": None}
    final = result.events.get("final")
    if result.gave_up:
        return {**base, "status": "timeout", "error_code": "client_gave_up",
                "cache_status": "unknown", "verification_verdict": "NOT_RUN",
                "provider_called": False, "tokens_in": 0, "tokens_out": 0,
                "first_sentence_ms": None, "client_error": None}
    if final is None:
        return {**base, "status": "error", "error_code": "internal_error",
                "cache_status": "unknown", "verification_verdict": "NOT_RUN",
                "provider_called": False, "tokens_in": 0, "tokens_out": 0,
                "first_sentence_ms": None, "client_error": result.client_error}
    delivered = final.get("status") == "ok" and "answer" in result.events
    return {**base, "status": final.get("status"), "error_code": final.get("error_code"),
            "cache_status": final.get("cache_status", "unknown"),
            "verification_verdict": final.get("verification_verdict", "NOT_RUN"),
            "provider_called": bool(final.get("provider_called", False)),
            "tokens_in": int(final.get("tokens_in") or 0),
            "tokens_out": int(final.get("tokens_out") or 0),
            "first_sentence_ms": result.first_byte_ms if delivered else None,
            "client_error": result.client_error,
            "spans": {k: final.get(k) for k in ("waiting_ms", "working_ms", "retrying_ms",
                                                "server_total_ms", "provider_first_token_ms",
                                                "retrieval_ms", "queue_depth_at_entry")}}


async def _replay_v1(runtime: compose.Runtime, ref: v2_load.V1Reference, *, run_id: str,
                     questions: dict[str, str]) -> list[dict[str, Any]]:
    """``v2_load._replay`` with v1's own client: each arrival is a POST /ask."""
    loop = asyncio.get_running_loop()
    async with serve_app_on_loopback(runtime.app) as base_url:
        limits = httpx.Limits(max_connections=500, max_keepalive_connections=0)
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(None),
                                     limits=limits) as client:
            started = loop.time()
            tasks = []
            for arrival in ref.arrivals:
                delay = started + arrival.at_s - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                tasks.append(asyncio.create_task(send_one(
                    client, arrival, f"f02-{arrival.index:03d}", questions[arrival.question_id],
                    retrieval_setting=ref.spec.retrieval_setting,
                    deadline_ms=ref.spec.deadline_ms)))
            results = await asyncio.gather(*tasks)
    return sorted((_v1_row(r, run_id=run_id) for r in results),
                  key=lambda row: row["arrival_index"])


async def run_v1_scenario(ref: v2_load.V1Reference, *, run_dir: Path, base: RelaySettings,
                          profile: Any, questions: dict[str, str]) -> dict[str, Any]:
    """``v2_load.run_scenario`` (same settings, fresh stand-in, store and cache), on /ask."""
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = "/".join(run_dir.parts[-3:])
    env = Settings(_env_file=None)  # type: ignore[call-arg]  # no .env: no key, no real calls
    async with serve_on_loopback(create_standin_app(profile)) as standin_url:
        settings = v2_load.v2_settings(base, ref, run_dir=run_dir, standin_url=standin_url)
        async with compose.open_runtime(settings, env) as runtime:
            if runtime.source != "stand-in" or runtime.real_call_cap is not None:
                raise RuntimeError("F-02 only runs against the stand-in")
            if ref.spec.cache_mode == "off":
                runtime.app.state.relay.cache = None  # as v2_load does for cache-off
            await v2_load._warm(runtime)  # noqa: SLF001 - same warm-up as the v2 path
            rows = await _replay_v1(runtime, ref, run_id=run_id, questions=questions)
            drained = await v2_load._drain(runtime)  # noqa: SLF001
        stats = await v2_load._standin_stats(standin_url.removesuffix(API_PREFIX))  # noqa: SLF001
    with (run_dir / "rows.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {
        "run_id": run_id, "scenario": ref.key, "path": "v1_ask (POST /ask)",
        "v1_reference_run": ref.run_id, "spec": ref.spec.model_dump(mode="json"),
        "requests_sent": len(rows), "drained": drained, "standin_stats": stats,
        "metrics": v2_load.metrics(rows, provider_calls=int(stats["requests"])),
        "first_status_ms_p50": None, "first_status_ms_p95": None,
        "settings": settings.model_dump(mode="json"),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    return summary


# --- driver -------------------------------------------------------------------------------


NO_L4_DIR = Path("scratch/f02b_no_l4_model")  # empty: compose turns L4 off
PATHS = {"v1_ask": "POST /ask (buffered, current code)",
         "v2_stream": "POST /v1/ask (verified streaming, v1-papers)",
         "v2_stream_l4off": "POST /v1/ask with L4 off (config: no L4 model dir)",
         "v2_stream_l4off_nofair": "POST /v1/ask, L4 off, no per-tenant buckets (diagnostic)"}

_ORIGINAL_QUEUE = compose.admission_queue


def _queue_without_tenant_buckets(settings, capacity):  # diagnostic only
    from relay.control.admission import AdmissionQueue
    q = settings.queue
    return AdmissionQueue.from_derivation(
        capacity, max_active=q.max_active or settings.guard.max_concurrency,
        tenant_rate_per_s=None, tenant_burst=q.tenant_burst,
        adaptive=q.adaptive_capacity and q.capacity is None, ewma_alpha=q.ewma_alpha)


async def main_async(repeat: int, paths: list[str], prefix: str) -> int:
    runs_dir = v2_load.check_output_dir(Path("runs"))
    root = runs_dir / f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    root.mkdir(parents=True, exist_ok=False)
    corpus = load_corpus_and_questions(Path("data/papers"), Path("eval/questions.json"))
    questions = {qid: q.question for qid, q in corpus.questions.by_id().items()}
    profile = load_profile(PROFILE)
    ref = v2_load.load_v1_reference(Path("evidence"), SCENARIO)
    manifest: dict[str, Any] = {
        "kind": "f02-overload", "started_at": datetime.now(UTC).isoformat(), "git": _git(),
        "scenario": SCENARIO, "repeat": repeat, "standin_profile": str(PROFILE),
        "standin_fix": "F-01 (extractive_answer splits passages with split_sentences)",
        "v1_reference_run": ref.run_id, "real_calls": 0,
        "paths": {k: PATHS[k] for k in paths},
        "order": f"alternating {', '.join(paths)} per run index",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{SCENARIO}: {len(ref.arrivals)} requests over {ref.arrivals[-1].at_s:.0f} s, "
          f"{repeat} runs per path -> {root}", flush=True)
    for n in range(1, repeat + 1):
        for path in paths:
            run_dir = root / path / f"run-{n}"
            if path == "v1_ask":
                summary = await run_v1_scenario(ref, run_dir=run_dir, base=RelaySettings(),
                                                profile=profile, questions=questions)
            else:
                base = (with_overrides(RelaySettings(),
                                       {"verifier": {"l4_model_dir": str(NO_L4_DIR)}})
                        if path.startswith("v2_stream_l4off") else RelaySettings())
                compose.admission_queue = (_queue_without_tenant_buckets
                                           if path == "v2_stream_l4off_nofair"
                                           else _ORIGINAL_QUEUE)
                summary = await v2_load.run_scenario(ref, run_dir=run_dir, base=base,
                                                     profile=profile, questions=questions)
            m, s = summary["metrics"], summary["standin_stats"]
            print(f"  {path} run {n}: delivered {m['delivered']}/{m['requests']}, refused "
                  f"{m['refused']}, dropped {m['dropped']}; calls {m['provider_calls']}; "
                  f"disconnects {s.get('disconnects')}; 429s {s.get('rate_limited')}; "
                  f"errors {m['error_codes']}; drained {summary['drained']}", flush=True)
    manifest["ended_at"] = datetime.now(UTC).isoformat()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"results: {root}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--paths", nargs="+", choices=sorted(PATHS),
                        default=["v1_ask", "v2_stream"])
    parser.add_argument("--prefix", default="f02-overload")
    a = parser.parse_args()
    sys.exit(asyncio.run(main_async(a.repeat, a.paths, a.prefix)))
