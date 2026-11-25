"""F-07: the F-02 overload suite with injected uncited sentences (stand-in only, 0 real calls).

F-01 made every stand-in sentence carry its own tag, so F-02c measured overload with 0
verifier stops. A real model does produce failing sentences: the real streaming replay
(V2-06, 55 answered rows) had 1 stopped answer in 55. This suite sets the stand-in's
``faults.uncited_rate`` (one sentence of an answer loses its tag) and reruns the same
alternating v1 /ask vs v2 /v1/ask overload schedule.

Each run index n uses profile seed ``SEED_BASE + n`` for both paths (paired), so v1 and v2
see the same injected faults per request index, and different runs see different faults.

Run: python scripts/experiments/fault_overload.py --rate 0.02 [--repeat 5]
Writes runs/f07-fault-r<rate>-<UTC>/{manifest.json, v1_ask/run-N/, v2_stream/run-N/}.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.app.settings import RelaySettings
from relay.experiments import v2_load
from relay.experiments.eval_runner import load_corpus_and_questions
from relay.standin.server import load_profile

_spec = importlib.util.spec_from_file_location("f02", Path("scripts/experiments/overload_suite.py"))
assert _spec is not None and _spec.loader is not None
f02 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f02)

SEED_BASE = 700


async def main_async(rate: float, repeat: int) -> int:
    runs_dir = v2_load.check_output_dir(Path("runs"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = runs_dir / f"f07-fault-r{rate:g}-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    corpus = load_corpus_and_questions(Path("data/papers"), Path("eval/questions.json"))
    questions = {qid: q.question for qid, q in corpus.questions.by_id().items()}
    base_profile = load_profile(f02.PROFILE)
    ref = v2_load.load_v1_reference(Path("evidence"), f02.SCENARIO)
    manifest: dict[str, Any] = {
        "kind": "f07-fault-overload", "started_at": datetime.now(UTC).isoformat(),
        "git": f02._git(), "scenario": f02.SCENARIO, "repeat": repeat,  # noqa: SLF001
        "standin_profile": str(f02.PROFILE), "faults.uncited_rate": rate,
        "seeds": {f"run-{n}": SEED_BASE + n for n in range(1, repeat + 1)},
        "rate_basis": "V2-06 real streaming replay: 1 stopped answer in 55 answered rows",
        "v1_reference_run": ref.run_id, "real_calls": 0,
        "paths": {k: f02.PATHS[k] for k in ("v1_ask", "v2_stream")},
        "order": "alternating v1_ask, v2_stream per run index (paired seeds)",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"rate {rate}: {len(ref.arrivals)} requests, {repeat} runs per path -> {root}",
          flush=True)
    for n in range(1, repeat + 1):
        profile = base_profile.with_overrides({"seed": SEED_BASE + n,
                                               "faults": {"uncited_rate": rate}})
        for path in ("v1_ask", "v2_stream"):
            run_dir = root / path / f"run-{n}"
            if path == "v1_ask":
                summary = await f02.run_v1_scenario(ref, run_dir=run_dir, base=RelaySettings(),
                                                    profile=profile, questions=questions)
            else:
                summary = await v2_load.run_scenario(ref, run_dir=run_dir, base=RelaySettings(),
                                                     profile=profile, questions=questions)
            m, s = summary["metrics"], summary["standin_stats"]
            print(f"  {path} run {n}: delivered {m['delivered']}/{m['requests']}, calls "
                  f"{m['provider_calls']}, injected {s.get('uncited_injected')}, "
                  f"disconnects {s.get('disconnects')}, 429s {s.get('rate_limited')}",
                  flush=True)
    manifest["ended_at"] = datetime.now(UTC).isoformat()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"results: {root}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, required=True)
    parser.add_argument("--repeat", type=int, default=5)
    a = parser.parse_args()
    sys.exit(asyncio.run(main_async(a.rate, a.repeat)))
