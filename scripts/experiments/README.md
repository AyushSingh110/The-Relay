# Experiment scripts

These are the scripts behind the measured results in the main README. Each one writes a new
folder under `runs/` and never touches `evidence/` or `results.csv`. Run them from the
repository root, after the setup in the main README (papers ingested, indexes built).

| Script | What it does | Runs it produced |
| :-- | :-- | :-- |
| `overload_suite.py` | v1's overload schedule, alternating `POST /ask` (v1) and `POST /v1/ask` (v2), 5 runs each | `runs/f02-overload-*`, `runs/f02b-*`, `runs/f02c-overload-*` |
| `overload_aggregate.py` | Mean ± std per path, per-run table, per-request differences | `aggregate.json`, `comparison.md` in each run folder |
| `latency_profile.py` | Per-stage timing of one request at a time (diagnostic) | written to `scratch/f02b/` |
| `fault_overload.py` | The overload suite with injected uncited sentences (`--rate`), paired seeds | `runs/f07-fault-r*` |
| `fault_summary.py` | Paired v2 − v1 difference with a 95 % interval, and L4 coverage | `runs/f07-summary-*` |
| `groq_refund_probe.py` | Does Groq refund a cancelled stream? Dry run on the stand-in by default | `runs/f03-*` |
| `judge_audit.py` | Blind review sheet for the L4 judge, its scorer, and three fixed test sets | written to `scratch/f07_judge/` |
| `answer_grading.py` | Blind grading sheet for the recorded answers, and the wrong-answer table | written to `scratch/f07_grading/` |

```bash
python scripts/experiments/fault_overload.py --rate 0.1 --repeat 5     # ~45 min, stand-in only
python scripts/experiments/fault_summary.py runs/f07-fault-r0.1-<UTC>
```

All of them use the calibrated stand-in and make **no real model calls**, except
`groq_refund_probe.py --live`. That needs `GROQ_API_KEY` and a hard `--max-real-calls` cap, and it
spends real quota.
