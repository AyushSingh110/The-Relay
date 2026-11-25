"""F-02: aggregate runs/f02-overload-<UTC>/ (5 x v1_ask, 5 x v2_stream) and compare with the
old (pre-F-01 stand-in) overload runs. Writes <root>/aggregate.json and <root>/comparison.md.

Run: python scripts/experiments/overload_aggregate.py runs/f02-overload-<UTC>
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from relay.experiments import v2_load

OLD_V1 = Path("evidence/experiments/exp-20261113T074816Z-c26f18c")  # read only
OLD_V2 = Path("runs/v2-load-rerun-20261120T084043Z/overload/run-1")
SHED = ("queue_full", "predicted_wait_exceeds_deadline", "deadline_expired")


def per_run(rows: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, Any]:
    m = v2_load.metrics(rows, provider_calls=int(stats["requests"]))
    codes = Counter(r["error_code"] for r in rows if r.get("error_code"))
    delivered = [r for r in rows if r["status"] == "ok"]
    verdicts = Counter(r["verification_verdict"] for r in delivered)
    return {
        "delivered": m["delivered"], "delivered_pct": m["delivered_pct"],
        "refused": m["refused"], "refused_pct": m["refused_pct"],
        "dropped": m["dropped"], "dropped_pct": m["dropped_pct"],
        "gave_up": codes.get("client_gave_up", 0),
        "shed_upfront": sum(codes.get(c, 0) for c in SHED),
        "queue_full": codes.get("queue_full", 0),
        "predicted_wait_exceeds_deadline": codes.get("predicted_wait_exceeds_deadline", 0),
        "deadline_expired": codes.get("deadline_expired", 0),
        "provider_calls": m["provider_calls"],
        "cancelled_streams": int(stats.get("disconnects", 0)),
        "completed_streams": int(stats.get("completed_streams", 0)),
        "provider_429s": sum((stats.get("rate_limited") or {}).values()),
        "stopped_mid_stream": sum(bool(r.get("stopped_reason")) for r in rows),
        "partial_answers": m["partial_answers"],
        "verified": verdicts.get("VERIFIED", 0), "blocked": verdicts.get("BLOCKED", 0),
        "early_refusal": verdicts.get("EARLY_REFUSAL", 0),
        "prompt_tokens": int(stats.get("prompt_tokens", 0)),
        "completion_tokens": int(stats.get("completion_tokens", 0)),
        "total_ms_p50": m["total_ms_p50"], "total_ms_p95": m["total_ms_p95"],
        "first_sentence_ms_p50": m["first_sentence_ms_p50"],
        "first_sentence_ms_p95": m["first_sentence_ms_p95"],
        "error_codes": dict(sorted(codes.items())),
    }


def load_path(root: Path, path: str) -> list[dict[str, Any]]:
    runs = []
    for run_dir in sorted((root / path).glob("run-*"), key=lambda p: int(p.name[4:])):
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (run_dir / "rows.jsonl").open(encoding="utf-8")]
        runs.append({"run": run_dir.name, "drained": summary["drained"], "rows": rows,
                     **per_run(rows, summary["standin_stats"])})
    return runs


def old_runs() -> dict[str, dict[str, Any]]:
    with (OLD_V1 / "rows.csv").open(encoding="utf-8", newline="") as handle:
        v1_rows = [dict(r) for r in csv.DictReader(handle)]
    v1_summary = json.loads((OLD_V1 / "summary.json").read_text(encoding="utf-8"))
    for r in v1_rows:
        r["total_ms"] = float(r["total_ms"])
        r["first_sentence_ms"] = (float(r["first_token_ms"]) if r.get("first_token_ms")
                                  else None)
        r["error_code"] = r["error_code"] or None
        r["partial_answer"] = False
        r["stopped_reason"] = None
    v2_summary = json.loads((OLD_V2 / "summary.json").read_text(encoding="utf-8"))
    v2_rows = [json.loads(line) for line in (OLD_V2 / "rows.jsonl").open(encoding="utf-8")]
    return {"v1_old": per_run(v1_rows, v1_summary["standin_stats"]),
            "v2_old": per_run(v2_rows, v2_summary["standin_stats"])}


def describe(values: list[float]) -> dict[str, Any]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"mean": None, "std": None, "min": None, "max": None, "n": 0}
    return {"mean": round(statistics.fmean(clean), 1),
            "std": round(statistics.stdev(clean), 1) if len(clean) > 1 else 0.0,
            "min": min(clean), "max": max(clean), "n": len(clean)}


KEYS = ("delivered", "delivered_pct", "refused", "refused_pct", "dropped", "dropped_pct",
        "gave_up", "shed_upfront", "queue_full", "predicted_wait_exceeds_deadline",
        "deadline_expired", "provider_calls", "cancelled_streams", "completed_streams",
        "provider_429s", "stopped_mid_stream", "partial_answers", "verified", "blocked",
        "early_refusal", "prompt_tokens", "completion_tokens", "total_ms_p50", "total_ms_p95",
        "first_sentence_ms_p50", "first_sentence_ms_p95")


def per_arrival(runs: list[dict[str, Any]]) -> list[int]:
    """How many of the runs delivered each arrival (index -> count)."""
    counts = [0] * len(runs[0]["rows"])
    for run in runs:
        for row in run["rows"]:
            counts[row["arrival_index"]] += row["status"] == "ok"
    return counts


def _fmt(d: dict[str, Any]) -> str:
    if d["mean"] is None:
        return "n/a"
    return f"{d['mean']} ± {d['std']} [{d['min']}–{d['max']}]"


def main(root: Path) -> int:
    paths = {p: load_path(root, p) for p in ("v1_ask", "v2_stream")}
    stats = {p: {k: describe([r[k] for r in runs]) for k in KEYS} for p, runs in paths.items()}
    old = old_runs()
    arrivals = {p: per_arrival(runs) for p, runs in paths.items()}
    n = len(paths["v1_ask"])
    diff = [{"arrival": i, "question_id": paths["v1_ask"][0]["rows"][i]["question_id"],
             "v1_delivered_runs": a, "v2_delivered_runs": b}
            for i, (a, b) in enumerate(zip(arrivals["v1_ask"], arrivals["v2_stream"], strict=True))
            if a != b]
    out = {"root": str(root), "runs_per_path": n,
           "per_run": {p: [{k: v for k, v in r.items() if k != "rows"} for r in runs]
                       for p, runs in paths.items()},
           "stats": stats, "old": old, "per_arrival_differences": diff,
           "all_drained": all(r["drained"] for runs in paths.values() for r in runs)}
    (root / "aggregate.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    labels = (("Delivered (n of 86)", "delivered"), ("Delivered (%)", "delivered_pct"),
              ("Refused (%)", "refused_pct"), ("Dropped (%)", "dropped_pct"),
              ("Gave up (n)", "gave_up"), ("Shed up front (n)", "shed_upfront"),
              ("  queue full", "queue_full"),
              ("  predicted wait > deadline", "predicted_wait_exceeds_deadline"),
              ("  deadline expired in queue", "deadline_expired"),
              ("Provider calls", "provider_calls"),
              ("Cancelled streams (stand-in disconnects)", "cancelled_streams"),
              ("Stopped mid-stream (v2)", "stopped_mid_stream"),
              ("Partial answers", "partial_answers"),
              ("Delivered VERIFIED", "verified"), ("Delivered BLOCKED", "blocked"),
              ("Delivered EARLY_REFUSAL", "early_refusal"),
              ("Provider 429s", "provider_429s"),
              ("Prompt tokens", "prompt_tokens"), ("Completion tokens", "completion_tokens"),
              ("Total time p50 (ms)", "total_ms_p50"), ("Total time p95 (ms)", "total_ms_p95"),
              ("First verified text p50 (ms)", "first_sentence_ms_p50"),
              ("First verified text p95 (ms)", "first_sentence_ms_p95"))
    md = [f"# F-02 overload: {root.name}", "",
          f"{n} runs per path; values are mean ± sample std [min–max] over runs.", "",
          "| Metric | v1 old (1 run) | v2 old (1 run) | v1 /ask fixed | v2 /v1/ask fixed |",
          "| :-- | --: | --: | --: | --: |"]
    for label, key in labels:
        md.append(f"| {label} | {old['v1_old'][key]} | {old['v2_old'][key]} | "
                  f"{_fmt(stats['v1_ask'][key])} | {_fmt(stats['v2_stream'][key])} |")
    md += ["", "## Per run", "", "| Path | Run | Delivered | Refused | Dropped | Calls | "
           "Cancelled | Shed | Gave up |",
           "| :-- | :-- | --: | --: | --: | --: | --: | --: | --: |"]
    for p, runs in paths.items():
        for r in runs:
            md.append(f"| {p} | {r['run']} | {r['delivered']} | {r['refused']} | {r['dropped']}"
                      f" | {r['provider_calls']} | {r['cancelled_streams']} | "
                      f"{r['shed_upfront']} | {r['gave_up']} |")
    md += ["", f"## Arrivals delivered a different number of times (of {n} runs)", "",
           "| Arrival | Question | v1 runs delivered | v2 runs delivered |",
           "| --: | :-- | --: | --: |"]
    md += [f"| {d['arrival']} | {d['question_id']} | {d['v1_delivered_runs']} | "
           f"{d['v2_delivered_runs']} |" for d in diff]
    (root / "comparison.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
