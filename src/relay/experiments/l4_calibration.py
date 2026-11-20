"""V2-07 L4 calibration: score every sentence of the recorded v1 Table 3 answers with the
entailment model, and tabulate what a blocking threshold would do (plan V2-07 task 4).

The recorded answers and their passages come from the v1 evidence run (read only); no model
provider is called. A sentence's L4 score is the best score over the passages it cites (the
scorer's rule at runtime too, ``streaming.score_sentence``). Labels say whether the cited
passages support the sentence; they are proposals by Claude Code, unreviewed (owner decision
2026-11-20), so every rate here is provisional until the owner reviews them.

A threshold blocks a shown sentence when its score is below it. Only sentences the user
would see are counted: claims that pass L1-L3 (L2-failed and refusal-shaped ones are already
stopped). A "positive" is a sentence that should be blocked:

- ``not fully supported``: labelled ``not_supported`` or ``partially_supported``;
- ``strict``: labelled ``not_supported`` only.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.orchestrator.verifier import TAG_PATTERN, EntailmentScore, Verifier, split_sentences
from relay.retrieval.hybrid_index import RetrievedPassage

LABELS: Final = ("supported", "partially_supported", "not_supported")
NOT_FULLY_SUPPORTED: Final = frozenset({"not_supported", "partially_supported"})
STRICT: Final = frozenset({"not_supported"})
LabelKey = tuple[str, tuple[str, ...]]  # (sentence without tags, cited chunk ids)


@dataclass(frozen=True)
class CalibrationRow:
    question_id: str
    setting: str
    index: int  # 1-based sentence index in the answer
    sentence: str  # without citation tags
    tags: tuple[int, ...]
    chunk_ids: tuple[str, ...]  # the passages it cites (a tag number differs by setting)
    l2_passed: bool  # L1 and L2 found nothing
    l3_refusal: bool
    failures: tuple[str, ...]
    l4: float | None
    label: str | None  # proposed label, or None when there is none

    @property
    def shown(self) -> bool:
        """Passes L1-L3 on its own (in an answer stopped earlier it may still not be
        shown: the counts below are per sentence, not per delivered answer)."""
        return self.l2_passed and not self.l3_refusal


def _passages(outcome: Mapping[str, object]) -> list[RetrievedPassage]:
    items = outcome["passages"]
    assert isinstance(items, list)
    return [RetrievedPassage.model_validate_json(json.dumps(p)) for p in items]


def calibration_rows(run_dir: Path, scorer: EntailmentScore,
                     labels: Mapping[LabelKey, str]) -> list[CalibrationRow]:
    """Every claim and refusal-shaped sentence of every answered row in ``run_dir``."""
    verifier = Verifier(V1_CUE_SET)
    rows: list[CalibrationRow] = []
    for line in (run_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        outcome = record["outcome"]
        raw = outcome["trace"].get("raw_answer")
        if not raw:
            continue
        passages = _passages(outcome)
        checker = verifier.sentence_checker(passages)
        units = split_sentences(raw)
        for index, unit in enumerate(units, start=1):
            check = checker.check(unit, index=index, is_last=index == len(units))
            if not (check.claim or check.refusal):
                continue
            body = TAG_PATTERN.sub(" ", unit).strip()
            chunk_ids = tuple(passages[t - 1].chunk_id for t in check.tags)
            l4 = None
            if check.claim and check.tags:
                l4 = round(max(scorer.score(body, passages[t - 1].text) for t in check.tags), 4)
            rows.append(CalibrationRow(
                question_id=record["question_id"], setting=record["retrieval_setting"],
                index=index, sentence=body, tags=check.tags, chunk_ids=chunk_ids,
                l2_passed=check.claim and not check.failures, l3_refusal=check.refusal,
                failures=tuple(f.code for f in check.failures), l4=l4,
                label=labels.get((body, chunk_ids))))
    return rows


@dataclass(frozen=True)
class ThresholdLine:
    threshold: float
    sentences: int  # shown sentences that have a label and a score
    positives: int  # should be blocked
    negatives: int  # should be kept
    blocked: int
    true_positives: int  # blocked and should be
    false_positives: int  # blocked but supported

    @property
    def tpr(self) -> float | None:
        return None if not self.positives else self.true_positives / self.positives

    @property
    def fpr(self) -> float | None:
        return None if not self.negatives else self.false_positives / self.negatives


def calibration_table(rows: Sequence[CalibrationRow], *, thresholds: Sequence[float],
                      positive: Collection[str] = NOT_FULLY_SUPPORTED) -> list[ThresholdLine]:
    scored = [r for r in rows if r.shown and r.label is not None and r.l4 is not None]
    lines = []
    for threshold in thresholds:
        blocked = [r for r in scored if r.l4 is not None and r.l4 < threshold]
        positives = sum(r.label in positive for r in scored)
        lines.append(ThresholdLine(
            threshold=threshold, sentences=len(scored), positives=positives,
            negatives=len(scored) - positives, blocked=len(blocked),
            true_positives=sum(r.label in positive for r in blocked),
            false_positives=sum(r.label not in positive for r in blocked)))
    return lines


def load_labels(path: Path) -> dict[LabelKey, str]:
    """``reports/v2-07-l4-labels.json`` (proposed labels) as {(sentence, chunk ids): label}:
    each label was judged against those exact passages."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[LabelKey, str] = {}
    for item in data["labels"]:
        if item["label"] not in LABELS:
            raise ValueError(f"{path}: unknown label {item['label']!r} for {item['id']}")
        out[(item["sentence"], tuple(item["chunk_ids"]))] = item["label"]
    return out


__all__ = ["NOT_FULLY_SUPPORTED", "STRICT", "CalibrationRow", "ThresholdLine",
           "calibration_rows", "calibration_table", "load_labels"]
