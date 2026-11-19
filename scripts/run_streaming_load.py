"""Run the steady_streaming scenario against ``relay serve``'s app and the stand-in.

    python scripts/run_streaming_load.py [--runs-dir runs] [--rounds N] [--gap-s S]

The service is composed exactly as ``relay serve`` builds it (sqlite mode, embedded
calibrated stand-in, the v1-papers collection over the v1 index), served by uvicorn on
127.0.0.1, and driven over real HTTP streaming. No API key and no real model call. The
15 answerable v1 questions are asked once each (no cache hits, no coalescing).

Writes ``<runs-dir>/<run_id>/`` (manifest.json, results.jsonl, summary.json). Never writes
under ``evidence/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relay.app import compose
from relay.app.bootstrap import V1_COLLECTION_ID
from relay.app.settings import load_relay_settings, with_overrides
from relay.config import Settings
from relay.experiments.streaming_load import (
    STREAMING_SCENARIOS,
    run_streaming_scenario,
    serve_app_on_loopback,
    summarise_streaming,
)

V1_STEADY_TOTAL_P50_MS = 701.1  # v1 steady scenario, stand-in (reports/phase-10, run …074251Z)
MAX_COMPLETION_TOKENS = 256  # v1 answers averaged ~131 tokens out; keeps a round within TPM


def answerable_questions(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(q["id"], q["question"]) for q in data["questions"]
            if str(q["answerable"]).lower() == "true"]


async def run(args: argparse.Namespace) -> int:
    scenario = replace(STREAMING_SCENARIOS["steady_streaming"], rounds=args.rounds,
                       round_gap_s=args.gap_s, concurrency=args.concurrency)
    if args.concurrency != STREAMING_SCENARIOS["steady_streaming"].concurrency:
        scenario = replace(scenario, name=f"steady_streaming_c{args.concurrency}")  # diagnostic
    questions = answerable_questions(Path("eval/questions.json"))
    run_id = "stream-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.runs_dir / run_id
    with tempfile.TemporaryDirectory() as tmp:
        settings = with_overrides(load_relay_settings(None, environ={}), {
            "cache": {"mode": "sqlite"},
            "paths": {"store_path": ":memory:", "uploads_dir": str(Path(tmp) / "uploads"),
                      "collections_dir": str(Path(tmp) / "collections")},
            "retrieval": {"settings": ["small_fast"]},
            "guard": {"max_concurrency": scenario.concurrency},
            "queue": {"capacity": 2 * scenario.concurrency,
                      "max_active": scenario.concurrency},
        })
        lane = settings.primary_lane.model_copy(
            update={"max_completion_tokens": MAX_COMPLETION_TOKENS})
        settings = settings.model_copy(update={"lanes": (lane,)})
        env = Settings(_env_file=None)  # type: ignore[call-arg]  # no key: stand-in only
        async with compose.open_runtime(settings, env) as runtime:
            if lane.kind != "standin":
                raise SystemExit("refusing to run: the lane is not the stand-in")
            async with serve_app_on_loopback(runtime.app) as base_url, httpx.AsyncClient(
                    base_url=base_url, timeout=120) as client:
                results = await run_streaming_scenario(
                    client, scenario, questions, collection_id=V1_COLLECTION_ID,
                    auth="Bearer local-dev")
            describe = runtime.describe()
    summary = summarise_streaming(scenario, results)
    p50 = summary["first_sentence_ms"]["p50"]
    summary["v1_steady_total_p50_ms"] = V1_STEADY_TOTAL_P50_MS
    summary["target_first_sentence_p50_ms"] = V1_STEADY_TOTAL_P50_MS / 2
    summary["meets_target"] = p50 is not None and p50 < V1_STEADY_TOTAL_P50_MS / 2
    out.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "run_id": run_id, "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "scenario": asdict(scenario), "source": "stand-in", "lane": lane.model_dump(mode="json"),
        "collection_id": V1_COLLECTION_ID, "questions": [qid for qid, _ in questions],
        "max_completion_tokens": MAX_COMPLETION_TOKENS, "service": describe,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out / "results.jsonl").write_text("".join(json.dumps(asdict(r)) + "\n" for r in results),
                                       encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"run written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="steady_streaming against the stand-in")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--rounds", type=int, default=STREAMING_SCENARIOS[
        "steady_streaming"].rounds)
    parser.add_argument("--gap-s", type=float, default=STREAMING_SCENARIOS[
        "steady_streaming"].round_gap_s)
    parser.add_argument("--concurrency", type=int, default=STREAMING_SCENARIOS[
        "steady_streaming"].concurrency, help="diagnostic runs only; the scenario is 5")
    args = parser.parse_args(argv)
    if "evidence" in args.runs_dir.resolve().parts:
        parser.error("--runs-dir must not be under evidence/")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
