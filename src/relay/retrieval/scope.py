"""Deterministic scope analysis (pipeline step 1, ARCHITECTURE.md section 6).

``ScopeAnalyser`` works on one corpus's ``CueSet`` (``relay.retrieval.cues``): a collection's
documents, or the v1 corpus (``relay.ingestion.v1_corpus``), which ``analyze_scope`` uses
when no cue set is given. No paper is named in this module (V2-04).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict

from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.retrieval.cues import CueSet, bounded, normalise
from relay.retrieval.hybrid_index import tokenize

_COMPARISON = re.compile(
    r"\b(compare[sd]?|comparison|differ(?:s|ence|ences|ent)?|versus|vs\.?|contrast|"
    r"both|similarit(?:y|ies)|in common|relative to)\b"
)
_REFERENCES = re.compile(
    r"\b(references?|bibliograph\w*|cite[sd]?|citing|citations?|cited by)\b|\[\d+\]"
)
_ARXIV = re.compile(r"\b(\d{4}\.\d{4,5})(?:v(\d+))?\b")
_IDENTIFIERS = (
    re.compile(r"\b\w+[_^]\w+\b"),  # d_model, 10^20
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:hz|khz|kbps|bps|ms|s|m|b|k|%)(?![a-z])", re.IGNORECASE),
    re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*\b"),  # RVQ, BLEU, WMT
    re.compile(r"\b[A-Za-z]+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b"),  # X-Net, RQ-Transformer
    re.compile(r"\b\d+(?:\.\d+)+\b"),  # 28.4, 0.1
)
OUT_OF_CORPUS_COVERAGE: Final = 0.5
# Question phrasing words: excluded from coverage (they say nothing about the topic).
_QUESTION_WORDS: Final = frozenset(
    [
        "many", "much", "use", "uses", "used", "using", "explain", "describe", "give", "list",
        "tell", "mean", "means", "value", "values", "kind", "kinds", "type", "types", "way",
        "ways",
    ]
)


class ScopeDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    question: str
    strong_papers: tuple[str, ...]
    weak_papers: tuple[str, ...]
    suggested_paper_filter: tuple[str, ...] | None
    comparison: bool
    asks_references: bool
    identifiers: tuple[str, ...]
    version_mentions: tuple[str, ...]
    version_mismatch: bool
    content_terms: tuple[str, ...]
    corpus_term_coverage: float | None
    out_of_corpus_signal: bool
    reasons: tuple[str, ...]


Compiled = tuple[tuple[str, re.Pattern[str]], ...]


def _compile(cues: Mapping[str, tuple[str, ...]]) -> dict[str, Compiled]:
    return {paper_id: tuple((p, bounded(p)) for p in patterns)
            for paper_id, patterns in cues.items()}


def _matches(text: str, patterns: Iterable[tuple[str, re.Pattern[str]]]) -> list[str]:
    return [source for source, compiled in patterns if compiled.search(text)]


class ScopeAnalyser:
    """Scope analysis for one corpus. The cue set is read once, at construction."""

    def __init__(self, cue_set: CueSet) -> None:
        self.cue_set = cue_set
        self._strong: Mapping[str, Compiled] = _compile(cue_set.strong_cues())
        self._weak: Mapping[str, Compiled] = _compile(cue_set.weak_cues())
        self._pinned: Mapping[str, int] = cue_set.pinned_versions()

    def analyze(self, question: str, vocabulary: frozenset[str] | None = None) -> ScopeDecision:
        text = normalise(question)
        reasons: list[str] = []
        strong: list[str] = []
        weak: list[str] = []
        for paper_id, patterns in self._strong.items():
            hits = _matches(text, patterns)
            if hits:
                strong.append(paper_id)
                reasons.append(f"strong cue for {paper_id}: {hits}")
        for paper_id, patterns in self._weak.items():
            hits = _matches(text, patterns)
            if hits and paper_id not in strong:
                weak.append(paper_id)
                reasons.append(f"weak (shared-vocabulary) cue for {paper_id}: {hits}")

        comparison = bool(_COMPARISON.search(text)) or len(strong) >= 2
        if comparison:
            reasons.append("comparison intent" if _COMPARISON.search(text)
                           else "several papers named")
        asks_references = bool(_REFERENCES.search(text))
        if asks_references:
            reasons.append("question is about references/citations")

        mentions: list[str] = []
        mismatch = False
        for arxiv_id, version in _ARXIV.findall(text):
            mentions.append(f"{arxiv_id}v{version}" if version else arxiv_id)
            pinned = self._pinned.get(arxiv_id)
            if version and pinned is not None and int(version) != pinned:
                mismatch = True
                reasons.append(f"asks about {arxiv_id}v{version}; corpus holds v{pinned} only")

        identifiers: list[str] = []
        for pattern in _IDENTIFIERS:
            for match in pattern.findall(question):
                token = match if isinstance(match, str) else match[0]
                if token not in identifiers:
                    identifiers.append(token)

        if len(strong) == 1:
            suggested: tuple[str, ...] | None = (strong[0],)
        elif len(strong) >= 2:
            suggested = tuple(sorted(strong))
        else:
            suggested = None

        terms = tuple(t for t in dict.fromkeys(tokenize(question)) if t not in _QUESTION_WORDS)
        coverage: float | None = None
        if vocabulary is not None and terms:
            coverage = round(sum(1 for t in terms if t in vocabulary) / len(terms), 4)
        out_signal = (
            not strong and not weak and coverage is not None
            and coverage < OUT_OF_CORPUS_COVERAGE
        )
        if out_signal:
            reasons.append(f"no paper cues and low corpus term coverage ({coverage})")
        return ScopeDecision(
            question=question,
            strong_papers=tuple(sorted(strong)),
            weak_papers=tuple(sorted(weak)),
            suggested_paper_filter=suggested,
            comparison=comparison,
            asks_references=asks_references,
            identifiers=tuple(identifiers),
            version_mentions=tuple(mentions),
            version_mismatch=mismatch,
            content_terms=terms,
            corpus_term_coverage=coverage,
            out_of_corpus_signal=out_signal,
            reasons=tuple(reasons),
        )


_V1_ANALYSER: Final = ScopeAnalyser(V1_CUE_SET)


def analyze_scope(question: str, vocabulary: frozenset[str] | None = None, *,
                  cues: CueSet | None = None) -> ScopeDecision:
    """``ScopeAnalyser(cues).analyze``; without ``cues``, the v1 corpus."""
    analyser = _V1_ANALYSER if cues is None else ScopeAnalyser(cues)
    return analyser.analyze(question, vocabulary)


__all__ = ["OUT_OF_CORPUS_COVERAGE", "ScopeAnalyser", "ScopeDecision", "analyze_scope"]
