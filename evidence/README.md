# Evidence

Raw, submission-relevant evidence. **Tracked in Git; never gitignored, edited by hand, or overwritten.**

| Directory | Contents |
| :--- | :--- |
| `groq_probes/<run_id>/` | `manifest.json`, `rows.csv` (probe requests), `traces.jsonl` (sanitised raw probe records) |
| `standin_calibration/<run_id>/` | `manifest.json` and calibration outputs |
| `experiments/<run_id>/` | `manifest.json`, `rows.csv`, `traces.jsonl`, plus grading/audit files when applicable |
| `canonical_runs.json` | The run IDs whose rows form the canonical `results.csv` |

Run IDs look like `exp-20261112T101500Z-6ffab47` (`probe` / `cal` / `exp`, UTC start time, git short SHA).
Each run directory is created exclusively; a new run never reuses an existing directory.
Only `scripts/build_results.py` writes the canonical `results.csv`.
