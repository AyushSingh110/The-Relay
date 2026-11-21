"""Side-by-side grader summary and the regression gate (plan V2-12).

``summarise`` puts the rubric, entailment and human grades of one run next to each other,
per answer and in aggregate, with the agreement between rubric and human and (once the
owner has graded) an L4 calibration sweep: for each threshold, how well "entailment score
>= threshold" predicts the owner's CORRECT. No threshold is chosen here: L4 stays advisory
until the owner picks one from this table.

``gate`` compares a candidate run with the accepted baseline:

- correctness = rubric-correct answers / answerable questions; it may not drop by more
  than 2 percentage points;
- grounded rate = answerable questions answered with a VERIFIED answer that cites a gold
  page; it may not drop by more than 2 points either. This is what a stand-in run can
  measure: the stand-in answers by extracting sentences, so it rarely states the rubric's
  required facts word for word (its rubric correctness is near 0), but whether the pipeline
  still retrieves, cites and verifies is plain to see (ARCHITECTURE_V2 §10.3: "plumbing");
- unanswerable refusal precision = correct refusals / unanswerable questions; it may not
  fall below 100 % (or below the baseline's own value, if the baseline was not at 100 %).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from relay.store import Store

MAX_CORRECTNESS_DROP: Final = 0.02
THRESHOLDS: Final = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
HUMAN_OK: Final = ("CORRECT", "UNANSWERABLE_CORRECT")


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _calibration(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]] | str:
    pairs = [(r["entailment"], r["human"] in HUMAN_OK) for r in rows
             if r["entailment"] is not None and r["human"] is not None]
    if not pairs:
        return "needs owner grades (the human grader) on scored answers"
    table = []
    for threshold in THRESHOLDS:
        tp = sum(1 for s, ok in pairs if s >= threshold and ok)
        fp = sum(1 for s, ok in pairs if s >= threshold and not ok)
        fn = sum(1 for s, ok in pairs if s < threshold and ok)
        tn = sum(1 for s, ok in pairs if s < threshold and not ok)
        table.append({"threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                      "precision": _rate(tp, tp + fp), "recall": _rate(tp, tp + fn),
                      "false_block_rate": _rate(fn, tp + fn)})
    return table


async def summarise(store: Store, run_id: str) -> dict[str, Any]:
    run = await store.eval_runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    questions = {q.id: q for q in await store.eval_questions.list_for_set(run.eval_set_id)}
    latest: dict[tuple[str, str], Any] = {}
    for grade in await store.grades.list_for_run(run_id):
        latest[(grade.grader, grade.eval_answer_id or "")] = grade
    rows: list[dict[str, Any]] = []
    for answer in await store.eval_answers.list_for_run(run_id):
        question = questions.get(answer.eval_question_id)
        spec = {} if question is None else question.spec
        rubric = latest.get(("rubric", answer.id))
        entail = latest.get(("entailment", answer.id))
        human = latest.get(("human", answer.id))
        hits: list[str] = []
        if rubric is not None and rubric.notes:
            try:
                hits = list(json.loads(rubric.notes).get("gold_page_hits") or [])
            except (ValueError, AttributeError):
                hits = []
        rows.append({
            "answer_id": answer.id, "question_id": spec.get("id"),
            "answerable": bool(spec.get("answerable", True)), "refused": answer.refused,
            "grounded": answer.verdict == "VERIFIED" and bool(hits) and not answer.refused,
            "rubric": None if rubric is None else rubric.verdict,
            "entailment": None if entail is None else entail.score,
            "human": None if human is None else human.verdict,
            "human_notes": None if human is None else human.notes})
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    scored = [r["entailment"] for r in rows if r["entailment"] is not None]
    graded = [r for r in rows if r["human"] is not None]
    both = [r for r in graded if r["rubric"] is not None]
    verdicts: dict[str, int] = {}
    for r in graded:
        verdicts[r["human"]] = verdicts.get(r["human"], 0) + 1
    return {
        "run": {"id": run.id, "kind": run.kind, "lane": run.lane, "model_id": run.model_id,
                "retrieval_setting": run.retrieval_setting, "source": run.source,
                "status": run.status, "created_at": run.created_at},
        "answers": len(rows),
        "rubric": {
            "correct": sum(1 for r in answerable if r["rubric"] == "correct"),
            "answerable": len(answerable),
            "refused_correctly": sum(1 for r in unanswerable
                                     if r["rubric"] == "refused_correctly"),
            "unanswerable": len(unanswerable),
            "correctness": _rate(sum(1 for r in answerable if r["rubric"] == "correct"),
                                 len(answerable)),
            "refusal_precision": _rate(sum(1 for r in unanswerable
                                           if r["rubric"] == "refused_correctly"),
                                       len(unanswerable)),
            "grounded": sum(1 for r in answerable if r["grounded"]),
            "grounded_rate": _rate(sum(1 for r in answerable if r["grounded"]),
                                   len(answerable))},
        "entailment": {"scored": len(scored),
                       "mean": None if not scored else round(sum(scored) / len(scored), 4)},
        "human": {"graded": len(graded), "verdicts": verdicts,
                  "correct_share": _rate(sum(1 for r in graded if r["human"] in HUMAN_OK),
                                         len(graded))},
        "rubric_vs_human": {"both": len(both), "agree": sum(
            1 for r in both if (r["rubric"] in ("correct", "refused_correctly"))
            == (r["human"] in HUMAN_OK))},
        "l4_calibration": _calibration(rows),
        "rows": rows,
    }


def gate_metrics(summary: Mapping[str, Any]) -> dict[str, Any]:
    rubric = summary["rubric"]
    return {"run": summary.get("run", {}).get("id"), "correctness": rubric["correctness"],
            "grounded_rate": rubric["grounded_rate"],
            "refusal_precision": rubric["refusal_precision"],
            "answerable": rubric["answerable"], "unanswerable": rubric["unanswerable"]}


def gate(baseline: Mapping[str, Any], candidate: Mapping[str, Any], *,
         max_drop: float = MAX_CORRECTNESS_DROP) -> tuple[bool, list[str]]:
    """(passed, reasons). Both are ``gate_metrics`` dicts."""
    failures: list[str] = []
    for metric in ("correctness", "grounded_rate"):
        base, cand = baseline.get(metric), candidate.get(metric)
        if base is not None and (cand is None or cand < base - max_drop - 1e-9):
            failures.append(f"{metric} dropped from {base:.1%} to "
                            f"{'n/a' if cand is None else f'{cand:.1%}'} "
                            f"(more than {max_drop:.0%} allowed)")
    floor = baseline.get("refusal_precision")
    floor = 1.0 if floor is None or floor >= 1.0 else floor
    refusal = candidate.get("refusal_precision")
    if candidate.get("unanswerable") and (refusal is None or refusal < floor - 1e-9):
        failures.append(f"unanswerable refusal precision {refusal!r} is below "
                        f"{floor:.0%}")
    return not failures, failures


def load_metrics(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("gate", data))


def format_summary(summary: Mapping[str, Any]) -> str:
    run, rubric = summary["run"], summary["rubric"]
    entail, human = summary["entailment"], summary["human"]
    lines = [f"run {run['id']} ({run['kind']}, lane {run['lane'] or '-'}, model "
             f"{run['model_id'] or '-'}, {run['retrieval_setting']}, {run['status']})",
             "",
             f"{'question':<9} {'answerable':<10} {'rubric':<18} {'entailment':<11} human",
             "-" * 64]
    for r in summary["rows"]:
        score = "-" if r["entailment"] is None else f"{r['entailment']:.3f}"
        lines.append(f"{r['question_id'] or '?':<9} {str(r['answerable']).lower():<10} "
                     f"{r['rubric'] or '-':<18} {score:<11} {r['human'] or 'not reviewed'}")
    lines += ["-" * 64,
              f"rubric:     correctness {_pct(rubric['correctness'])} "
              f"({rubric['correct']}/{rubric['answerable']} answerable), grounded "
              f"{_pct(rubric['grounded_rate'])} ({rubric['grounded']}/{rubric['answerable']}), "
              "refusal precision "
              f"{_pct(rubric['refusal_precision'])} "
              f"({rubric['refused_correctly']}/{rubric['unanswerable']} unanswerable)",
              f"entailment: mean {entail['mean'] if entail['mean'] is not None else '-'} "
              f"over {entail['scored']} scored answers (advisory; no threshold set)",
              f"human:      {human['graded']} reviewed, correct share "
              f"{_pct(human['correct_share'])} {human['verdicts'] or ''}".rstrip(),
              f"rubric vs human: {summary['rubric_vs_human']['agree']} of "
              f"{summary['rubric_vs_human']['both']} agree"]
    calibration = summary["l4_calibration"]
    lines.append("L4 calibration: " + (calibration if isinstance(calibration, str) else ""))
    if not isinstance(calibration, str):
        for row in calibration:
            lines.append(f"  >= {row['threshold']:.1f}: precision {_pct(row['precision'])} "
                         f"recall {_pct(row['recall'])} (tp {row['tp']} fp {row['fp']} "
                         f"fn {row['fn']} tn {row['tn']})")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


__all__ = ["MAX_CORRECTNESS_DROP", "format_summary", "gate", "gate_metrics", "load_metrics",
           "summarise"]
