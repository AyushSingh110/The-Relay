"""Provider-neutral request/response contracts, the provider protocol, the real-call cap
and an offline ``ScriptedProvider``.

A streamed call yields, in order:

    ResponseOpened  ->  (ReasoningDelta | ContentDelta)*  ->  UsageReported?  ->  StreamCompleted

or raises a ``ProviderError`` part-way. Every event carries ``at_ns`` from the
provider's monotonic clock; ``ResponseOpened.sent_ns`` is taken just before the request
was sent, so time-to-first-token can be computed without a second clock.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from relay.providers.errors import (
    ProviderError,
    ProviderProtocolError,
    RealCallBudgetExceededError,
    ScriptExhaustedError,
)
from relay.providers.headers import RateLimitSnapshot, ResponseMeta
from relay.schemas import Source
from relay.telemetry.spans import MonotonicClock, ns_to_ms

_STRICT_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")
NonNegInt = Annotated[int, Field(ge=0)]
NonNegFloat = Annotated[float, Field(ge=0.0)]


# --- Requests ------------------------------------------------------------------------


class ChatMessage(BaseModel):
    model_config = _STRICT_FROZEN

    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """One chat-completions request. Optional fields are omitted from the payload when unset."""

    model_config = _STRICT_FROZEN

    model: Annotated[str, Field(min_length=1, max_length=128)]
    messages: Annotated[tuple[ChatMessage, ...], Field(min_length=1)]
    max_completion_tokens: Annotated[int, Field(ge=1)]
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    seed: int | None = None
    reasoning_effort: str | None = None
    reasoning_format: str | None = None
    include_reasoning: bool | None = None

    @property
    def prompt_chars(self) -> int:
        return sum(len(message.content) for message in self.messages)

    def to_payload(self, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump() for message in self.messages],
            "max_completion_tokens": self.max_completion_tokens,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        optional = {
            "temperature": self.temperature,
            "seed": self.seed,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_format": self.reasoning_format,
            "include_reasoning": self.include_reasoning,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload


# --- Usage and events ----------------------------------------------------------------


class TokenUsage(BaseModel):
    """Provider-reported usage (authoritative token counts, ARCHITECTURE.md section 9.3)."""

    model_config = _STRICT_FROZEN

    prompt_tokens: NonNegInt
    completion_tokens: NonNegInt  # includes reasoning tokens where the provider counts them
    total_tokens: NonNegInt | None = None
    reasoning_tokens: NonNegInt | None = None
    # Groq-specific server-side timings (seconds), kept for stand-in calibration.
    queue_time_s: NonNegFloat | None = None
    prompt_time_s: NonNegFloat | None = None
    completion_time_s: NonNegFloat | None = None
    total_time_s: NonNegFloat | None = None
    # Where the usage was found: OpenAI-style top-level `usage` or Groq's `x_groq.usage`.
    location: Literal["usage", "x_groq"] = "usage"


class _Event(BaseModel):
    model_config = _STRICT_FROZEN

    at_ns: int


class ResponseOpened(_Event):
    kind: Literal["opened"] = "opened"
    sent_ns: int
    meta: ResponseMeta


class ReasoningDelta(_Event):
    kind: Literal["reasoning"] = "reasoning"
    text: str


class ContentDelta(_Event):
    kind: Literal["content"] = "content"
    text: str


class UsageReported(_Event):
    kind: Literal["usage"] = "usage"
    usage: TokenUsage


class StreamCompleted(_Event):
    kind: Literal["completed"] = "completed"
    finish_reason: str | None
    model: str | None
    response_id: str | None


ProviderEvent = ResponseOpened | ReasoningDelta | ContentDelta | UsageReported | StreamCompleted


class CompletionResult(BaseModel):
    """A complete (buffered) provider answer plus its timing and header evidence."""

    model_config = _STRICT_FROZEN

    content: str
    reasoning: str
    finish_reason: str | None
    model: str | None
    response_id: str | None
    usage: TokenUsage | None
    meta: ResponseMeta
    streamed: bool
    content_deltas: NonNegInt
    sent_ns: int
    opened_ns: int
    first_reasoning_ns: int | None
    first_content_ns: int | None
    finished_ns: int

    @property
    def rate_limits(self) -> RateLimitSnapshot:
        return self.meta.rate_limits

    def since_sent_ms(self, at_ns: int | None) -> float | None:
        return None if at_ns is None else ns_to_ms(at_ns - self.sent_ns)

    @property
    def first_content_ms(self) -> float | None:
        """Provider time-to-first-content-token (reasoning excluded). None if not streamed."""
        return self.since_sent_ms(self.first_content_ns)

    @property
    def total_ms(self) -> float:
        return ns_to_ms(self.finished_ns - self.sent_ns)


async def collect_stream(events: AsyncIterator[ProviderEvent]) -> CompletionResult:
    """Buffer a whole stream into a ``CompletionResult`` (verify-before-delivery, section 7)."""
    opened: ResponseOpened | None = None
    content: list[str] = []
    reasoning: list[str] = []
    usage: TokenUsage | None = None
    first_reasoning_ns: int | None = None
    first_content_ns: int | None = None
    async for event in events:
        if isinstance(event, ResponseOpened):
            opened = event
        elif isinstance(event, ReasoningDelta):
            if first_reasoning_ns is None:
                first_reasoning_ns = event.at_ns
            reasoning.append(event.text)
        elif isinstance(event, ContentDelta):
            if first_content_ns is None:
                first_content_ns = event.at_ns
            content.append(event.text)
        elif isinstance(event, UsageReported):
            usage = event.usage
        elif isinstance(event, StreamCompleted):
            if opened is None:
                raise ProviderProtocolError("stream completed without being opened")
            return CompletionResult(
                content="".join(content),
                reasoning="".join(reasoning),
                finish_reason=event.finish_reason,
                model=event.model,
                response_id=event.response_id,
                usage=usage,
                meta=opened.meta,
                streamed=True,
                content_deltas=len(content),
                sent_ns=opened.sent_ns,
                opened_ns=opened.at_ns,
                first_reasoning_ns=first_reasoning_ns,
                first_content_ns=first_content_ns,
                finished_ns=event.at_ns,
            )
    raise ProviderProtocolError("event stream ended without a completion event")


# --- Model discovery -----------------------------------------------------------------


class ModelInfo(BaseModel):
    """One listed model. Only ``id`` is guaranteed; everything else may be absent."""

    model_config = _STRICT_FROZEN

    id: Annotated[str, Field(min_length=1)]
    owned_by: str | None = None
    created: int | None = None
    active: bool | None = None
    context_window: int | None = None
    max_completion_tokens: int | None = None
    metadata: dict[str, Any] = {}  # the full entry exactly as listed


class ModelCatalog(BaseModel):
    model_config = _STRICT_FROZEN

    models: tuple[ModelInfo, ...]
    meta: ResponseMeta
    metadata_issues: tuple[str, ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(model.id for model in self.models)


# --- Protocol ------------------------------------------------------------------------


class ChatProvider(Protocol):
    @property
    def source(self) -> Source: ...

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]: ...

    async def complete_chat(self, request: ChatRequest) -> CompletionResult: ...

    async def list_models(self) -> ModelCatalog: ...

    async def aclose(self) -> None: ...


# --- Real-call cap -------------------------------------------------------------------


class RealCallBudget:
    """Hard cap on outgoing provider HTTP requests. Consumed *before* each request is sent.

    ``on_consume`` lets the caller persist the count (e.g. ``RunHandle.record_real_calls``).
    """

    def __init__(self, max_calls: int, on_consume: Callable[[], None] | None = None) -> None:
        if max_calls < 0:
            raise ValueError("max_calls must be non-negative")
        self._max_calls = max_calls
        self._used = 0
        self._on_consume = on_consume

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return self._max_calls - self._used

    def consume(self) -> None:
        if self._used >= self._max_calls:
            raise RealCallBudgetExceededError(
                f"real-call cap reached ({self._used}/{self._max_calls}); request not sent"
            )
        self._used += 1
        if self._on_consume is not None:
            self._on_consume()


# --- Scripted provider (offline) -----------------------------------------------------


def _empty_meta() -> ResponseMeta:
    return ResponseMeta(status_code=200, headers={}, rate_limits=RateLimitSnapshot())


@dataclass(frozen=True)
class ScriptedReply:
    """One scripted answer. ``error`` is raised after ``fail_after_chunks`` content chunks
    (``0`` = before the response opens)."""

    content_chunks: tuple[str, ...] = ()
    reasoning_chunks: tuple[str, ...] = ()
    usage: TokenUsage | None = None
    finish_reason: str | None = "stop"
    error: ProviderError | None = None
    fail_after_chunks: int = 0
    meta: ResponseMeta = field(default_factory=_empty_meta)


class ScriptedProvider:
    """Deterministic, network-free provider that replays ``ScriptedReply`` objects in order."""

    def __init__(
        self,
        replies: Iterable[ScriptedReply],
        *,
        models: Iterable[str] = (),
        source: Source = "stand-in",
        clock: MonotonicClock = time.perf_counter_ns,
    ) -> None:
        self._replies = list(replies)
        self._models = tuple(models)
        self._source: Source = source
        self._clock = clock
        self.requests: list[ChatRequest] = []

    @property
    def source(self) -> Source:
        return self._source

    def _next_reply(self, request: ChatRequest) -> ScriptedReply:
        self.requests.append(request)
        if not self._replies:
            raise ScriptExhaustedError("ScriptedProvider has no reply left for this call")
        return self._replies.pop(0)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        reply = self._next_reply(request)
        sent_ns = self._clock()
        if reply.error is not None and reply.fail_after_chunks == 0:
            raise reply.error
        yield ResponseOpened(at_ns=self._clock(), sent_ns=sent_ns, meta=reply.meta)
        for text in reply.reasoning_chunks:
            await asyncio.sleep(0)
            yield ReasoningDelta(at_ns=self._clock(), text=text)
        for index, text in enumerate(reply.content_chunks):
            if reply.error is not None and index == reply.fail_after_chunks:
                raise reply.error
            await asyncio.sleep(0)
            yield ContentDelta(at_ns=self._clock(), text=text)
        if reply.error is not None:
            raise reply.error
        if reply.usage is not None:
            yield UsageReported(at_ns=self._clock(), usage=reply.usage)
        yield StreamCompleted(
            at_ns=self._clock(),
            finish_reason=reply.finish_reason,
            model=request.model,
            response_id=None,
        )

    async def complete_chat(self, request: ChatRequest) -> CompletionResult:
        result = await collect_stream(self.stream_chat(request))
        return result.model_copy(update={"streamed": False, "first_content_ns": None})

    async def list_models(self) -> ModelCatalog:
        return ModelCatalog(
            models=tuple(ModelInfo(id=model_id) for model_id in self._models),
            meta=_empty_meta(),
        )

    async def aclose(self) -> None:
        return None


__all__ = [
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "CompletionResult",
    "ContentDelta",
    "ModelCatalog",
    "ModelInfo",
    "ProviderEvent",
    "RealCallBudget",
    "ReasoningDelta",
    "ResponseOpened",
    "ScriptedProvider",
    "ScriptedReply",
    "StreamCompleted",
    "TokenUsage",
    "UsageReported",
    "collect_stream",
]
