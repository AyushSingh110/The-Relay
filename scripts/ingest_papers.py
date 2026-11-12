"""Fetch (if needed), validate, parse and chunk the three pinned arXiv papers.

  python scripts/ingest_papers.py            # download missing PDFs from arXiv, then process
  python scripts/ingest_papers.py --offline  # never touch the network; PDFs must exist
  python scripts/ingest_papers.py --check    # offline; exit 1 if manifest.json would change

Exit codes: 0 ok, 1 validation failure or (with --check) manifest drift.
Only arxiv.org is contacted, and only for missing PDFs (or with --refresh).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from relay.errors import RelayError, sanitize_error
from relay.ingestion.arxiv_fetcher import render_manifest
from relay.ingestion.pipeline import run_ingest

EXIT_OK = 0
EXIT_FAILURE = 1


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="never download")
    mode.add_argument("--refresh", action="store_true",
                      help="re-download even if cached (checksums must still match)")
    mode.add_argument("--check", action="store_true",
                      help="offline dry run: compare the would-be manifest, write nothing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_path = args.papers_dir / "manifest.json"
    try:
        result = run_ingest(
            args.papers_dir,
            args.processed_dir,
            offline=args.offline or args.check,
            refresh=args.refresh,
            write=not args.check,
        )
    except RelayError as exc:
        print(json.dumps({"outcome": "failed", "error": sanitize_error(exc), "message": str(exc)}))
        return EXIT_FAILURE
    extraction = result.manifest.extraction or {}
    summary = {
        "outcome": "checked" if args.check else "written",
        "downloaded": list(result.downloaded),
        "papers": {
            r.paper_id: {"sha256": r.sha256, "size_bytes": r.size_bytes, "pages": r.page_count}
            for r in result.manifest.papers
        },
        "chunk_sets": extraction.get("chunk_sets"),
    }
    if args.check:
        current = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else ""
        summary["manifest_unchanged"] = current == render_manifest(result.manifest)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return EXIT_OK if summary["manifest_unchanged"] else EXIT_FAILURE
    print(json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
