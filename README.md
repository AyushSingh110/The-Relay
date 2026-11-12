# The Relay

Rate-limit-aware RAG Q&A that answers only from three pinned arXiv papers
(`1706.03762v7`, `2410.00037v1`, `2507.07955v2`), verifies citations before
delivery, and records every request in a recomputable `results.csv`.

Design: [ARCHITECTURE.md](ARCHITECTURE.md). Phase plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
Execution history: [ENGINEERING_LOG.md](ENGINEERING_LOG.md).

## Setup (Conda environment `the-relay`, Python 3.12)

```powershell
conda activate the-relay
python -m pip install -e ".[dev]"
copy .env.example .env   # then fill in GROQ_API_KEY; .env is gitignored
```

## Checks

```powershell
python -m pytest -v
python -m ruff check .
```

## Groq probes (`scripts/probe_groq.py`)

Default is a **dry run** that prints the planned calls and contacts nothing:

```powershell
python scripts/probe_groq.py discover
python scripts/probe_groq.py baseline --model openai/gpt-oss-20b --max-real-calls 5
```

A live run (owner-approved runs only) needs all of: `--live`, `--max-real-calls N` covering
the whole plan, `MAX_REAL_CALLS >= N` in `.env`, and `GROQ_API_KEY`. The `minute-limit` mode
additionally needs `--owner-approved-rate-limit-probe`. Runs stop at the first daily-scope
429, at any 429 outside `minute-limit`, and at `DAILY_SAFETY_FRACTION` of the
`x-ratelimit-*-requests` headers. Exit codes: 0 ok/dry run, 1 failure, 2 refused by a safeguard.

## Evidence and results

- `evidence/groq_probes/<run_id>/`, `evidence/standin_calibration/<run_id>/`,
  `evidence/experiments/<run_id>/` hold raw run evidence (tracked in Git).
- `evidence/canonical_runs.json` lists the runs that make up the canonical results.
- `results.csv` is produced **only** by:

```powershell
python scripts/build_results.py            # refuses to replace a differing results.csv
python scripts/build_results.py --check    # validate only, write nothing
python scripts/build_results.py --replace  # explicitly regenerate
```
