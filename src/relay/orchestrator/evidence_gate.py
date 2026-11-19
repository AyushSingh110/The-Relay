"""Evidence gate (pipeline step 3): a deterministic branch and one bounded loop.

``EvidenceGate`` works on one corpus's ``CueSet``: its pinned arXiv versions (for the
version refusal) and its document cues (for choosing the re-query sentence: a sentence is
kept when it names a missing document by title, short name, arXiv id or topic). The
module functions use the v1 corpus. No paper is named in this module (V2-04).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.retrieval.cues import CueSet
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.scope import ScopeAnalyser, ScopeDecision

# [PROPOSED] Calibrated on P04B: off-topic scored <= -8.5, answerable >= +0.22.
RERANK_REFUSAL_SCORE: Final = -5.0
MAX_LOOPS: Final = 1

GateAction = Literal["SUFFICIENT", "LOOP_EXPAND", "EARLY_REFUSAL"]
GateReason = Literal[
    "evidence_found",
    "missing_paper_scope",
    "no_passages",
    "version_not_in_corpus",
    "low_corpus_coverage",
    "rerank_evidence_below_threshold",
]
_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    action: GateAction
    reason: GateReason
    loop_count: int
    paper_filter: tuple[str, ...] | None = None
    requery: str | None = None
    detail: str = ""


def expected_papers(scope: ScopeDecision) -> tuple[str, ...]:
    """Papers the answer should draw on: strong cues, plus weak cues for a comparison."""
    papers = set(scope.strong_papers)
    if scope.comparison:
        papers.update(scope.weak_papers)
    return tuple(sorted(papers))


def _best_rerank(passages: Sequence[RetrievedPassage]) -> float | None:
    scores = [p.rerank_score for p in passages if p.rerank_score is not None]
    return max(scores) if scores else None


class EvidenceGate:
    """The gate for one corpus. The cue set is read once, at construction."""

    def __init__(self, cue_set: CueSet, *, analyser: ScopeAnalyser | None = None) -> None:
        self.cue_set = cue_set
        # re-query cues: the same document cues as scope analysis (shared when given)
        self._analyser = ScopeAnalyser(cue_set) if analyser is None else analyser
        self._pinned: Mapping[str, str] = cue_set.pinned_papers()

    def requery_text(self, question: str, papers: Sequence[str]) -> str:
        """Deterministically narrow the question to the part naming the missing paper(s)."""
        wanted = set(papers)
        sentences = [s.strip() for s in _SENTENCE_END.split(question.strip()) if s.strip()]
        if len(sentences) < 2:
            return question
        chosen = []
        for sentence in sentences:
            scope = self._analyser.analyze(sentence)
            if wanted & (set(scope.strong_papers) | set(scope.weak_papers)):
                chosen.append(sentence)
        return " ".join(chosen) if chosen else question

    def decide(self, scope: ScopeDecision, passages: Sequence[RetrievedPassage], *,
               loop_count: int) -> GateDecision:
        return _decide(scope, passages, loop_count=loop_count, pinned=self._pinned,
                       requery=self.requery_text)


_V1_GATE: Final = EvidenceGate(V1_CUE_SET)


def requery_text(question: str, papers: Sequence[str]) -> str:
    """``EvidenceGate.requery_text`` for the v1 corpus."""
    return _V1_GATE.requery_text(question, papers)


def decide(scope: ScopeDecision, passages: Sequence[RetrievedPassage], *,
           loop_count: int) -> GateDecision:
    """``EvidenceGate.decide`` for the v1 corpus."""
    return _V1_GATE.decide(scope, passages, loop_count=loop_count)


def _decide(scope: ScopeDecision, passages: Sequence[RetrievedPassage], *, loop_count: int,
            pinned: Mapping[str, str],
            requery: Callable[[str, Sequence[str]], str]) -> GateDecision:
    if scope.version_mismatch:
        held = sorted({pinned[m.split("v")[0]] for m in scope.version_mentions
                       if m.split("v")[0] in pinned})
        return GateDecision(
            action="EARLY_REFUSAL", reason="version_not_in_corpus", loop_count=loop_count,
            detail=f"asked about {list(scope.version_mentions)}; corpus holds {held}",
        )
    if not passages:
        return GateDecision(action="EARLY_REFUSAL", reason="no_passages", loop_count=loop_count)
    if scope.out_of_corpus_signal:
        return GateDecision(
            action="EARLY_REFUSAL", reason="low_corpus_coverage", loop_count=loop_count,
            detail=f"no paper cues; corpus term coverage {scope.corpus_term_coverage}",
        )
    best = _best_rerank(passages)
    if not scope.strong_papers and best is not None and best < RERANK_REFUSAL_SCORE:
        return GateDecision(
            action="EARLY_REFUSAL", reason="rerank_evidence_below_threshold",
            loop_count=loop_count,
            detail=f"best rerank score {best:.3f} < {RERANK_REFUSAL_SCORE}",
        )
    if loop_count < MAX_LOOPS:
        present = {p.paper_id for p in passages}
        missing = tuple(p for p in expected_papers(scope) if p not in present)
        if missing:
            return GateDecision(
                action="LOOP_EXPAND", reason="missing_paper_scope", loop_count=loop_count,
                paper_filter=missing, requery=requery(scope.question, missing),
                detail=f"no passage from {list(missing)} in the first result",
            )
    return GateDecision(action="SUFFICIENT", reason="evidence_found", loop_count=loop_count)


def expansion_size(top_k: int) -> int:
    """How many re-query passages may be added to a prompt of ``top_k`` passages."""
    return max(1, top_k // 2)


def merge_expansion(original: Sequence[RetrievedPassage],
                    expansion: Sequence[RetrievedPassage], top_k: int) -> tuple[
                        RetrievedPassage, ...]:
    """Append the best new passages of the paper-scoped re-query to the first result."""
    merged = list(original)
    seen = {p.chunk_id for p in merged}
    limit = len(merged) + expansion_size(top_k)
    for passage in expansion:
        if len(merged) >= limit:
            break
        if passage.chunk_id not in seen:
            merged.append(passage)
            seen.add(passage.chunk_id)
    return tuple(p.model_copy(update={"rank": i}) for i, p in enumerate(merged, start=1))


__all__ = [
    "MAX_LOOPS",
    "RERANK_REFUSAL_SCORE",
    "EvidenceGate",
    "GateDecision",
    "decide",
    "expansion_size",
    "expected_papers",
    "merge_expansion",
    "requery_text",
]
