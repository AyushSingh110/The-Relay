"""Verified streaming: sentences are split, verified and released as tokens arrive (V2-05).

- ``IncrementalSentenceSplitter`` turns a token stream into sentences. It uses the v1
  splitter (``relay.orchestrator.verifier.split_sentences``) on the text so far and
  releases a sentence only once nothing that can still arrive could change it:
  - a sentence is never the last unit (more text may extend it);
  - a sentence with no citation tag is held while the next unit could still start with
    a tag (v1 attaches such tags to the sentence before).

  So the released sentences are always exactly v1's split of the whole answer, which is
  property-tested in ``tests/test_streaming.py``. This is what makes the cases correct:
  tags inside a sentence, decimals (3.14), ellipses, abbreviations (e.g.), list items
  and tags written after the full stop ("... holds. [P1]").
- ``StreamingVerifier`` runs the v1 per-sentence checks (L1 structure, L2 literal
  grounding: tags, paper names, arXiv ids, pages, numbers, identifiers) on one sentence.
  It is the same code the whole-answer verifier runs for each sentence (``SentenceChecker``);
  the whole-answer pass is still run at the end by the caller.
- ``SentenceStream`` consumes tokens and yields ``VerifiedSentence`` events. On the first
  sentence that fails it closes the token generator (cancelling generation) and yields a
  ``StoppedEvent``. A failed sentence is never yielded. A lone refusal marker yields
  ``StreamRefusal``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Final

from relay.errors import ContractViolationError
from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.orchestrator.verifier import (
    LEADING_TAGS,
    REFUSAL_MARKER_PATTERN,
    WORD_CHAR,
    CitedPassage,
    SentenceChecker,
    Verifier,
    VerifierFailure,
    is_refusal_marker,
    split_sentences,
)
from relay.retrieval.hybrid_index import RetrievedPassage

_LEADING_TAGS = LEADING_TAGS
_WORD = WORD_CHAR
# A line that may still become a list item ("1", "(2", "10."): v1 strips the number as a
# bullet, after which a tag at the start of the item moves back onto the sentence before.
_MAYBE_BULLET = re.compile(r"^\(?\d{1,2}[.)]?$")

REASON_CLASSES: Final[dict[str, str]] = {
    "empty_answer": "uncited", "no_citations": "uncited", "uncited_sentence": "uncited",
    "unknown_passage_tag": "unknown_citation",
    "paper_mismatch": "metadata_mismatch", "arxiv_mismatch": "metadata_mismatch",
    "page_mismatch": "metadata_mismatch",
    "ungrounded_number": "ungrounded", "ungrounded_identifier": "ungrounded",
    "refusal_with_content": "refusal_with_content",
}


# --- Splitting ---------------------------------------------------------------------------


def _settled(following: str) -> bool:
    """Can ``following`` (the next unit, as far as it has arrived) no longer change the
    sentence before it? Only citation tags at the start of the next unit can: v1 moves a run of
    leading tags back onto the sentence before, so a sentence that just gained one tag can
    still gain more. Settled once the next unit has words after any complete tags."""
    leading = _LEADING_TAGS.match(following)
    rest = following[leading.end():] if leading else following
    if rest.lstrip().startswith("[") and "]" not in rest:
        return False  # an unfinished bracket: it may still become a tag
    if _MAYBE_BULLET.match(rest.strip()):
        return False  # a list number: the item (maybe starting with a tag) is still coming
    return _WORD.search(rest) is not None


class IncrementalSentenceSplitter:
    def __init__(self) -> None:
        self._text = ""
        self._released: list[str] = []

    @property
    def text(self) -> str:
        return self._text

    def _units(self) -> list[str]:
        units = split_sentences(self._text)
        if units[:len(self._released)] != self._released:  # must never happen
            raise ContractViolationError("streaming split changed a released sentence")
        return units

    def feed(self, token: str) -> list[str]:
        """Add ``token``; return the sentences that became final."""
        self._text += token
        units = self._units()
        new: list[str] = []
        for i in range(len(self._released), len(units) - 1):
            if not _settled(units[i + 1]):
                break
            new.append(units[i])
        self._released += new
        return new

    def flush(self) -> list[str]:
        """End of stream: every sentence not released yet, including an unfinished tail."""
        rest = self._units()[len(self._released):]
        self._released += rest
        return rest


# --- Verification ------------------------------------------------------------------------


@dataclass(frozen=True)
class SentenceVerdict:
    passed: bool
    reason_class: str | None
    confidence: float | None  # L4 entailment (not built yet): always None for now
    failures: tuple[VerifierFailure, ...]
    citations: tuple[CitedPassage, ...]
    claim: bool = True  # False for a lead-in ("... these parts:"): nothing was checked


class StreamingVerifier:
    """Per-sentence L1/L2 checks for one corpus (default the v1 papers)."""

    def __init__(self, verifier: Verifier | None = None) -> None:
        self._verifier = verifier or Verifier(V1_CUE_SET)
        self._checker: tuple[tuple[str, ...], SentenceChecker] | None = None

    def _checker_for(self, passages: Sequence[RetrievedPassage]) -> SentenceChecker:
        key = tuple(p.chunk_id for p in passages)
        if self._checker is None or self._checker[0] != key:
            self._checker = (key, self._verifier.sentence_checker(passages))
        return self._checker[1]

    def verify_sentence(self, sentence: str, passages: Sequence[RetrievedPassage], *,
                        index: int = 1, is_last: bool = False) -> SentenceVerdict:
        checker = self._checker_for(passages)
        check = checker.check(sentence, index=index, is_last=is_last)
        if check.failures:
            reason = REASON_CLASSES.get(check.failures[0].code, "unverified")
            return SentenceVerdict(False, reason, None, check.failures, (), check.claim)
        return SentenceVerdict(True, None, None, (), checker.citations(check.tags),
                               check.claim)


# --- The stream --------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedSentence:
    index: int
    text: str
    citations: tuple[CitedPassage, ...]


@dataclass(frozen=True)
class StoppedEvent:
    reason_class: str
    partial: bool  # verified sentences were already released
    sentence_index: int | None
    failures: tuple[VerifierFailure, ...] = ()


@dataclass(frozen=True)
class StreamRefusal:
    """The model answered with the refusal marker alone."""


StreamEvent = VerifiedSentence | StoppedEvent | StreamRefusal


class SentenceStream:
    """Tokens in; verified sentences (or one stop) out. Iterate it once."""

    def __init__(self, tokens: AsyncIterator[str], passages: Sequence[RetrievedPassage],
                 verifier: StreamingVerifier | None = None) -> None:
        self._tokens = tokens
        self._passages = tuple(passages)
        self._verifier = verifier or StreamingVerifier()
        self._splitter = IncrementalSentenceSplitter()
        self._released = 0
        self._marker_held = False
        self._held: list[VerifiedSentence] = []  # lead-ins waiting for their item
        self.completed = False  # the token stream ended without a stop
        self.sentences: list[VerifiedSentence] = []  # released, in order

    @property
    def text(self) -> str:
        """Everything the model produced so far (the input of the whole-answer pass)."""
        return self._splitter.text

    @property
    def released(self) -> int:
        return self._released

    async def _close_tokens(self) -> None:
        close = getattr(self._tokens, "aclose", None)
        if close is not None:
            await close()

    def _judge(self, unit: str, *, index: int, is_last: bool) -> list[StreamEvent]:
        """The events this unit releases: nothing (held), sentences, or one stop."""
        if self._marker_held:
            return []  # after the marker everything is held; decided at the end
        # v1 looks for the marker in the whole answer (it may span a line break)
        if is_refusal_marker(unit) or is_refusal_marker(self._splitter.text):
            if self._released > 0:
                return [StoppedEvent("refusal_with_content", True, index)]
            self._held.clear()
            self._marker_held = True  # a refusal, unless other content comes with it
            return []
        verdict = self._verifier.verify_sentence(unit, self._passages, index=index,
                                                 is_last=is_last)
        if not verdict.passed:
            return [StoppedEvent(verdict.reason_class or "unverified", self._released > 0,
                                 index, verdict.failures)]
        sentence = VerifiedSentence(index, unit, verdict.citations)
        if not verdict.claim:
            self._held.append(sentence)  # a lead-in goes out only with a verified item
            return []
        released, self._held = [*self._held, sentence], []
        self._released += len(released)
        self.sentences += released
        return list(released)

    def _end_of_marker(self) -> StreamEvent:
        rest = REFUSAL_MARKER_PATTERN.sub("", self._splitter.text)
        if _WORD.search(rest):
            return StoppedEvent("refusal_with_content", False, None)
        return StreamRefusal()

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        index = 0
        try:
            async for token in self._tokens:
                for unit in self._splitter.feed(token):
                    index += 1
                    for event in self._judge(unit, index=index, is_last=False):
                        if isinstance(event, StoppedEvent):
                            await self._close_tokens()  # cancel generation first
                            yield event
                            return
                        yield event
            if getattr(self._tokens, "failed", False):
                # The provider failed: what is left is cut off and is never released (the
                # caller reports the failure).
                self.completed = True
                return
            tail = self._splitter.flush()
            for n, unit in enumerate(tail, start=1):
                index += 1
                for event in self._judge(unit, index=index, is_last=n == len(tail)):
                    yield event
                    if isinstance(event, StoppedEvent):
                        return
            self.completed = True
            if self._marker_held:
                yield self._end_of_marker()
        finally:
            if not self.completed:
                await self._close_tokens()


__all__ = ["REASON_CLASSES", "IncrementalSentenceSplitter", "SentenceStream",
           "SentenceVerdict", "StoppedEvent", "StreamEvent", "StreamRefusal",
           "StreamingVerifier", "VerifiedSentence"]
