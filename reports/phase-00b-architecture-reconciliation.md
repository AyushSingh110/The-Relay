# Phase 00B Report — Architecture Reconciliation

> **Phase ID:** `phase-00b`
> **Date:** `2026-11-11 13:14 IST`
> **Status:** `Completed` (documentation only)
> **Owner Approval:** `Pending Review`

---

## 1. Objective

Incorporate the owner's Phase 00B decisions and corrections into the governing documents (`ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`, `CLAUDE.md`), preserve `ASSIGNMENT.md` unchanged, record what changed and why, and list the decisions that remain open, without writing application code, installing dependencies, or sending Groq requests.

## 2. Implementation Summary

- `ARCHITECTURE.md` was rewritten as **v2**. Every decision carries a tag: **[OWNER-DECIDED]** (binding), **[PROPOSED]** (engineering proposal), **[OPEN]** (needs owner choice) or **[TO MEASURE]**. The status line now reads `REVISED — OWNER DECISIONS INCORPORATED; FORMAL APPROVAL PENDING`. Open items are collected in §16.
- `IMPLEMENTATION_PLAN.md` was rewritten as **v2**:
  - a re-ordered 3-day schedule with the Groq provider and baseline probes on Day 1 and a Day 3 afternoon submission buffer;
  - a real-call budget per day;
  - a dependency graph;
  - a cut line;
  - concise per-phase specifications;
  - one reusable phase prompt template, replacing the ten v1 prompts.
- `CLAUDE.md` received targeted edits only: the decision-tag semantics in §2, updated examples in §5.6, and new restrictions §5.12–16.
- `ENGINEERING_LOG.md`: one entry appended; the earlier entry is untouched.
- `ASSIGNMENT.md`: not modified.

## 3. Files Created

- `reports/phase-00b-architecture-reconciliation.md` — this report.

## 4. Files Modified

- `ARCHITECTURE.md` — full rewrite to v2 (scope described in §5–§6 below).
- `IMPLEMENTATION_PLAN.md` — full rewrite to v2.
- `CLAUDE.md` — §2 item 2; §5 item 6; §5 items 12–16 added.
- `ENGINEERING_LOG.md` — Phase 00B entry appended.

## 5. What Changed: Owner Decisions Incorporated

| # | Owner decision | Where it now lives |
| :--- | :--- | :--- |
| 1 | Exact cache + SingleFlight only; semantic cache deferred and documented as deliberately excluded | ARCH §8, §14; PLAN P08 |
| 2 | Buffer the full answer, verify citation integrity and grounding, then deliver; on failure return a safe refusal with no second LLM call; record provider TTFT and user-visible TTFT separately | ARCH §7.1–7.3, §13.4; PLAN P05 |
| 3 | The harness is the single owner of `results.csv`; runs stored separately; duplicate-safe aggregation; tables recomputable; abandonment = `timeout` + `client_gave_up`; no new grade values | ARCH §13; PLAN P01, P10, P11; CLAUDE §5.13 |
| 4 | Provider + baseline probes on Day 1; hard max-call guard; owner approval before deliberately triggering limits; never exhaust the daily quota for testing | ARCH §9.4; PLAN §1, P02, P02-LIVE; CLAUDE §5.12 |
| 5 | `openai/gpt-oss-20b` is a candidate only; discover account models; small comparison before selection | ARCH §10; PLAN P02-LIVE, P07 |
| 6 | Owner writes ≥15 (target 18) questions; Claude prepares the structure and grading method, then verifies them against the PDFs | ARCH §11; PLAN P04, P04B; CLAUDE §5.15 |
| 7 | Separate identifiable run directories for probes, calibration and experiments; evidence never gitignored | ARCH §13.1; PLAN P01; CLAUDE §5.14 |

| Correction | Where it now lives |
| :--- | :--- |
| Queue capacity derived from measured throughput × patience model (fixed 16 withdrawn) | ARCH §15; PLAN P06 |
| Every Groq header's meaning verified empirically; request headers never read as RPM | ARCH §9.1 (verification-method table); PLAN P02, P02-LIVE; CLAUDE §5.16 |
| Token reservations reconciled with provider-reported usage; no tokenizer assumption | ARCH §3, §9.3; PLAN P06 |
| One worker; SingleFlight and in-memory limits documented | ARCH §2 |
| Retrieval-quality runs isolated from cache and load runs | ARCH §8; PLAN P07 (sequential, cache off, no concurrency) |
| No unnecessary second tool, repair loop or infrastructure | ARCH §6, §14 |
| 3-day plan with final-day buffer | PLAN §1 |

