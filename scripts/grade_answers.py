"""Owner grading tooling: deterministic proposals, then owner confirm/override.

  python scripts/grade_answers.py propose --run-id RUN --answers answers.jsonl --out grades.json
  python scripts/grade_answers.py show    --grades grades.json [--pending]
  python scripts/grade_answers.py review  --grades grades.json --request-id ID            # confirm
  python scripts/grade_answers.py review  --grades grades.json --request-id ID \
         --final wrong --note "cites p.5; the fact is on p.4"                             # override
  python scripts/grade_answers.py summary --grades grades.json

`propose` refuses to overwrite an existing grades file, so owner reviews are never lost.
Only reviewed (confirmed/overridden) grades count as final.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from relay.eval.grader import (
    load_answers,
    load_grades,
    propose_grades,
    review,
    summary,
    write_grades,
)
from relay.eval.questions import load_question_file
from relay.ingestion.arxiv_fetcher import load_corpus_manifest

LABELS = ("correct", "wrong", "refused_correctly", "refused_wrongly")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--run-id", required=True)
    p.add_argument("--answers", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--questions", type=Path, default=Path("eval/questions.json"))
    p.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    s = sub.add_parser("show")
    s.add_argument("--grades", type=Path, required=True)
    s.add_argument("--pending", action="store_true")
    r = sub.add_parser("review")
    r.add_argument("--grades", type=Path, required=True)
    r.add_argument("--request-id", required=True)
    r.add_argument("--final", choices=LABELS, default=None,
                   help="omit to confirm the proposal; give a label to override it")
    r.add_argument("--note", default=None)
    m = sub.add_parser("summary")
    m.add_argument("--grades", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "propose":
        if args.out.exists():
            print(json.dumps({"outcome": "refused", "reason": f"{args.out} exists"}))
            return 2
        pages = {r.paper_id: r.page_count for r in load_corpus_manifest(args.papers_dir).papers}
        questions, report = load_question_file(args.questions, pages)
        if questions is None or not report.valid:
            print(json.dumps({"outcome": "invalid_questions", "errors": list(report.errors)}))
            return 1
        grades = propose_grades(args.run_id, questions, load_answers(args.answers))
        write_grades(args.out, grades)
        print(json.dumps({"outcome": "proposed", **summary(grades)}, sort_keys=True))
        return 0
    grades = load_grades(args.grades)
    if args.command == "show":
        rows = [g.model_dump(mode="json") for g in grades.grades
                if not args.pending or g.review_status == "pending"]
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if args.command == "review":
        try:
            updated = review(grades, args.request_id, final=args.final, note=args.note)
        except (KeyError, ValueError) as exc:
            print(json.dumps({"outcome": "rejected", "reason": str(exc)}))
            return 1
        write_grades(args.grades, updated)
        print(json.dumps({"outcome": "reviewed", **summary(updated)}, sort_keys=True))
        return 0
    print(json.dumps(summary(grades), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
