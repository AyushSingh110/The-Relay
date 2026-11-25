"""V2-07: the L4 calibration table from the recorded v1 Table 3 answers (no model calls).

    python scripts/run_l4_calibration.py [--out reports/v2-07-l4-calibration.md]

Scores every sentence of the 55 answered Table 3 rows (21 questions x 3 settings; q18 was
refused before any model call) with the configured L4 model, joins the proposed labels in
reports/v2-07-l4-labels.json, and writes the threshold table. Evidence is only read. No
blocking threshold is set: that is the owner's decision.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from relay.app.settings import load_relay_settings
from relay.experiments.l4_calibration import (
    NOT_FULLY_SUPPORTED,
    STRICT,
    CalibrationRow,
    ThresholdLine,
    calibration_rows,
    calibration_table,
    load_labels,
)
from relay.experiments.table3_replay import TABLE3_RUN
from relay.orchestrator.verifier import EntailmentScorer

THRESHOLDS = (0.3, 0.5, 0.7, 0.9)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f} %"


def _table(lines: Sequence[ThresholdLine]) -> list[str]:
    out = ["| Threshold | Sentences blocked | True positive rate (blocked / should block) "
           "| False positive rate (blocked / should keep) |", "| --: | --: | --: | --: |"]
    for line in lines:
        out.append(f"| {line.threshold} | {line.blocked} of {line.sentences} "
                   f"| {_pct(line.tpr)} ({line.true_positives}/{line.positives}) "
                   f"| {_pct(line.fpr)} ({line.false_positives}/{line.negatives}) |")
    return out


def _distribution(rows: Sequence[CalibrationRow]) -> list[str]:
    out = ["| Label | Sentences | L4 median | L4 min | L4 max |", "| :-- | --: | --: | --: | --: |"]
    for label in ("supported", "partially_supported", "not_supported"):
        scores = [r.l4 for r in rows if r.shown and r.label == label and r.l4 is not None]
        if scores:
            out.append(f"| {label} | {len(scores)} | {statistics.median(scores):.3f} "
                       f"| {min(scores):.3f} | {max(scores):.3f} |")
    return out


def render(rows: Sequence[CalibrationRow], *, model: str, seconds: float) -> str:
    shown = [r for r in rows if r.shown]
    unlabelled = [r for r in shown if r.label is None]
    l2_failed = [r for r in rows if not r.l2_passed and not r.l3_refusal]
    refusals = [r for r in rows if r.l3_refusal]
    lines = [
        "# V2-07 L4 calibration (advisory; no threshold set)", "",
        f"- **Model:** `{model}`; scored {len(rows)} sentences in {seconds:.1f} s.",
        f"- **Source:** the recorded v1 Table 3 answers, `evidence/experiments/{TABLE3_RUN}` "
        "(read only). There are 55 answered rows across 21 questions; q18 was refused before "
        "any model call in every setting. No model provider was called.",
        f"- **Sentences:** {len(shown)} pass L1–L3 on their own; these are the sentences "
        "L4 would act on. They are counted per sentence: a few come after a sentence that "
        "stops their answer, so a user would not see them. "
        f"{len(l2_failed)} fail L1/L2, and {len(refusals)} are refusal-shaped (L3). Neither "
        "group is counted below, because both are already stopped.",
        "- **Score:** a sentence's L4 score is the best entailment probability over the "
        "passages it cites. A threshold blocks a shown sentence whose score is below it.",
        "- **Labels:** `reports/v2-07-l4-labels.json` holds per-sentence labels **drafted "
        "by an AI assistant and not yet reviewed** (owner decision 2026-11-20). Every rate below "
        "is provisional until you review them.",
        f"- **Unlabelled shown sentences:** {len(unlabelled)}.", "",
        "## If 'should block' means not fully supported (not_supported or partially_supported)",
        "", *_table(calibration_table(rows, thresholds=THRESHOLDS,
                                      positive=NOT_FULLY_SUPPORTED)), "",
        "## If 'should block' means not_supported only (strict)", "",
        *_table(calibration_table(rows, thresholds=THRESHOLDS, positive=STRICT)), "",
        "## L4 scores by proposed label (shown sentences)", "", *_distribution(rows), "",
        "## Sentences proposed as not fully supported", "",
        "| Where | Label | L4 | Sentence |", "| :-- | :-- | --: | :-- |",
    ]
    for r in sorted(shown, key=lambda r: (r.label or "", r.l4 or 0.0)):
        if r.label in NOT_FULLY_SUPPORTED:
            text = r.sentence.replace("|", "/")[:140]
            lines.append(f"| {r.question_id}/{r.setting}#{r.index} | {r.label} | {r.l4} "
                         f"| {text} |")
    lines += ["", "## Sentences that fail L1/L2 (already blocked), for reference", "",
              "| Where | Failures | L4 |", "| :-- | :-- | --: |"]
    for r in l2_failed:
        lines.append(f"| {r.question_id}/{r.setting}#{r.index} | {', '.join(r.failures)} "
                     f"| {r.l4} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("reports/v2-07-l4-calibration.md"))
    parser.add_argument("--labels", type=Path, default=Path("reports/v2-07-l4-labels.json"))
    args = parser.parse_args(argv)
    if "evidence" in args.out.resolve().parts:
        print("refused: the calibration report never goes under evidence/")
        return 2
    v = load_relay_settings(None).verifier
    scorer = EntailmentScorer(v.l4_model_dir, model_file=v.l4_model_file, threads=v.l4_threads)
    started = time.perf_counter()
    rows = calibration_rows(Path("evidence/experiments") / TABLE3_RUN, scorer,
                            load_labels(args.labels))
    seconds = time.perf_counter() - started
    args.out.write_text(render(rows, model=f"{v.l4_model_dir.name}/{v.l4_model_file}",
                               seconds=seconds), encoding="utf-8")
    print(f"{len(rows)} sentences scored in {seconds:.1f} s; written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
