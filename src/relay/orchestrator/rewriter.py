"""Standalone-question rewriting for follow-ups (plan V2-08; docs/ARCHITECTURE_V2.md §6 step 1,
§8: only standalone questions are cache keys).

A first question (no prior turn) is never rewritten: no model call, no latency. A follow-up
is rewritten from at most the last ``HISTORY_TURNS`` turns, each truncated, so the prompt
never grows with the conversation. Any failure falls back to the original question.

- ``LLMQueryRewriter``: one small model call through the service's guarded generator (the
  ProviderGuard's limits and quota apply): few-shot, temperature 0, at most 64 completion
  tokens, a 1.5 s timeout; the output is cleaned and checked before it is used.
- ``RuleQueryRewriter``: deterministic, no model. Used with the stand-in model (which cannot
  rewrite) and in tests: it resolves "it/its/they/their/them" and "this/that/the
  model/paper/..." to the subject named most recently in the conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from relay.control.pricing import ROUTE_NOTE, LaneUse, RouteNote
from relay.orchestrator.pipeline import AnswerGenerator, GenerationResult
from relay.orchestrator.verifier import TAG_PATTERN
from relay.providers.base import ChatMessage, ChatRequest
from relay.store.repositories import Turn
from relay.telemetry.spans import ServerSpanTracker

HISTORY_TURNS: Final = 2
HISTORY_CHARS: Final = 600  # per message in the rewrite prompt
MAX_COMPLETION_TOKENS: Final = 64
DEFAULT_TIMEOUT_S: Final = 1.5
MAX_QUESTION_CHARS: Final = 300


@dataclass(frozen=True)
class Rewrite:
    question: str  # what retrieval, the cache and SingleFlight use
    original: str  # what the user typed (kept for the conversation history)
    rewritten: bool
    method: str  # "none" (first question), "rules" or "llm"
    fallback: str | None = None  # why the original was kept: timeout, provider_error, ...
    ms: float = 0.0
    # V2-09: the model call's ledger entry (lane, tokens, cost); None when no call was made
    usage: LaneUse | None = None

    @property
    def tokens(self) -> int:
        """Model tokens the rewrite cost (prompt + completion)."""
        return 0 if self.usage is None else self.usage.tokens_in + self.usage.tokens_out


class QueryRewriter(Protocol):
    async def resolve(self, question: str, history: Sequence[Turn]) -> Rewrite: ...


async def rewrite(rewriter: QueryRewriter, question: str, history: Sequence[Turn]) -> str:
    """The standalone question (convenience over ``resolve``)."""
    return (await rewriter.resolve(question, history)).question


def _unchanged(question: str, method: str, *, fallback: str | None = None,
               usage: LaneUse | None = None, started: float | None = None) -> Rewrite:
    ms = 0.0 if started is None else round((time.perf_counter() - started) * 1000, 1)
    return Rewrite(question, question, False, method, fallback, ms, usage)


def _usage(result: GenerationResult, model: str) -> LaneUse | None:
    """The rewrite call's ledger entry: the router's (with its lane and price), else the
    provider's reported usage on an unrouted generator (no price: cost unknown)."""
    if result.route is not None and result.route.uses:
        return result.route.uses[0]
    usage = None if result.completion is None else result.completion.usage
    if usage is None:
        return None
    return LaneUse(lane="unrouted", provider="unknown", model=model, purpose="rewrite",
                   tokens_in=usage.prompt_tokens, tokens_out=usage.completion_tokens,
                   usage_source="provider", cost_usd=None)


def _timed_out_usage(note: RouteNote) -> LaneUse | None:
    """A rewrite cancelled by its timeout: the lane is known, what it spent is not."""
    if note.lane is None or note.provider is None or note.model is None:
        return None  # it never reached a lane
    return LaneUse(lane=note.lane, provider=note.provider, model=note.model,
                   purpose="rewrite", tokens_in=0, tokens_out=0, usage_source="none",
                   cost_usd=None)


# --- rules ---------------------------------------------------------------------------------

_LEADING = re.compile(r"^\s*(?:and|so|also|but|then|ok(?:ay)?)\s*[,:]?\s+", re.IGNORECASE)
_NOUNS = r"(?:model|paper|architecture|method|approach|system|work|technique|network|codec)"
_DETERMINER = re.compile(rf"\b(?:this|that|the)\s+{_NOUNS}\b", re.IGNORECASE)
_POSSESSIVE = re.compile(r"\b(?:its|their)\b", re.IGNORECASE)
_PRONOUN = re.compile(r"\b(?:it|they|them)\b", re.IGNORECASE)
_NAME = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*\b")
_NOT_NAMES: Final = frozenset(
    ["What", "How", "Why", "When", "Where", "Which", "Who", "Whom", "Whose", "Is", "Are",
     "Was", "Were", "Does", "Do", "Did", "Can", "Could", "Would", "Should", "Will", "And",
     "But", "So", "Also", "Then", "The", "A", "An", "In", "On", "Of", "For", "To", "With",
     "By", "From", "This", "That", "These", "Those", "It", "Its", "They", "Their", "Them",
     "I", "We", "You", "Explain", "Describe", "Compare", "Tell", "Give", "List", "Okay",
     "Ok", "Please", "Yes", "No"])


def _subject(text: str) -> str | None:
    text = TAG_PATTERN.sub(" ", text)  # "[P1]" in a stored answer is no name
    for match in _NAME.finditer(text):
        name = match.group(0)
        if name not in _NOT_NAMES and not (match.start() == 0 and name.lower() in
                                           {n.lower() for n in _NOT_NAMES}):
            return name
    return None


