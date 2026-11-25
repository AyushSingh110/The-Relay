"""F-07: summarise the fault-injection overload suites (and F-02c as the earlier baseline).

    python scripts/experiments/fault_summary.py runs/f07-fault-r0-* runs/f07-fault-r0.3-* ...

For each suite: runs scripts/experiments/overload_aggregate.py (writes <root>/aggregate.json
and comparison.md),
then the paired v2 - v1 delivery difference per run index (both paths share a seed per
index), its 95 % t interval, the stand-in's injected / disconnect counts, and the L4 score
coverage (``verifier.l4`` spans with a score, from each v2 run's request log).
Writes runs/f07-summary-<UTC>.json and .md next to the suites.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306}

_spec = importlib.util.spec_from_file_location(
    "f02agg", Path("scripts/experiments/overload_aggregate.py"))
assert _spec is not None and _spec.loader is not None
agg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agg)


def _summary(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))


def l4_coverage(run_dir: Path) -> tuple[int, int]:
    """(sentences with an L4 span, of those with a score)."""
    store = run_dir / "store.sqlite3"
    if not store.exists():
        return (0, 0)
    spans = scored = 0
    with sqlite3.connect(store) as db:
        for (raw,) in db.execute("select breakdown from request_log where breakdown is not null"):
            for span in json.loads(raw).get("spans") or []:
                if span.get("name") == "verifier.l4":
                    spans += 1
                    scored += span.get("attributes", {}).get("l4_score") is not None
    return spans, scored


def paired(a: list[float], b: list[float]) -> dict[str, Any]:
    d = [y - x for x, y in zip(a, b, strict=True)]
    mean = statistics.fmean(d)
    if len(d) < 2:
        return {"diffs": d, "mean": mean, "ci95": None}
    half = T975[len(d) - 1] * statistics.stdev(d) / len(d) ** 0.5
    return {"diffs": d, "mean": round(mean, 2), "ci95": [round(mean - half, 2),
                                                          round(mean + half, 2)]}


def suite(root: Path) -> dict[str, Any]:
    agg.main(root)
    data = json.loads((root / "aggregate.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    n = data["runs_per_path"]
    runs = {p: [_summary(root / p / f"run-{i}") for i in range(1, n + 1)]
            for p in ("v1_ask", "v2_stream")}
    delivered = {p: [r["delivered"] for r in data["per_run"][p]] for p in runs}
    injected = {p: [s["standin_stats"].get("uncited_injected") for s in runs[p]] for p in runs}
    cov = [l4_coverage(root / "v2_stream" / f"run-{i}") for i in range(1, n + 1)]
    pick = ("delivered", "provider_calls", "cancelled_streams", "stopped_mid_stream",
            "partial_answers", "blocked", "verified", "shed_upfront", "gave_up",
            "first_sentence_ms_p50", "total_ms_p50", "prompt_tokens", "completion_tokens")
    return {
        "root": str(root), "rate": manifest.get("faults.uncited_rate"), "runs": n,
        "stats": {p: {k: data["stats"][p][k] for k in pick} for p in runs},
        "delivered_per_run": delivered,
        "paired_v2_minus_v1": paired(delivered["v1_ask"], delivered["v2_stream"]),
        "uncited_injected_per_run": injected,
        "l4_spans_scored": {"spans": sum(c[0] for c in cov), "scored": sum(c[1] for c in cov)},
        "all_drained": data["all_drained"],
    }


def _f(d: dict[str, Any]) -> str:
    return "n/a" if d["mean"] is None else f"{d['mean']} ± {d['std']}"


def main(roots: list[Path]) -> int:
    suites = [suite(r) for r in roots]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path("runs") / f"f07-summary-{stamp}"
    out.with_suffix(".json").write_text(json.dumps(suites, indent=2) + "\n", encoding="utf-8")
    md = ["# F-07: overload with injected uncited sentences", "",
          "5 paired runs per rate (v1 /ask and v2 /v1/ask share a seed per run index). "
          "Values: mean ± sample std over runs. Stand-in only, 0 real calls.", "",
          "| Rate | Path | Delivered (of 86) | Calls | Injected | Stopped mid-stream | "
          "Cancelled streams | BLOCKED delivered | Shed | First verified p50 ms |",
          "| --: | :-- | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for s in suites:
        for p in ("v1_ask", "v2_stream"):
            st = s["stats"][p]
            inj = s["uncited_injected_per_run"][p]
            md.append(f"| {s['rate']} | {p} | {_f(st['delivered'])} | {_f(st['provider_calls'])}"
                      f" | {statistics.fmean(inj) if None not in inj else 'n/a'} "
                      f"| {_f(st['stopped_mid_stream'])} | {_f(st['cancelled_streams'])} "
                      f"| {_f(st['blocked'])} | {_f(st['shed_upfront'])} "
                      f"| {_f(st['first_sentence_ms_p50'])} |")
    md += ["", "| Rate | v2 − v1 delivered, per run | Mean | 95 % CI (paired t) | "
           "L4 scored / spans |", "| --: | :-- | --: | :-- | :-- |"]
    for s in suites:
        pd, cov = s["paired_v2_minus_v1"], s["l4_spans_scored"]
        md.append(f"| {s['rate']} | {pd['diffs']} | {pd['mean']} | {pd['ci95']} "
                  f"| {cov['scored']} / {cov['spans']} |")
    out.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main([Path(p) for p in sys.argv[1:]]))
