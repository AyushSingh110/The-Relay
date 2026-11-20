"""V2-14: replay the five v1 load scenarios against v2's POST /v1/ask (stand-in only).

  python scripts/run_v2_load.py                       # all five, each run twice
  python scripts/run_v2_load.py --scenario burst_cache_on --repeat 1

Writes runs/v2-load-<UTC>/ (never evidence/):
  manifest.json                     git state, v1 reference runs, what was run
  <scenario>/run-1/, run-2/         rows.jsonl (one V2Row per request) and summary.json
  comparison.json, comparison.md    v1 (canonical run, read from evidence/) against the
                                    LAST run of each scenario

No real model calls: the lane is the v1-calibrated stand-in on 127.0.0.1 and .env is not
read. Exit codes: 0 ok, 1 failure, 2 refused (an output path under evidence/).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.app.settings import RelaySettings
from relay.experiments import v2_load
from relay.experiments.eval_runner import load_corpus_and_questions
from relay.standin.server import load_profile

PROFILE = Path("evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(v2_load.V1_RUNS), action="append",
                        help="repeatable; default: all five")
    parser.add_argument("--repeat", type=int, default=2, choices=range(1, 6),
                        help="runs per scenario (default 2; the last is compared)")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--questions-file", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    return parser.parse_args(argv)


def _git() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=False).stdout.strip()
    return {"commit": run("rev-parse", "--short", "HEAD"),
            "dirty": bool(run("status", "--porcelain"))}


async def main_async(args: argparse.Namespace) -> int:
    try:
        runs_dir = v2_load.check_output_dir(args.runs_dir)
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    keys = args.scenario or list(v2_load.V1_RUNS)
    root = runs_dir / f"v2-load-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    root.mkdir(parents=True, exist_ok=False)
    corpus = load_corpus_and_questions(args.papers_dir, args.questions_file)
    questions = {qid: q.question for qid, q in corpus.questions.by_id().items()}
    profile = load_profile(PROFILE)
    refs = {key: v2_load.load_v1_reference(args.evidence_dir, key) for key in keys}
    manifest: dict[str, Any] = {
        "kind": "v2-load", "started_at": datetime.now(UTC).isoformat(), "git": _git(),
        "scenarios": keys, "repeat": args.repeat, "standin_profile": str(PROFILE),
        "v1_reference_runs": {k: r.run_id for k, r in refs.items()}, "real_calls": 0,
        "notes": "rows of the last run of each scenario are compared with v1",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    comparison: dict[str, Any] = {}
    for key, ref in refs.items():
        for n in range(1, args.repeat + 1):
            print(f"{key}: run {n} of {args.repeat} ({len(ref.arrivals)} requests over "
                  f"{ref.arrivals[-1].at_s:.0f} s) ...", flush=True)
            summary = await v2_load.run_scenario(ref, run_dir=root / key / f"run-{n}",
                                                 base=RelaySettings(), profile=profile,
                                                 questions=questions)
            m = summary["metrics"]
            print(f"  delivered {m['delivered']}/{m['requests']}, refused {m['refused']}, "
                  f"dropped {m['dropped']}; total p50 {m['total_ms_p50']} ms; first sentence "
                  f"p50 {m['first_sentence_ms_p50']} ms; provider calls {m['provider_calls']}"
                  f"; drained {summary['drained']}", flush=True)
        v1 = v2_load.metrics(ref.rows, provider_calls=ref.provider_calls)
        comparison[key] = {"v1_run": ref.run_id, "v2_run": summary["run_id"], "v1": v1,
                           "v2": summary["metrics"],
                           "v2_first_status_ms_p50": summary["first_status_ms_p50"],
                           "v2_first_status_ms_p95": summary["first_status_ms_p95"]}
    (root / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n",
                                          encoding="utf-8")
    md = [f"# v2 load re-validation: {root.name}\n"]
    for key, c in comparison.items():
        md += [f"## {key} (v1 {c['v1_run']}, v2 {key}/{c['v2_run']})\n",
               v2_load.comparison_table(c["v1"], c["v2"]), ""]
    (root / "comparison.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    manifest["ended_at"] = datetime.now(UTC).isoformat()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"results: {root}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
