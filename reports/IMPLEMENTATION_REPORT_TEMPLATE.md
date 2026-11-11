# Phase XX Implementation Report — `<Short Description>`

> **Phase ID:** `phase-XX`  
> **Date:** `YYYY-MM-DD HH:MM`  
> **Status:** `Completed | Partial | Blocked`  
> **Owner Approval:** `Pending Review`

---

## 1. Objective
State the exact objective of this phase as specified in `IMPLEMENTATION_PLAN.md` and the owner's phase prompt.

## 2. Implementation Summary
Concise technical overview of what was built, how components connect, and how `ASSIGNMENT.md` / `ARCHITECTURE.md` constraints were honored.

## 3. Files Created
List every created file path with a one-line purpose description.
- `path/to/file.py` — ...

## 4. Files Modified
List every modified existing file path and exact scope of change (or `None`).
- `path/to/file.py` — ...

## 5. Technical Decisions
Document implementation-level choices made within the approved architecture, including rationale and any assumptions flagged for owner review.

## 6. Commands Actually Executed
List exact commands run verbatim (no secrets included).
```bash
# command 1
# command 2
```

## 7. Test Results
Exact output summary from `pytest`, `ruff check .`, and any smoke checks run **after the final edit**. Never claim passing tests without running them.
- `pytest ...`: `X passed, Y failed, Z skipped`
- `ruff check .`: `exit code ...`

## 8. Actual Output and Generated Artifacts
Cite real observed outputs, artifact paths, schema checks, or sample records produced (clearly distinguishing offline/scripted/stand-in outputs from real Groq outputs).

## 9. Errors and Root-Cause Analysis
Document every non-trivial error encountered during implementation or testing and its true underlying root cause (or `None`).

## 10. Fixes Applied
Describe each fix made in response to Section 9 and confirm post-fix re-verification.

## 11. Outstanding Issues
List any edge cases, incomplete sub-items, unverified live-API behaviors, or workarounds explicitly left open.

## 12. Acceptance-Criteria Status
Map each acceptance criterion from `IMPLEMENTATION_PLAN.md` to `PASS`, `FAIL`, or `PENDING` with evidence.

| Criterion | Status | Evidence |
| :--- | :--- | :--- |
| ... | `PASS / FAIL / PENDING` | ... |

## 13. Remaining Risks
Identify technical, quota, schedule, or integration risks carried forward into subsequent phases.

## 14. Recommended Next Phase
State the next phase from `IMPLEMENTATION_PLAN.md`, prerequisites needed from the owner (e.g., `GROQ_API_KEY`, approval decision), and stop to await owner instruction.
