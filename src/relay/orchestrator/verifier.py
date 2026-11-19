"""Deterministic answer verifier (pipeline step 5; ARCHITECTURE.md section 7.3).

``Verifier`` works on one corpus's ``CueSet`` (V2-04). Its paper-name check uses the names
of the corpus's documents (their non-numeric strong cues: titles, short names, aliases)
and, for isolation between collections, the names of ``foreign`` documents: the tenant's
documents outside this collection. A sentence that names a document but cites no passage
from it is blocked as ``paper_mismatch``. ``verify`` uses the v1 corpus. No paper is named
in this module.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.orchestrator.prompt import passage_metadata
from relay.retrieval.cues import CueSet, DocumentCues, bounded
from relay.retrieval.hybrid_index import RetrievedPassage

Verdict = Literal["VERIFIED", "REFUSAL_VERIFIED", "BLOCKED"]
Check = Literal["a", "b", "c", "d", "e"]
FailureCode = Literal[
    "empty_answer",
    "no_citations",
    "uncited_sentence",
    "unknown_passage_tag",
    "paper_mismatch",
    "arxiv_mismatch",
    "page_mismatch",
    "ungrounded_number",
    "ungrounded_identifier",
    "refusal_with_content",
]

LEAD_IN_MAX_WORDS: Final = 15
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")

_MARKER = re.compile(r"insufficient[\s_]+evidence", re.IGNORECASE)
_TAG_BODY = r"\[\s*P\s*\d+(?:\s*[,;]\s*P?\s*\d+)*\s*\]"
_TAG = re.compile(_TAG_BODY, re.IGNORECASE)
_LEADING_TAGS = re.compile(rf"^(?:{_TAG_BODY}[\s,;.]*)+", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•–]\s+|\(?\d{1,2}[.)]\s+|#{1,6}\s+)")
_BOUNDARY = re.compile(r"[.!?]+[\"')]*\s+")
_ABBREVIATIONS: Final = frozenset(
    {"e.g.", "i.e.", "al.", "fig.", "figs.", "eq.", "eqs.", "sec.", "vs.", "cf.", "approx.",
     "resp.", "no."}
)
_INITIAL = re.compile(r"^[A-Z]\.$")
_WORD_CHAR = re.compile(r"[A-Za-z0-9]")

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ID_SCRIPT = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_^](?:\{[^{}\s]{1,20}\}|[A-Za-z0-9]+))+")
_ID_ALNUM = re.compile(r"(?<![A-Za-z0-9_^{])[A-Za-z]+-?\d+[A-Za-z0-9]*(?![A-Za-z0-9])")
_ARXIV = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v(\d+))?(?![\d])")
_PAGE = re.compile(r"\b(?:pp?\.|pages?)\s*(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?", re.IGNORECASE)
_STRIP_CHARS = str.maketrans("", "", "{}\\$*`")
NameCues = tuple[re.Pattern[str], ...]


class VerifierFailure(BaseModel):
    model_config = _STRICT

    check: Check
    code: FailureCode
    sentence: int | None  # 1-based index of the offending sentence
    detail: str


class CitedPassage(BaseModel):
    model_config = _STRICT

    tag: int
    chunk_id: str
    paper_id: str
    page_start: int
    page_end: int


class VerificationResult(BaseModel):
    model_config = _STRICT

    verdict: Verdict
    failures: tuple[VerifierFailure, ...]
    citations: tuple[CitedPassage, ...]
    sentences: int
    claim_sentences: int

    @property
    def passed(self) -> bool:
        return self.verdict != "BLOCKED"


# --- Text handling --------------------------------------------------------------------


def _canon(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().translate(_STRIP_CHARS)


def _loose(text: str) -> str:
    return text.replace("_", "").replace("^", "")


def _bounded(needle: str, haystack: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _split_line(line: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(line):
        following = line[match.end():match.end() + 1]
        before = line[start:match.start() + 1].split()
        last = before[-1] if before else ""
        if following.islower() or last.casefold() in _ABBREVIATIONS or _INITIAL.match(last):
            continue
        parts.append(line[start:match.end()].strip())
        start = match.end()
    tail = line[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def split_sentences(answer: str) -> list[str]:
    """Sentence-like units: lines and list items, then sentences within each line."""
    units: list[str] = []
    for raw_line in answer.splitlines():
        line = _BULLET.sub("", raw_line).replace("*", "").replace("`", "").strip()
        if _WORD_CHAR.search(line):
            units.extend(_split_line(line))
    fixed: list[str] = []
    for unit in units:
        leading = _LEADING_TAGS.match(unit)
        if leading and fixed and not _TAG.search(fixed[-1]):
            fixed[-1] = f"{fixed[-1]} {leading.group(0).strip()}"
            unit = unit[leading.end():].strip()
            if not unit:
                continue
        fixed.append(unit)
    return fixed


def _tags(unit: str) -> list[int]:
    return [int(n) for group in _TAG.findall(unit) for n in re.findall(r"\d+", group)]


def _numbers(text: str) -> set[str]:
    cleaned = _THOUSANDS.sub("", unicodedata.normalize("NFKC", text))
    found: set[str] = set()
    for token in _NUMBER.findall(cleaned):
        try:
            found.add(format(Decimal(token).normalize(), "f"))
        except InvalidOperation:  # cannot happen for this pattern; never drop a number silently
            found.add(token)
    return found


def _identifiers(text: str) -> list[str]:
    cleaned = unicodedata.normalize("NFKC", text)
    found = [m.group(0) for m in _ID_SCRIPT.finditer(cleaned)]
    found += [m.group(0) for m in _ID_ALNUM.finditer(cleaned)]
    return list(dict.fromkeys(found))


def _is_lead_in(body: str, is_last: bool) -> bool:
    """A short heading-like line ending in ':' that introduces cited items."""
    return (
        not is_last
        and body.rstrip().endswith(":")
        and len(body.split()) <= LEAD_IN_MAX_WORDS
        and not re.search(r"\d", body)
        and not _identifiers(body)
    )


# --- Per-sentence checks --------------------------------------------------------------


def _metadata_failures(index: int, body: str, cited: Sequence[RetrievedPassage],
                       texts: Sequence[str],
                       name_cues: Mapping[str, NameCues]) -> list[VerifierFailure]:
    failures: list[VerifierFailure] = []
    lowered = _canon(body)
    cited_papers = {p.paper_id for p in cited}
    for paper_id, patterns in name_cues.items():
        if paper_id in cited_papers:
            continue
        for pattern in patterns:
            hit = pattern.search(lowered)
            if hit and not any(hit.group(0) in text for text in texts):
                failures.append(VerifierFailure(
                    check="c", code="paper_mismatch", sentence=index,
                    detail=f"mentions {hit.group(0)!r} ({paper_id}) but cites no passage from it",
                ))
                break
    for arxiv_id, version in _ARXIV.findall(body):
        literal = f"{arxiv_id}v{version}" if version else arxiv_id
        same_paper = [p for p in cited if p.arxiv_id == arxiv_id]
        version_ok = not version or any(p.version == int(version) for p in same_paper)
        if (not same_paper or not version_ok) and not any(literal in text for text in texts):
            failures.append(VerifierFailure(
                check="c", code="arxiv_mismatch", sentence=index,
                detail=f"arXiv {literal} does not match the cited passages",
            ))
    for match in _PAGE.finditer(body):
        pages = [int(g) for g in match.groups() if g]
        covered = all(any(p.page_start <= n <= p.page_end for p in cited) for n in pages)
        if not covered and not any(_canon(match.group(0)) in text for text in texts):
            failures.append(VerifierFailure(
                check="c", code="page_mismatch", sentence=index,
                detail=f"{match.group(0)!r} is outside the cited passages' pages",
            ))
    return failures


def _grounding_failures(index: int, body: str, haystacks: Sequence[str]) -> list[VerifierFailure]:
    failures: list[VerifierFailure] = []
    known_numbers: set[str] = set()
    for haystack in haystacks:
        known_numbers |= _numbers(haystack)
    missing = sorted(_numbers(body) - known_numbers)
    if missing:
        failures.append(VerifierFailure(
            check="d", code="ungrounded_number", sentence=index,
            detail=f"numbers not in the cited passages: {missing}",
        ))
    loose_haystacks = [_loose(h) for h in haystacks]
    unknown = []
    for identifier in _identifiers(body):
        canon = _canon(identifier)
        if any(_bounded(canon, h) for h in haystacks):
            continue
        if any(_bounded(_loose(canon), h) for h in loose_haystacks):
            continue
        unknown.append(identifier)
    if unknown:
        failures.append(VerifierFailure(
            check="d", code="ungrounded_identifier", sentence=index,
            detail=f"identifiers not in the cited passages: {unknown}",
        ))
    return failures


# --- Entry point ----------------------------------------------------------------------


def _blocked(failures: Sequence[VerifierFailure], sentences: int = 0,
             claims: int = 0) -> VerificationResult:
    return VerificationResult(verdict="BLOCKED", failures=tuple(failures), citations=(),
                              sentences=sentences, claim_sentences=claims)


class Verifier:
    """The verifier for one corpus. The cue sets are read (and compiled) once.

    Own documents are named by all their non-numeric strong cues (v1 behaviour). A foreign
    document is named only by its title and short names, never its aliases or topics (an
    alias can be an ordinary word in another collection), and a name that one of this
    corpus's own documents also uses is never treated as foreign."""

    def __init__(self, cue_set: CueSet, *, foreign: Sequence[DocumentCues] = ()) -> None:
        self.cue_set = cue_set
        names: dict[str, tuple[str, ...]] = dict(cue_set.name_cues())
        own = {p for d in cue_set.documents for p in (*d.strong, *d.name_patterns())}
        for document in foreign:
            if document.paper_id in cue_set.paper_ids or document.paper_id in names:
                continue
            patterns = tuple(p for p in document.name_patterns() if p not in own)
            if patterns:
                names[document.paper_id] = patterns
        self._names: Mapping[str, NameCues] = {
            paper_id: tuple(bounded(p) for p in patterns) for paper_id, patterns in names.items()}
        self._titles: Mapping[str, str] = cue_set.titles_by_paper()

    def verify(self, answer: str, passages: Sequence[RetrievedPassage]) -> VerificationResult:
        """Check a buffered answer against the passages that were supplied to the model."""
        return _verify(answer, passages, name_cues=self._names, titles=self._titles)

    def sentence_checker(self, passages: Sequence[RetrievedPassage]) -> SentenceChecker:
        """Per-sentence checks with this corpus's names and titles (streaming, V2-05)."""
        return SentenceChecker(passages, name_cues=self._names, titles=self._titles)