def _antecedent(history: Sequence[Turn]) -> str | None:
    """The subject named most recently: the user's questions first (newest first), then the
    answers."""
    recent = list(history)[-HISTORY_TURNS:][::-1]
    for text in [t.user for t in recent] + [t.assistant for t in recent]:
        name = _subject(text)
        if name is not None:
            return name
    return None


class RuleQueryRewriter:
    """Deterministic follow-up resolution (stand-in mode, offline tests); no model call."""

    async def resolve(self, question: str, history: Sequence[Turn]) -> Rewrite:
        if not history:
            return _unchanged(question, "none")
        refers = (_DETERMINER.search(question) or _POSSESSIVE.search(question)
                  or _PRONOUN.search(question))
        name = _antecedent(history) if refers else None
        if name is None:
            return _unchanged(question, "rules")
        text = _LEADING.sub("", question.strip())
        text = _DETERMINER.sub(name, text)
        text = _POSSESSIVE.sub(f"{name}'s", text)
        text = _PRONOUN.sub(name, text)
        text = text[:1].upper() + text[1:]
        return Rewrite(text, question, text != question, "rules")


# --- LLM -----------------------------------------------------------------------------------

_SYSTEM = """You rewrite a follow-up question into one standalone question.
Use the conversation only to resolve references such as "it", "its", "they", "this model" or
"the second paper". Keep everything else exactly as asked. If the question is already
standalone, return it unchanged. Output ONLY the question on one line: no quotes, no
explanation, no answer.

Conversation:
User: How does Moshi achieve full-duplex dialogue?
Assistant: Moshi models its own speech and the user's speech as parallel streams.
Follow-up: And what is its latency?
Standalone question: What is Moshi's latency?

Conversation:
User: What is H-Net?
Assistant: H-Net is a hierarchical network with dynamic chunking.
Follow-up: How many stages does it use?
Standalone question: How many stages does H-Net use?

Conversation:
User: What is the Transformer's model dimension?
Assistant: The model dimension is 512.
Follow-up: What optimizer was used to train BERT?
Standalone question: What optimizer was used to train BERT?"""

_PREFIX = re.compile(r"^\s*(?:standalone question|rewritten question|question)\s*:\s*",
                     re.IGNORECASE)
_DECLINES = re.compile(r"^\s*(?:i\s+(?:cannot|can't|am unable)|sorry|as an ai)",
                       re.IGNORECASE)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= HISTORY_CHARS else text[:HISTORY_CHARS] + " …"


def rewrite_messages(question: str, history: Sequence[Turn]) -> tuple[ChatMessage, ...]:
    """The bounded rewrite prompt: the last ``HISTORY_TURNS`` turns, each message clipped."""
    lines = []
    for turn in list(history)[-HISTORY_TURNS:]:
        lines += [f"User: {_clip(turn.user)}", f"Assistant: {_clip(turn.assistant)}"]
    user = "Conversation:\n" + "\n".join(lines) + \
        f"\nFollow-up: {_clip(question)}\nStandalone question:"
    return (ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user))


def clean_output(text: str, original: str) -> str | None:
    """The first line, without a label or quotes; None when it is not a usable question."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    first = _PREFIX.sub("", first).strip().strip("\"'`“”‘’").strip()
    if not first or len(first) > MAX_QUESTION_CHARS or _DECLINES.match(first):
        return None
    if original.rstrip().endswith("?") and not first.endswith("?"):
        return None  # a question must stay a question
    return first


class LLMQueryRewriter:
    """One small, bounded model call through the guarded generator; falls back on failure."""

    def __init__(self, generator: AnswerGenerator, *, model: str,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_completion_tokens: int = MAX_COMPLETION_TOKENS,
                 reasoning_effort: str | None = None) -> None:
        self._generator = generator
        self._model = model
        self._timeout_s = timeout_s
        self._max_tokens = max_completion_tokens
        self._reasoning_effort = reasoning_effort

    async def resolve(self, question: str, history: Sequence[Turn]) -> Rewrite:
        if not history:
            return _unchanged(question, "none")  # first question: no model call
        started = time.perf_counter()
        request = ChatRequest(model=self._model, messages=rewrite_messages(question, history),
                              max_completion_tokens=self._max_tokens, temperature=0.0,
                              reasoning_effort=self._reasoning_effort)
        note = RouteNote()
        token = ROUTE_NOTE.set(note)  # the router notes the lane, even if the call times out
        try:
            result = await asyncio.wait_for(
                self._generator.generate(request, ServerSpanTracker()), self._timeout_s)
        except TimeoutError:
            return _unchanged(question, "llm", fallback="timeout",
                              usage=_timed_out_usage(note), started=started)
        finally:
            with contextlib.suppress(ValueError):  # set in this context, so reset works
                ROUTE_NOTE.reset(token)
        usage = _usage(result, self._model)
        if result.error is not None or result.completion is None:
            return _unchanged(question, "llm", fallback="provider_error", usage=usage,
                              started=started)
        cleaned = clean_output(result.completion.content, question)
        if cleaned is None:
            return _unchanged(question, "llm", fallback="unusable_output", usage=usage,
                              started=started)
        ms = round((time.perf_counter() - started) * 1000, 1)
        return Rewrite(cleaned, question, cleaned != question, "llm", None, ms, usage)


__all__ = ["DEFAULT_TIMEOUT_S", "HISTORY_CHARS", "HISTORY_TURNS", "LLMQueryRewriter",
           "QueryRewriter", "Rewrite", "RuleQueryRewriter", "clean_output", "rewrite",
           "rewrite_messages"]