## 6. Technical Decisions Made Within the Owner's Decisions (flagged for review)

These are my proposals. They follow from the decisions above but were not individually dictated by the owner:

1. **SQLite cache → in-memory cache, cleared per run.** Persistence wasn't needed for any table, and it risked contaminating experiments across runs.
2. **`tiktoken` removed and chunk sizes defined in words.** It isn't verified to match Groq tokenizers and it downloads encoding files on first use, which conflicts with offline tests. Token costs come from provider usage.
3. **New columns `server_total_ms`, `run_id`, `question_id`, `degraded`, `usage_source`.** `total_ms` and `first_token_ms` are now client-measured, so the span invariant applies to `server_total_ms`, and the difference is client/transport overhead.
4. **Verdict enum changed.** It is now `VERIFIED | REFUSAL_VERIFIED | BLOCKED | EARLY_REFUSAL | NOT_RUN`; `UNVERIFIED`/`SKIPPED` are replaced.
5. **`evidence/` (tracked) + `scratch/` (ignored)** replace `runs/`. `evidence/canonical_runs.json` selects which runs make up `results.csv`. Request IDs are prefixed by run_id, so re-runs never collide.
6. **Probe sanitisation** also redacts organisation/account identifiers found in Groq error bodies, not just keys and auth headers.
7. **Daily safety fraction** (proposed 0.75) as a second guard alongside `--max-real-calls`.
8. **The retrieval eval runner (P07) calls the pipeline in-process**, not through the HTTP API. This keeps it isolated from admission and cache, so for those runs the runner acts as the client.
9. **Phase renumbering:** P01–P11, plus P02-LIVE (an operational probe run) and P04B (verification of the owner's questions).

## 7. Original Recommendations: Retained, Changed or Rejected

| Original recommendation (Phase 00 / my review) | Outcome |
| :--- | :--- |
| One nominal LLM call; deterministic scope/tool/gate/verifier | **Retained** |
| Verify-before-delivery | **Retained**; failure path changed from "optional repair or safe error" to **safe refusal only** (owner) |
| My suggestion of an extractive answer on verification failure | **Rejected by owner** in favour of a safe refusal. The daily-exhausted path keeps "honest notice + passage pointers" (no generated text) |
| Sentence-gated streaming as a possible later optimisation | **Not adopted**; TTFT cost is measured and reported instead |
| L2 semantic cache (v1 optional module) | **Rejected / excluded** (owner) |
| SingleFlight leadership transfer | **Changed** to a detached leader task, cancelled only when no waiters remain |
| `fetch_section_context` second tool | **Removed** |
| SQLite `cache.db` | **Removed** (§6.1) |
| Fixed queue `maxsize=16` | **Withdrawn**; derived capacity |
| Header mapping (requests = RPD, tokens = TPM) | **Retained as a hypothesis**; must be verified empirically |
| `tiktoken` `cl100k_base` | **Removed** |
| Server writes `results.csv` | **Rejected**; harness aggregation is the only writer |
| `runs/` gitignored | **Rejected**; `evidence/` tracked |
| Claude authors 18 eval questions (v1 Phase 03) | **Rejected**; owner authors, Claude verifies |
| Real Groq calls start Day 2 | **Changed** to Day 1 baseline |
| "~150 real calls over 3 days" | **Replaced** with a per-day budget (≤40 / ≤80 / ≤30) plus the daily safety fraction, subject to measured limits |
| Verifier-blocked status `error` (v1) | **Changed** to proposed `ok` + `error_code` so the row stays gradable (**open**) |

## 8. Commands Actually Executed

```bash
date "+%Y-%m-%d %H:%M %Z"     # -> 2026-11-11 13:14 IST
git status --short            # -> (empty: clean tree before edits)
# Consistency scan over ARCHITECTURE.md, IMPLEMENTATION_PLAN.md, CLAUDE.md for stale v1 terms:
#   runs/ | SQLite | cache.db | semantic | fetch_section_context | maxsize=16 | tiktoken | max_retries | old phase numbers | repair
#   -> remaining hits are intentional (describe removed items) or unrelated ("hyphenation repair", "semantic entailment").
```

Earlier in this session (read-only environment check): `python --version` → `Python 3.13.5`; `uv --version` → `uv 0.12.5`; `pip --version` → `pip 25.1 (python 3.13)`.

No Groq requests, no network calls, no dependency installs, no source code.

## 9. Test Results

None. There is no source code yet. `pytest`/`ruff` were not run, because there is nothing to test.

## 10. Contradiction Review (after edits)

Checked: ARCH ↔ PLAN ↔ CLAUDE ↔ ASSIGNMENT.

- **Resolved during review:** the docs didn't define whether the harness reveals its sampled patience to the server as `deadline_ms`. Leaving it undefined would blur server-side shedding with client abandonment. Added as ARCH §16 item 10.
- **ASSIGNMENT interpretation notes vs v2:** consistent. Appended columns from 13 onward, still subject to approval; blank `answer_grade` for undelivered answers.
- **Status of verifier-blocked answers:** v2 proposes `ok`. This is consistent with ASSIGNMENT note 3, which leaves grades blank only for rate_limited/timeout/error rows, so blocked answers stay gradable. Still open.
- **Remaining known tension (not a contradiction):** Table 1's "what it returns when a limit is hit" and "time until it recovers" can only be measured by deliberately triggering a minute limit. That probe needs separate approval. Without it, those cells come from documentation or the `retry-after` of incidental 429s, at lower confidence, or are marked "not measured". Daily recovery will be "not measured" by design.
- **Historical documents:** `reports/phase-00-architecture-review.md` and the Phase 00 log entry still describe v1 (`runs/`, SQLite, L2). They are historical records, deliberately left unedited per the append-only rule. v2 supersedes them.
- **Brief allows 4 days; owner has 3:** the plan uses 3. No conflict.

## 11. Outstanding Issues

- Nothing about Groq has been measured: limits, header behaviour, latency, model list and pricing are all unverified.
- Retrieval preset sizes are placeholders until P03/P04 text inspection.
- IDE markdown-lint warnings (blank lines around headings/lists/fences) on the rewritten docs are cosmetic and were not fixed.

## 12. Acceptance-Criteria Status

| Criterion (from the owner's Phase 00B instructions) | Status | Evidence |
| :--- | :--- | :--- |
| Owner decisions 1–7 incorporated | PASS | §5 table, with locations |
| Seven corrections incorporated | PASS | §5 second table |
| `ASSIGNMENT.md` preserved | PASS | not edited; not in the modified list |
| Phase 00B log entry appended (no overwrite) | PASS | `ENGINEERING_LOG.md` second entry |
| This report created | PASS | this file |
| No source code, installs, Groq calls, or Phase 1 start | PASS | §8 commands |
| Documents inspected for contradictions | PASS | §10 |

## 13. Remaining Risks

1. **Schedule:** 11 phases plus two operational steps in about 2.5 engineering days, with an approval stop after each. Owner review turnaround is on the critical path. The cut line is in PLAN §1.
2. **Token budget:** if measured TPD is small relative to tokens per answer, Table 3 (all questions × 3 presets) may not fit in one day. Mitigation: split it across Days 2–3, reduce the model comparison, and log the decision.
3. **Owner question delivery timing:** P04B and P07 are blocked until the questions exist. The owner should ideally draft them on Day 1 evening.
4. **Verifier strength:** it checks structure and grounding, not entailment, so wrong-but-cited answers can pass. Owner grading is the backstop, and the limitation must appear in the write-up.
5. **Stand-in fidelity** depends on how many Day 1 baseline probes were collected. With few samples, the tail fit will be weak and must be reported as low confidence.

## 14. Recommended Next Phase

**P01 — Foundation, Contracts, Run Directories and Aggregation**, once the owner:
1. resolves at least ARCH §16 items 2 (blocked-answer status), 3 (probe rows in `results.csv`) and 9 (verifier granularity), because they shape the P01 schema;
2. changes the `ARCHITECTURE.md` status to `APPROVED` (or lists amendments);
3. authorises P01.

Items 4–8 and 10 can wait until the phase that needs them. P02-LIVE additionally needs `GROQ_API_KEY` in `.env` and explicit approval of its call cap.

*Stopping here for owner review.*
