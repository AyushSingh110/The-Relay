"""P11: compute Tables 1, 2a, 2b and 3 from the canonical results.csv and run evidence.

  python scripts/compute_tables.py                      # writes scratch/computed_tables.md
  python scripts/compute_tables.py --check              # recompute and compare, write nothing

The output is deterministic: running it twice on the same inputs gives identical bytes.
Exit codes: 0 ok, 1 failure or (with --check) a difference.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

from relay.errors import RelayError, sanitize_error
from relay.experiments.tables import render

CALIBRATION_RUN = "cal-20261113T022403Z-3f687c6"  # owner decision 2026-11-13: cited, not listed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, default=Path("results.csv"))
    parser.add_argument("--evidence-root", type=Path, default=Path("evidence"))
    parser.add_argument("--canonical", type=Path, default=Path("evidence/canonical_runs.json"))
    parser.add_argument("--calibration-run", default=CALIBRATION_RUN)
    parser.add_argument("--output", type=Path, default=Path("scratch/computed_tables.md"))
    parser.add_argument("--check", action="store_true",
                        help="recompute and compare with --output without writing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        text = render(args.results, args.evidence_root, calibration_run=args.calibration_run,
                      canonical_path=args.canonical)
    except (RelayError, OSError, ValueError, KeyError) as exc:
        print(f"failed: {sanitize_error(exc)}")
        return 1
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if args.check:
        existing = args.output.read_bytes() if args.output.is_file() else None
        same = existing == data
        print(f"recomputed sha256 {digest}; {'identical to' if same else 'DIFFERS from'} "
              f"{args.output}")
        return 0 if same else 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"wrote {args.output} ({len(data)} bytes, sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
