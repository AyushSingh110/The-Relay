"""Deterministic answer verifier (pipeline step 5; ARCHITECTURE.md section 7.3)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from relay.orchestrator.prompt import passage_metadata
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.scope import STRONG_CUES

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
_NAME_CUES: Final = {
    paper_id: tuple(p for p in patterns if not re.search(r"\d", p))
    for paper_id, patterns in STRONG_CUES.items()
}


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
                       texts: Sequence[str]) -> list[VerifierFailure]:
    failures: list[VerifierFailure] = []
    lowered = _canon(body)
    cited_papers = {p.paper_id for p in cited}
    for paper_id, patterns in _NAME_CUES.items():
        if paper_id in cited_papers:
            continue
        for pattern in patterns:
            hit = re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", lowered)
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


def verify(answer: str, passages: Sequence[RetrievedPassage]) -> VerificationResult:
    """Check a buffered answer against the passages that were supplied to the model."""
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

    haystack_by_tag = {
        tag: _canon(f"{passage_metadata(p)}\n{p.text}")
        for tag, p in enumerate(passages, start=1)
    }
    text_by_tag = {tag: _canon(p.text) for tag, p in enumerate(passages, start=1)}
    failures: list[VerifierFailure] = []
    cited_order: list[int] = []
    claims = 0
    for index, unit in enumerate(units, start=1):
        tags = _tags(unit)
        body = _TAG.sub(" ", unit).strip()
        unknown = sorted({t for t in tags if t not in haystack_by_tag})
        if unknown:
            failures.append(VerifierFailure(
                check="b", code="unknown_passage_tag", sentence=index,
                detail=f"tags {['P' + str(t) for t in unknown]} were not supplied "
                       f"(valid: P1..P{len(passages)})",
            ))
        valid = [t for t in dict.fromkeys(tags) if t in haystack_by_tag]
        cited_order.extend(t for t in valid if t not in cited_order)
        if not _WORD_CHAR.search(body) or _is_lead_in(body, index == len(units)):
            continue
        claims += 1
        if not valid:
            if not unknown:
                failures.append(VerifierFailure(
                    check="a", code="uncited_sentence", sentence=index,
                    detail=f"no citation: {body[:120]!r}",
                ))
            continue
        cited = [passages[t - 1] for t in valid]
        failures += _metadata_failures(index, body, cited, [text_by_tag[t] for t in valid])
        failures += _grounding_failures(index, body, [haystack_by_tag[t] for t in valid])

    if failures:
        return _blocked(failures, len(units), claims)
    citations = tuple(
        CitedPassage(tag=t, chunk_id=passages[t - 1].chunk_id, paper_id=passages[t - 1].paper_id,
                     page_start=passages[t - 1].page_start, page_end=passages[t - 1].page_end)
        for t in cited_order
    )
    return VerificationResult(verdict="VERIFIED", failures=(), citations=citations,
                              sentences=len(units), claim_sentences=claims)


__all__ = [
    "CitedPassage",
    "VerificationResult",
    "VerifierFailure",
    "split_sentences",
    "verify",
]
