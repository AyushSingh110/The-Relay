# Changelog

All notable changes to this project are listed here. Versions follow
[Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-11-25

### Added
- **Verified streaming** (`POST /v1/ask`): each sentence is checked (structure, grounding,
  refusal form) before it is sent; an L4 entailment score arrives with the final event.
- **Collections** with PDF upload, per-collection indexes and follow-up questions.
- **Multi-tenant access:** hashed API keys, fair per-tenant admission, priorities.
- **Several model lanes** with fallback and per-lane quota guards; optional call spacing
  (`min_interval_ms`).
- **Optional Redis state** for several workers: a shared cache, SingleFlight and quota counters.
- **Observability:** request traces, a timing waterfall per request, Prometheus `/metrics`.
- **Evaluation tooling** (`relay eval`) and a CI quality gate.
- **A web UI** (React + TypeScript), Docker Compose packaging and `relay doctor`.
- **The stand-in model:** `faults.uncited_rate` for fault-injection load tests.
- **`scripts/experiments/`:** the scripts behind the README's measured results.

### Changed
- **L4 scoring runs in the background** by default (`[verifier] l4_mode = "async"`).
- **Admission is work-conserving:** a lone tenant is served first come, first served.

### Fixed
- **The stand-in split sentences differently from the verifier,** which caused false stops in
  load tests.
- **The stand-in raised an error** when a client left before its request body arrived.

## [1.0.0] - 2026-11-13

### Added
- **The first release:** answers from three pinned arXiv papers; the whole answer is verified
  before it is sent (`POST /ask`); quota-aware calls to Groq's free tier; an exact cache and
  SingleFlight; recorded experiments in `results.csv` and `evidence/`.
