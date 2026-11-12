"""Validate the owner-written evaluation questions (no edits are ever made).

  python scripts/validate_questions.py                       # eval/questions.json, strict
  python scripts/validate_questions.py --allow-incomplete    # coverage shortfalls -> warnings
  python scripts/validate_questions.py --file eval/questions.template.json

Exit codes: 0 valid, 1 invalid (errors are printed as JSON).
Page numbers are checked against the page counts in data/papers/manifest.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from relay.eval.questions import load_question_file
from relay.ingestion.arxiv_fetcher import load_corpus_manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="report coverage shortfalls as warnings, not errors")
    args = parser.parse_args(argv)
    page_counts = {r.paper_id: r.page_count for r in load_corpus_manifest(args.papers_dir).papers}
    _, report = load_question_file(args.file, page_counts,
                                   strict_coverage=not args.allow_incomplete)
    print(json.dumps({"file": str(args.file), **report.model_dump()}, indent=2, sort_keys=True))
    return 0 if report.valid else 1


if __name__ == "__main__":
    sys.exit(main())
