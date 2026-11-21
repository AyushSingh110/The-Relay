"""Deterministic answer verifier (pipeline step 5; ARCHITECTURE.md section 7.3).

``Verifier`` works on one corpus's ``CueSet`` (V2-04). Its paper-name check uses the names
of the corpus's documents (their non-numeric strong cues: titles, short names, aliases)
and, for isolation between collections, the names of ``foreign`` documents: the tenant's
documents outside this collection. A sentence that names a document but cites no passage
from it is blocked as ``paper_mismatch``. ``verify`` uses the v1 corpus. No paper is named
in this module.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.orchestrator.prompt import passage_metadata
from relay.retrieval.cues import CueSet, DocumentCues, bounded
from relay.retrieval.hybrid_index import RetrievedPassage

Verdict = Literal["VERIFIED", "REFUSAL_VERIFIED", "BLOCKED"]
Check = Literal["a", "b", "c", "d", "e", "f"]  # f: L3 refusal form (V2-07)
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
    "refusal_as_claim",
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

# L3 (V2-07): a sentence saying the passages do not contain something is a refusal, not a
# claim, whatever it cites. Every branch names the retrieved sources explicitly ("the
# (provided) passages / documents / excerpts"); "context" and "text" count only with a
# qualifier ("the provided context") or before a reporting verb ("the context does not
# specify"), so claims such as "X cannot be determined from context in advance", "not
# addressed in the context of this architecture" or "no information is provided to the
# decoder" are not mistaken for refusals.
_QUALIFIER = r"(?:provided|given|supplied|retrieved|cited|available|above)\s+"
_NOUN = r"(?:passages?|documents?|excerpts?)"
_SUBJECT = rf"\bthe\s+(?:{_QUALIFIER})?{_NOUN}(?:\s+(?:provided|given|supplied|above))?"
_SOURCE = rf"(?:the\s+(?:{_QUALIFIER})?{_NOUN}\b|the\s+{_QUALIFIER}(?:context|texts?)\b)"
_REPORTS = (r"(?:state|specif(?:y|ies)|mention|provide|contain|include|say|give|report"
            r"|describe|discuss|address|explain|indicate|offer|detail|cover)s?")
_PARTICIPLE = (r"(?:mentioned|stated|provided|specified|given|reported|described|discussed"
               r"|addressed|found|included|covered|detailed|explained|indicated)")
_REFUSAL_FORM = re.compile("|".join((
    rf"{_SUBJECT}\s+(?:do|does|did)\s*n[o']t\b",
    rf"{_SUBJECT}\s+contains?\s+no\b",
    rf"\bthe\s+(?:{_QUALIFIER})?passages?(?:\s+(?:provided|given))?\s+(?:cannot|can't|can\s+not)\b",
    rf"\bthe\s+(?:{_QUALIFIER})?(?:context|texts?)\s+(?:do|does|did)\s*n[o']t\s+(?:\w+ly\s+)?"
    rf"{_REPORTS}\b",
    rf"\bnot\s+(?:\w+ly\s+)?{_PARTICIPLE}\s+(?:in|by|within)\s+{_SOURCE}",
    rf"\b(?:do|does|did)\s+not\s+appear\s+(?:in|within)\s+{_SOURCE}",
    r"\bno\s+information\s+(?:is\s+|was\s+)?(?:provided|given|available)\s+"
    r"(?:about|on|regarding|concerning|for)\b",
    rf"\bthere(?:\s+is|'s)\s+no\s+(?:information|mention|data|details?|indication)\s+"
    rf"(?:in|within)\s+{_SOURCE}",
    rf"\bnone\s+of\s+{_SOURCE}",
    rf"\bcannot\s+be\s+determined\s+from\s+{_SOURCE}",
    rf"\b(?:based\s+on|from|according\s+to)\s+{_SOURCE}\s*,?\s*it\s+is\s+not\s+possible\b",
)), re.IGNORECASE)

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


# --- L4: entailment (advisory, V2-07) --------------------------------------------------


class EntailmentScore(Protocol):
    """Anything that scores how well a passage supports a sentence (0.0 to 1.0)."""

    def score(self, sentence: str, passage: str) -> float: ...


def entailment_probability(logits: Sequence[float], index: int) -> float:
    """The softmax probability of class ``index`` (numerically stable)."""
    top = max(logits)
    weights = [math.exp(x - top) for x in logits]
    return weights[index] / sum(weights)


WINDOW_SENTENCES: Final = 2
MAX_WINDOWS: Final = 64  # one batch per (sentence, passage) stays bounded


def passage_windows(passage: str, size: int = WINDOW_SENTENCES) -> list[str]:
    """The passage as overlapping windows of ``size`` consecutive sentences (V2-08: NLI
    models are trained on short premises; whole passages scored ~0.05 whatever the label,
    2-sentence windows separated the V2-07 calibration labels, AUROC 0.756 vs 0.530). A very
    long passage gets wider windows, so at most ``MAX_WINDOWS`` still cover all of it."""
    parts = split_sentences(passage)
    if len(parts) <= size:
        return [" ".join(parts) if parts else passage]
    if len(parts) - size + 1 > MAX_WINDOWS:
        size = math.ceil((len(parts) - 1) / MAX_WINDOWS) + 1
    step = 1 if size == WINDOW_SENTENCES else size - 1
    return [" ".join(parts[i:i + size])
            for i in range(0, len(parts) - size + step, step)][:MAX_WINDOWS]


SCORED_WINDOWS: Final = 2
_STOPWORDS: Final = frozenset(
    ["the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "by", "is", "are",
     "was", "were", "be", "been", "this", "that", "these", "those", "it", "its", "as", "at",
     "from", "which", "into", "than", "then", "also", "can", "not", "has", "have", "had"])


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2
            and w not in _STOPWORDS}


def best_windows(sentence: str, windows: Sequence[str], k: int = SCORED_WINDOWS) -> list[str]:
    """The ``k`` windows sharing the most content words with the sentence (ties keep the
    passage order). V2-08 measurement on the V2-07 labels: the top 2 separate as well as
    all windows (AUROC 0.771 vs 0.768) at a quarter of the latency (178 vs 685 ms p50)."""
    words = _content_words(sentence)
    ranked = sorted(range(len(windows)), key=lambda i: -len(words & _content_words(windows[i])))
    return [windows[i] for i in ranked[:k]]


class EntailmentScorer:
    """L4: P(the passage entails the sentence) from a local ONNX NLI cross-encoder
    (owner-approved model; onnxruntime + tokenizers, already installed via fastembed).

    The sentence is scored against the 2-sentence windows of the passage that share the
    most words with it (``best_windows``), in one batched ONNX call; the best window counts
    (V2-08). ``model_dir`` holds
    ``config.json`` (its ``id2label`` gives the entailment class), ``tokenizer.json`` and
    ``model_file``. Loading takes a few seconds: build it once at start-up. ``score`` is
    blocking CPU work: call it from a worker thread. One ONNX session is safe to share
    between threads; each call uses at most ``MAX_THREADS`` ONNX threads, so concurrent
    requests do not starve the CPU."""

    MAX_TOKENS: Final = 512
    MAX_THREADS: Final = 4

    def __init__(self, model_dir: Path, *, model_file: str = "onnx/model.onnx",
                 threads: int = 1) -> None:
        import onnxruntime  # heavy: imported only when L4 is used
        from tokenizers import Tokenizer

        config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
        labels = {int(k): str(v).lower() for k, v in config["id2label"].items()}
        found = [i for i, name in labels.items() if name.startswith("entail")]
        if len(found) != 1:
            raise ValueError(f"{model_dir}: no single entailment label in {labels}")
        self.entailment_index = found[0]
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=self.MAX_TOKENS)
        pad = next((t for t in ("<pad>", "[PAD]") if self._tokenizer.token_to_id(t)
                    is not None), None)
        if pad is None:
            raise ValueError(f"{model_dir}: the tokenizer has no padding token")
        self._tokenizer.enable_padding(pad_id=self._tokenizer.token_to_id(pad), pad_token=pad)
        self.threads = max(1, min(threads, self.MAX_THREADS))
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = self.threads
        self._session = onnxruntime.InferenceSession(
            str(model_dir / model_file), sess_options=options,
            providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}

    def score(self, sentence: str, passage: str) -> float:
        import numpy as np

        windows = best_windows(sentence, passage_windows(passage))
        encodings = self._tokenizer.encode_batch(  # (premise, hypothesis) per window
            [(window, sentence) for window in windows])
        feed = {"input_ids": [e.ids for e in encodings],
                "attention_mask": [e.attention_mask for e in encodings],
                "token_type_ids": [e.type_ids for e in encodings]}
        arrays = {name: np.array(values, dtype=np.int64)
                  for name, values in feed.items() if name in self._inputs}
        logits = self._session.run(None, arrays)[0]  # one call, one row per window
        return float(max(entailment_probability([float(x) for x in row],
                                                self.entailment_index) for row in logits))


def is_refusal_form(text: str) -> bool:
    """L3: the sentence says the passages do not contain the answer (a refusal in prose)."""
    return _REFUSAL_FORM.search(_TAG.sub(" ", text)) is not None


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
    checker = SentenceChecker(passages, name_cues=name_cues, titles=titles)
    checks = [checker.check(unit, index=index, is_last=index == len(units))
              for index, unit in enumerate(units, start=1)]
    # L3: an answer made only of refusal-shaped sentences is a refusal in prose (owner
    # decision 2026-11-20); one among claims fails like any other sentence (BLOCKED).
    if any(c.refusal for c in checks) and not any(c.claim for c in checks):
        return VerificationResult(verdict="REFUSAL_VERIFIED", failures=(), citations=(),
                                  sentences=len(units), claim_sentences=0)
    if not any(_tags(unit) for unit in units):
        return _blocked([VerifierFailure(check="a", code="no_citations", sentence=None,
                                         detail="the answer cites no passage")], len(units))

    failures: list[VerifierFailure] = []
    cited_order: list[int] = []
    claims = 0
    for check in checks:
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
    """The checks of one sentence-like unit (L1 structure, L2 literal grounding, L3 refusal
    form)."""

    failures: tuple[VerifierFailure, ...]
    tags: tuple[int, ...]  # the valid passage tags it cites, in order
    claim: bool  # False for a lead-in, a unit with no words, or a refusal (nothing to check)
    refusal: bool = False  # L3: the unit says the passages do not contain the answer


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
        if not _WORD_CHAR.search(body):
            return UnitCheck(tuple(failures), valid, claim=False)
        if _REFUSAL_FORM.search(body):  # L3: a refusal, whatever it cites (it is no claim)
            return UnitCheck((VerifierFailure(
                check="f", code="refusal_as_claim", sentence=index,
                detail=f"refusal-shaped sentence: {body[:120]!r}"),), valid, claim=False,
                refusal=True)
        if _is_lead_in(body, is_last):
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
    "EntailmentScore",
    "EntailmentScorer",
    "SentenceChecker",
    "UnitCheck",
    "VerificationResult",
    "Verifier",
    "VerifierFailure",
    "LEADING_TAGS",
    "REFUSAL_MARKER_PATTERN",
    "TAG_PATTERN",
    "WORD_CHAR",
    "entailment_probability",
    "best_windows",
    "passage_windows",
    "is_refusal_form",
    "is_refusal_marker",
    "split_sentences",
    "verify",
]
