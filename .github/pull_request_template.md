## What and why

<!-- What does this change, and why? Link the issue: Closes #... -->

## Checklist

- [ ] `ruff check .` and `pytest -q` pass (offline, no API key)
- [ ] UI checks pass if `web/` changed (`npx tsc --noEmit`, `npx eslint .`, `npx jest`)
- [ ] Tests added or updated (a failing test first for bug fixes)
- [ ] Docs or README updated if behaviour changed
- [ ] No secrets, and no edits to `results.csv`, `evidence/` or existing `runs/` folders
