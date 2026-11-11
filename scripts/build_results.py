"""Build the canonical `results.csv` from the runs listed in `evidence/canonical_runs.json`.

Exit codes: 0 success, 1 validation/aggregation failure, 2 overwrite refused.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from relay.errors import RelayError, ResultsOverwriteRefusedError
from relay.telemetry.aggregation import build_results, describe


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=Path("evidence"))
    parser.add_argument("--canonical", type=Path, default=None,
                        help="default: <evidence-root>/canonical_runs.json")
    parser.add_argument("--output", type=Path, default=Path("results.csv"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="validate and report whether the output would change; write nothing")
    mode.add_argument("--replace", action="store_true",
                      help="allow replacing an existing output file whose content differs")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    canonical = args.canonical or args.evidence_root / "canonical_runs.json"
    try:
        summary = build_results(
            args.evidence_root,
            canonical,
            args.output,
            replace=args.replace,
            check_only=args.check,
        )
    except ResultsOverwriteRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except RelayError as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(describe(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
