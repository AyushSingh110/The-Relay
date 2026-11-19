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

## Web UI (V2-06)

The UI is a React + Vite app in `web/`. It is built to static files that `relay serve` serves at
`http://127.0.0.1:8000/` on the same origin as the API, so it needs no second server. Building it
needs Node.js 20 or newer; running it needs only Python.

```powershell
cd web
npm ci                          # once: installs the build and test tools into web/node_modules
cd ..
relay ui build                  # npm run build in web/, copied to src/relay/app/static/
$env:RELAY_CACHE__MODE = "sqlite"   # collections, uploads and /v1/ask need the metadata store
relay serve                     # then open http://127.0.0.1:8000/
```

The default lane is the stand-in model, so this needs no Groq key.

**API key.** The UI sends `Authorization: Bearer <key>` on every `/v1` request. The key is read at
build time from `VITE_API_KEY` (see `web/.env.example`), and the default is `relay-dev-key`. Until
V2-10 the API accepts any Bearer key for the development tenant. The key is compiled into the
JavaScript, so **never build a public UI with a real key**.

```powershell
$env:VITE_API_KEY = "relay-dev-key"; relay ui build
```

**Frontend checks** (in `web/`): `npm test` (Jest + React Testing Library), `npm run lint`
(ESLint with jsx-a11y), and `npm run build` (type-checks, then builds).

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

## Paper ingestion (`scripts/ingest_papers.py`)

```powershell
python scripts/ingest_papers.py            # download missing pinned PDFs from arXiv, then process
python scripts/ingest_papers.py --offline  # never touch the network (PDFs must exist)
python scripts/ingest_papers.py --check    # offline; exit 1 if data/papers/manifest.json would change
```

PDFs (`data/papers/*.pdf`) and generated text/chunks (`data/processed/`) are gitignored and
regenerable; `data/papers/manifest.json` (checksums, page counts, extraction summary) is tracked.

## Retrieval indexes and evaluation tooling

```powershell
python scripts/build_indexes.py --download-models  # one time only: fetch models into data/models
python scripts/build_indexes.py                    # offline build of data/indexes/<preset>
python scripts/build_indexes.py --check            # rebuild in memory; exit 1 on drift
python scripts/validate_questions.py               # validate the owner's eval/questions.json
python scripts/grade_answers.py --help             # propose / show / review / summary
```

Models (`data/models/`) and indexes (`data/indexes/`) are gitignored. Provenance (model
revisions, ONNX SHA-256, digests) is in the tracked `data/retrieval_manifest.json`.
How to write questions: [eval/README.md](eval/README.md).

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
