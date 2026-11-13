"""Evidence gate (pipeline step 3): a deterministic branch and one bounded loop.

``decide`` returns one of three actions (ARCHITECTURE.md section 6):

- ``EARLY_REFUSAL``: only when the question is *clearly* outside the corpus, so no LLM
  tokens are spent. The triggers are deliberately narrow:
    * nothing was retrieved;
    * the question asks about a non-pinned arXiv version of a corpus paper;
    * no paper cues at all and low corpus term coverage (the scope analyzer's signal);
    * no strong paper cue and, in a reranked setting, the best cross-encoder score is
      far below anything seen for on-topic questions.
- ``LOOP_EXPAND``: a paper the question expects has no passage in the result. The
  pipeline re-queries once, scoped to the missing paper(s). ``loop_count`` bounds this to
  a single re-query: on the second visit the gate never asks again.
- ``SUFFICIENT``: go to the single LLM call.

Near-topic unanswerable questions pass this gate by design; the LLM's cite-or-refuse
instruction and the verifier handle them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from relay.ingestion.arxiv_fetcher import PAPERS
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.scope import ScopeDecision, analyze_scope

# [PROPOSED] Calibrated on the P04B observations (n = 21 owner questions): off-topic
# questions scored -8.5 or lower, every answerable or near-topic one +0.22 or higher.
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
_PINNED: Final = {p.arxiv_id: p.paper_id for p in PAPERS}


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


def requery_text(question: str, papers: Sequence[str]) -> str:
    """The part of the question that names the missing paper(s); the whole question if
    it cannot be narrowed. Deterministic: no model rewrites the query."""
    wanted = set(papers)
    sentences = [s.strip() for s in _SENTENCE_END.split(question.strip()) if s.strip()]
    if len(sentences) < 2:
        return question
    chosen = []
    for sentence in sentences:
        scope = analyze_scope(sentence)
        if wanted & (set(scope.strong_papers) | set(scope.weak_papers)):
            chosen.append(sentence)
    return " ".join(chosen) if chosen else question


def _best_rerank(passages: Sequence[RetrievedPassage]) -> float | None:
    scores = [p.rerank_score for p in passages if p.rerank_score is not None]
    return max(scores) if scores else None


def decide(scope: ScopeDecision, passages: Sequence[RetrievedPassage], *,
           loop_count: int) -> GateDecision:
    if scope.version_mismatch:
        pinned = sorted({_PINNED[m.split("v")[0]] for m in scope.version_mentions
                         if m.split("v")[0] in _PINNED})
        return GateDecision(
            action="EARLY_REFUSAL", reason="version_not_in_corpus", loop_count=loop_count,
            detail=f"asked about {list(scope.version_mentions)}; corpus holds {pinned}",
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
                paper_filter=missing, requery=requery_text(scope.question, missing),
                detail=f"no passage from {list(missing)} in the first result",
            )
    return GateDecision(action="SUFFICIENT", reason="evidence_found", loop_count=loop_count)


def expansion_size(top_k: int) -> int:
    """How many re-query passages may be added to a prompt of ``top_k`` passages."""
    return max(1, top_k // 2)


def merge_expansion(original: Sequence[RetrievedPassage],
                    expansion: Sequence[RetrievedPassage], top_k: int) -> tuple[
                        RetrievedPassage, ...]:
    """Append the best new passages of the paper-scoped re-query to the first result.

    Nothing already retrieved is dropped (replacing half the first result was measured to
    lose gold evidence); the prompt grows by at most ``expansion_size(top_k)`` passages,
    and only on requests that loop. Passages are renumbered 1..n.
    """
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
    "GateDecision",
    "decide",
    "expansion_size",
    "expected_papers",
    "merge_expansion",
    "requery_text",
]
