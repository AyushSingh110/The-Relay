---
name: relay-verify
description: End-of-phase verification and documentation checklist for The Relay (v2 phases). Use when a phase's code is done, before writing the ENGINEERING_LOG entry and the phase report, or whenever the full backend suite, ruff, UI tests (Jest/tsc/eslint) and the frozen-evidence check must be run.
---

# relay-verify

One command runs every check CLAUDE.md requires after the final edit and prints only a
summary (full logs stay on disk, read them only on failure):

```bash
bash .claude/skills/relay-verify/verify.sh            # backend + ruff + UI + evidence
bash .claude/skills/relay-verify/verify.sh --ui-build # also `relay ui build` (UI changed)
bash .claude/skills/relay-verify/verify.sh --skip-ui  # backend-only phase
```

It reports: `pytest` exit and count (plus FAILED lines), `ruff`, `jest` count, `tsc`,
`eslint`, the `results.csv` sha256 against the frozen `8dfb10dfaadb`, and the number of git
changes under `evidence/` and `results.csv` (must be 0).

## A failure in `tests/test_standin.py::test_stragglers_are_much_slower`

It is wall-clock sensitive (190 ms threshold). Re-run it alone 5 times before calling it a
regression; report it as a flake only if the stand-in code is unchanged (`git diff --stat
-- src/relay/standin tests/test_standin.py` is empty).

## Then, per CLAUDE.md §6 (never skip)

1. Append an `### Execution Entry` to `ENGINEERING_LOG.md` (append only; never edit old ones).
2. Write `reports/phase-v2-XX-<short>.md` with the 14 sections of
   `reports/IMPLEMENTATION_REPORT_TEMPLATE.md`.
3. Final answer: verified facts only (counts from this script's output), rulings, owner
   decisions; then STOP. Never commit or push.

## Editing tips that saved time in earlier phases

- Multi-line patches: write a small Python patch script with the Write tool (assert each
  `old` occurs exactly once, then replace) instead of bash heredocs. Heredocs break on
  quotes, apostrophes and `\n`, and a `$${...}` template can trip the shell safety check.
- `ruff --fix` can strip quotes from `Literal["a", "b"]` when `Literal` is not imported:
  import first, then fix.
- New fields on shared models: add them with defaults (v1 `/ask` and `results.csv` paths
  must not change); `ErrorCode` in `relay/schemas.py` is the frozen v1 schema.
- Mutation check for tests written after the code: a tiny pytest plugin module that
  monkeypatches the feature off, run with `PYTHONPATH=<dir> pytest -p <module>`; the
  feature's tests must fail.
