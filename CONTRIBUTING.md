# Contributing to The Relay

Thanks for taking the time to help. Bug reports, questions, docs fixes and code are all welcome.

## Ground rules

- **Be kind.** This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Open an issue first for anything big** (a new feature, a design change), so we can agree on
  the approach before you write code. Small fixes can go straight to a pull request.
- **Never commit secrets.** Keys live in `.env` (gitignored). Use `.env.example` as the template.

## Set up

```bash
git clone https://github.com/AyushSingh110/The-Relay && cd The-Relay
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt && pip install --no-deps -e .
cd web && npm ci && cd ..                               # only if you touch the UI
```

You don't need an API key, a Redis server or the PDFs to run the tests.

## Before you open a pull request

Run the same checks as CI:

```bash
ruff check .
pytest -q                                   # 1,040+ tests, fully offline
cd web && npx tsc -p tsconfig.json --noEmit && npx eslint . && npx jest   # if you touched web/
```

## How the code is organised

| Folder | What lives there |
| :-- | :-- |
| `src/relay/ingestion/` | PDF parsing and chunking |
| `src/relay/retrieval/` | BM25 + dense search (NumPy), reranking |
| `src/relay/orchestrator/` | evidence gate, prompt, the sentence verifier (L1–L4), streaming |
| `src/relay/control/` | quota guard (ProviderGuard), admission queue, cache, SingleFlight, shared state |
| `src/relay/providers/` | the HTTP client for OpenAI-compatible APIs (Groq, the stand-in) |
| `src/relay/standin/` | the calibrated stand-in model server used in tests and load runs |
| `src/relay/api/`, `src/relay/app/` | FastAPI routes, settings and wiring |
| `src/relay/store/` | SQLite store (tenants, keys, collections, request log, evaluation) |
| `src/relay/eval/` | evaluation sets, graders and the CI quality gate (`relay eval`) |
| `src/relay/telemetry/` | request traces, timing spans, Prometheus metrics |
| `src/relay/experiments/` | load-test and evaluation harnesses |
| `web/` | the React + TypeScript UI |
| `tests/` | the offline test suite |
| `scripts/`, `scripts/experiments/` | setup scripts and the experiments behind the README's numbers |

The design is in [docs/ARCHITECTURE_V2.md](docs/ARCHITECTURE_V2.md), and decisions are recorded
in [docs/DECISIONS.md](docs/DECISIONS.md).

## Project rules worth knowing

1. **Tests are offline.** They use the stand-in model or `httpx.MockTransport`, never a real API
   key and never the network. Please keep it that way.
2. **No hidden retries.** Every model call goes through the quota guard (`ProviderGuard`), and a
   failed call is never silently retried.
3. **An unchecked sentence is never shown.** Changes to streaming or verification must keep
   this.
4. **Recorded evidence is read-only.** `results.csv`, `evidence/` and existing `runs/` folders
   are measurement records. New measurements go into a **new** folder under `runs/`.
5. **Keep dependencies small.** Please don't add LangChain, LlamaIndex, a vector database or
   PyTorch. Open an issue first if you think a new dependency is needed.
6. **Write tests first for bug fixes.** Show the failing test, then the fix.

## Commit messages and pull requests

- One logical change per pull request, with a short description of what and why.
- Mention the issue it closes (`Closes #12`).
- If you changed behaviour, update the README or docs in the same pull request.

## Reporting a security problem

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
