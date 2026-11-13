"""Deterministic scope analysis (pipeline step 1, ARCHITECTURE.md section 6)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel, ConfigDict

from relay.ingestion.arxiv_fetcher import PAPERS
from relay.retrieval.hybrid_index import tokenize

ATTENTION, MOSHI, HNET = (p.paper_id for p in PAPERS)

STRONG_CUES: Final[dict[str, tuple[str, ...]]] = {
    ATTENTION: (r"attention is all you need", r"vaswani", r"1706\.03762"),
    MOSHI: (r"moshi", r"mimi", r"helium", r"kyutai", r"inner monologue", r"2410\.00037"),
    HNET: (r"h-?net", r"dynamic chunking", r"2507\.07955", r"hierarchical sequence modell?ing"),
}
WEAK_CUES: Final[dict[str, tuple[str, ...]]] = {
    ATTENTION: (r"transformers?", r"self-attention", r"multi-head attention",
                r"positional encodings?", r"machine translation", r"wmt", r"bleu"),
    MOSHI: (r"speech", r"audio", r"dialogues?", r"full-duplex", r"rvq", r"codec", r"voice"),
    HNET: (r"chunk(?:s|ing)?", r"byte-level", r"tokeni[sz]er-free", r"tokeni[sz]ation",
           r"routing module", r"dechunk\w*"),
}
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
    re.compile(r"\b[A-Za-z]+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b"),  # H-Net, RQ-Transformer
    re.compile(r"\b\d+(?:\.\d+)+\b"),  # 28.4, 0.1
)
_PINNED_VERSION: Final = {p.arxiv_id: p.version for p in PAPERS}
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


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _matches(text: str, patterns: Iterable[str]) -> list[str]:
    return [p for p in patterns if re.search(rf"(?<![a-z0-9]){p}(?![a-z0-9])", text)]


def analyze_scope(question: str, vocabulary: frozenset[str] | None = None) -> ScopeDecision:
    text = _normalise(question)
    reasons: list[str] = []
    strong: list[str] = []
    weak: list[str] = []
    for paper_id, patterns in STRONG_CUES.items():
        hits = _matches(text, patterns)
        if hits:
            strong.append(paper_id)
            reasons.append(f"strong cue for {paper_id}: {hits}")
    for paper_id, patterns in WEAK_CUES.items():
        hits = _matches(text, patterns)
        if hits and paper_id not in strong:
            weak.append(paper_id)
            reasons.append(f"weak (shared-vocabulary) cue for {paper_id}: {hits}")

    comparison = bool(_COMPARISON.search(text)) or len(strong) >= 2
    if comparison:
        reasons.append("comparison intent" if _COMPARISON.search(text) else "several papers named")
    asks_references = bool(_REFERENCES.search(text))
    if asks_references:
        reasons.append("question is about references/citations")

    mentions: list[str] = []
    mismatch = False
    for arxiv_id, version in _ARXIV.findall(text):
        mentions.append(f"{arxiv_id}v{version}" if version else arxiv_id)
        pinned = _PINNED_VERSION.get(arxiv_id)
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
        not strong and not weak and coverage is not None and coverage < OUT_OF_CORPUS_COVERAGE
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


__all__ = ["STRONG_CUES", "WEAK_CUES", "ScopeDecision", "analyze_scope"]