_V1_VERIFIER: Final = Verifier(V1_CUE_SET)


# Shared with the streaming splitter, which must treat tags and the marker exactly alike.
LEADING_TAGS: Final = _LEADING_TAGS
TAG_PATTERN: Final = _TAG  # one whole tag, e.g. [P1, P2]
WORD_CHAR: Final = _WORD_CHAR
REFUSAL_MARKER_PATTERN: Final = _MARKER


def is_refusal_marker(text: str) -> bool:
    """True when ``text`` contains the model's refusal marker."""
    return _MARKER.search(text) is not None


def verify(answer: str, passages: Sequence[RetrievedPassage]) -> VerificationResult:
    """``Verifier.verify`` for the v1 corpus."""
    return _V1_VERIFIER.verify(answer, passages)


def _verify(answer: str, passages: Sequence[RetrievedPassage], *,
            name_cues: Mapping[str, NameCues],
            titles: Mapping[str, str]) -> VerificationResult:
    if _MARKER.search(answer):
        if _WORD_CHAR.search(_MARKER.sub("", answer)):
            return _blocked([VerifierFailure(
                check="e", code="refusal_with_content", sentence=None,
                detail="refusal marker combined with other content",
            )])
        return VerificationResult(verdict="REFUSAL_VERIFIED", failures=(), citations=(),
                                  sentences=0, claim_sentences=0)
    units = split_sentences(answer)
    if not units:
        return _blocked([VerifierFailure(check="a", code="empty_answer", sentence=None,
                                         detail="the answer has no content")])
    if not any(_tags(unit) for unit in units):
        return _blocked([VerifierFailure(check="a", code="no_citations", sentence=None,
                                         detail="the answer cites no passage")], len(units))

    checker = SentenceChecker(passages, name_cues=name_cues, titles=titles)
    failures: list[VerifierFailure] = []
    cited_order: list[int] = []
    claims = 0
    for index, unit in enumerate(units, start=1):
        check = checker.check(unit, index=index, is_last=index == len(units))
        failures += check.failures
        cited_order.extend(t for t in check.tags if t not in cited_order)
        claims += check.claim

    if failures:
        return _blocked(failures, len(units), claims)
    return VerificationResult(verdict="VERIFIED", failures=(),
                              citations=checker.citations(cited_order),
                              sentences=len(units), claim_sentences=claims)


