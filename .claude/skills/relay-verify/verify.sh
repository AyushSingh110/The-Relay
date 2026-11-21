#!/usr/bin/env bash
# The Relay: end-of-phase verification in one command (CLAUDE.md §5.5, §6).
# Runs the backend suite, ruff, the UI tests, tsc, eslint and the frozen-evidence check;
# prints a short summary (full logs are kept in $OUT for reading on failure).
#
#   bash .claude/skills/relay-verify/verify.sh [--ui-build] [--skip-ui]
#
# RELAY_PY overrides the interpreter (default: the project's conda env).
set -u
ROOT=$(git rev-parse --show-toplevel)
PY=${RELAY_PY:-/c/Users/ASUS/anaconda3/envs/the-relay/python.exe}
OUT=${RELAY_VERIFY_OUT:-${TMPDIR:-/tmp}/relay-verify}
FROZEN_SHA="8dfb10dfaadb"  # results.csv (v1 evidence, read-only)
UI=1
BUILD=0
for arg in "$@"; do
  case "$arg" in
    --skip-ui) UI=0 ;;
    --ui-build) BUILD=1 ;;
  esac
done
mkdir -p "$OUT"
cd "$ROOT" || exit 1

"$PY" -m pytest -q -p no:cacheprovider > "$OUT/pytest.txt" 2>&1
echo "pytest exit=$? :: $(tail -1 "$OUT/pytest.txt")"
grep -E "^(FAILED|ERROR)" "$OUT/pytest.txt" | head -20

"$PY" -m ruff check . > "$OUT/ruff.txt" 2>&1
echo "ruff exit=$? :: $(tail -1 "$OUT/ruff.txt")"

if [ "$UI" = 1 ]; then
  (
    cd web || exit 1
    npx jest > "$OUT/jest.txt" 2>&1
    echo "jest exit=$? :: $(grep -E '^Tests:' "$OUT/jest.txt")"
    npx tsc -p tsconfig.json --noEmit > "$OUT/tsc.txt" 2>&1
    echo "tsc exit=$?"
    npx eslint . > "$OUT/eslint.txt" 2>&1
    echo "eslint exit=$?"
  )
fi
if [ "$BUILD" = 1 ]; then
  "$(dirname "$PY")/Scripts/relay.exe" ui build > "$OUT/uibuild.txt" 2>&1 \
    || relay ui build > "$OUT/uibuild.txt" 2>&1
  echo "ui build exit=$? :: $(tail -1 "$OUT/uibuild.txt")"
fi

SHA=$(sha256sum results.csv | cut -c1-12)
CHANGED=$(git status --short results.csv evidence/ | wc -l)
echo "results.csv sha256 $SHA (frozen $FROZEN_SHA): $([ "$SHA" = "$FROZEN_SHA" ] && echo OK || echo CHANGED)"
echo "evidence/ + results.csv git changes: $CHANGED (must be 0)"
echo "logs: $OUT"
