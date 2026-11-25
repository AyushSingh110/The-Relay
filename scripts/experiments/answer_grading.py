"""F-07: a blind grading sheet for the Table 3 answers, for a wrong-answer rate per setting.

    python scripts/experiments/answer_grading.py worksheet   # scratch/f07_grading/worksheet.csv
    python scripts/experiments/answer_grading.py score       # the table, from the owner's grades

golflover's point: "delivered" counts a verified refusal as success but says nothing about
whether a delivered answer is right. This needs a person's grade of each real answer.
The recorded grades in evidence/ (rubric proposals, pending review) are frozen and only read,
so the owner grades in a separate sheet. The sheet is blind: the rubric's proposal is kept in
key.json. Grades: correct / partial / incorrect (answerable questions, q01-q15). Refusals and
unanswerable questions (q16-q21) are graded from the record (a refusal on an unanswerable
question is correct; a refusal on an answerable one is a wrong refusal; an answer to an
unanswerable question is wrong), and listed for the owner to confirm.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

RUN = Path("evidence/experiments/exp-20261113T064542Z-c482696")
QUESTIONS = Path("eval/questions.json")
OUT = Path("scratch/f07_grading")
VALID = ("correct", "partial", "incorrect")


def _questions() -> dict[str, dict]:
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    items = data["questions"] if isinstance(data, dict) else data
    return {q["id"]: q for q in items}


def _answers() -> list[dict]:
    answers = [json.loads(line) for line in
               (RUN / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
    settings = {}
    for line in (RUN / "traces.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        settings[record["request_id"]] = record["retrieval_setting"]
    for a in answers:
        a["setting"] = settings.get(a["request_id"], "?")
    return answers


def worksheet() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sheet = OUT / "worksheet.csv"
    if sheet.exists():
        print(f"refused: {sheet} exists (it may hold your grades); move it first")
        return 2
    questions, answers = _questions(), _answers()
    proposed = {g["request_id"]: g["proposed"] for g in json.loads(
        (RUN / "grades.json").read_text(encoding="utf-8"))["grades"]}
    to_grade = 0
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["grade_id", "question_id", "setting", "question", "expected_answer",
                         "delivered_answer", "refused", "your_grade (correct / partial / "
                         "incorrect; blank for auto rows)", "auto_grade", "notes"])
        for n, a in enumerate(sorted(answers, key=lambda a: (a["question_id"], a["setting"])),
                              start=1):
            q = questions[a["question_id"]]
            auto = ""
            if not q["answerable"]:
                auto = "correct_refusal" if a["refused"] else "answered_unanswerable"
            elif a["refused"]:
                auto = "wrong_refusal"
            else:
                to_grade += 1
            writer.writerow([f"g{n:03d}", a["question_id"], a["setting"], q["question"],
                             q.get("expected_answer") or "", a["answer_text"], a["refused"],
                             "", auto, ""])
    (OUT / "key.json").write_text(json.dumps({"run": str(RUN), "rubric_proposed": proposed},
                                             indent=2) + "\n", encoding="utf-8")
    print(f"wrote {sheet}: {len(answers)} answers, {to_grade} to grade by hand")
    return 0


def score() -> int:
    rows = list(csv.DictReader((OUT / "worksheet.csv").open(encoding="utf-8-sig")))
    grade_col = next(k for k in rows[0] if k.startswith("your_grade"))
    table: dict[str, Counter[str]] = defaultdict(Counter)
    missing = []
    for row in rows:
        grade = row["auto_grade"] or row[grade_col].strip().lower()
        if not row["auto_grade"] and grade not in VALID:
            missing.append(row["grade_id"])
            continue
        table[row["setting"]][grade] += 1
    if missing:
        print(f"score needs every hand row graded; missing {len(missing)}: {missing[:15]}")
        return 2
    lines = ["| Setting | Correct | Partial | Incorrect (wrong answer) | Wrong refusal "
             "(answerable) | Correct refusal (unanswerable) | Answered an unanswerable |",
             "| :-- | --: | --: | --: | --: | --: | --: |"]
    for setting in sorted(table):
        c = table[setting]
        lines.append(f"| {setting} | {c['correct']} | {c['partial']} | {c['incorrect']} "
                     f"| {c['wrong_refusal']} | {c['correct_refusal']} "
                     f"| {c['answered_unanswerable']} |")
    text = "\n".join(lines) + "\n"
    (OUT / "result.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("worksheet", "score"))
    sys.exit({"worksheet": worksheet, "score": score}[parser.parse_args().command]())