@dataclass(frozen=True)
class UnitCheck:
    """The checks of one sentence-like unit (L1 structure, L2 literal grounding)."""

    failures: tuple[VerifierFailure, ...]
    tags: tuple[int, ...]  # the valid passage tags it cites, in order
    claim: bool  # False for a lead-in or a unit with no words (nothing to check)


class SentenceChecker:
    """Per-sentence checks against fixed passages: the single implementation used by the
    whole-answer verifier and by the streaming verifier (one sentence at a time)."""

    def __init__(self, passages: Sequence[RetrievedPassage], *,
                 name_cues: Mapping[str, NameCues], titles: Mapping[str, str]) -> None:
        self._passages = tuple(passages)
        self._names = name_cues
        self._haystacks = {tag: _canon(f"{passage_metadata(p, titles)}\n{p.text}")
                           for tag, p in enumerate(self._passages, start=1)}
        self._texts = {tag: _canon(p.text) for tag, p in enumerate(self._passages, start=1)}

    def check(self, unit: str, *, index: int, is_last: bool) -> UnitCheck:
        failures: list[VerifierFailure] = []
        tags = _tags(unit)
        body = _TAG.sub(" ", unit).strip()
        unknown = sorted({t for t in tags if t not in self._haystacks})
        if unknown:
            failures.append(VerifierFailure(
                check="b", code="unknown_passage_tag", sentence=index,
                detail=f"tags {['P' + str(t) for t in unknown]} were not supplied "
                       f"(valid: P1..P{len(self._passages)})",
            ))
        valid = tuple(t for t in dict.fromkeys(tags) if t in self._haystacks)
        if not _WORD_CHAR.search(body) or _is_lead_in(body, is_last):
            return UnitCheck(tuple(failures), valid, claim=False)
        if not valid:
            if not unknown:
                failures.append(VerifierFailure(
                    check="a", code="uncited_sentence", sentence=index,
                    detail=f"no citation: {body[:120]!r}",
                ))
            return UnitCheck(tuple(failures), valid, claim=True)
        cited = [self._passages[t - 1] for t in valid]
        failures += _metadata_failures(index, body, cited, [self._texts[t] for t in valid],
                                       self._names)
        failures += _grounding_failures(index, body, [self._haystacks[t] for t in valid])
        return UnitCheck(tuple(failures), valid, claim=True)

    def citations(self, tags: Sequence[int]) -> tuple[CitedPassage, ...]:
        return tuple(
            CitedPassage(tag=t, chunk_id=p.chunk_id, paper_id=p.paper_id,
                         page_start=p.page_start, page_end=p.page_end)
            for t in tags for p in (self._passages[t - 1],))


__all__ = [
    "CitedPassage",
    "SentenceChecker",
    "UnitCheck",
    "VerificationResult",
    "Verifier",
    "VerifierFailure",
    "LEADING_TAGS",
    "REFUSAL_MARKER_PATTERN",
    "TAG_PATTERN",
    "WORD_CHAR",
    "is_refusal_marker",
    "split_sentences",
    "verify",
]
