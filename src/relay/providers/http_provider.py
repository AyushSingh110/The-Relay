"""``HttpOpenAIProvider``: one OpenAI-compatible HTTP/SSE client for Groq and the Stand-In.

Design rules (ARCHITECTURE.md sections 7.1, 9.1, 9.2):

- Plain ``httpx.AsyncClient``; the default transport is ``AsyncHTTPTransport(retries=0)``
  and there is **no retry logic here**. Exactly one HTTP request is sent per call; any
  retry is ProviderGuard's (P06) visible, counted decision.
- Timeouts: httpx connect/read/write/pool, plus a per-event **stall** timeout (no SSE
  data event for ``stall_s``) and a **total** timeout per call.
- Every response's headers are captured verbatim (sanitised) and the ``x-ratelimit-*``
  / ``retry-after`` headers parsed with "documented, unverified" semantics.
- Failures become typed ``ProviderError`` subclasses (``relay.providers.errors``).
- Groq-specific wire details (``x_groq.usage``, ``reasoning`` deltas, 498 capacity
  errors, rate-limit message wording) are handled only in this module.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from relay.config import Settings
from relay.errors import ConfigurationError
from relay.providers.base import (
    ChatRequest,
    CompletionResult,
    ContentDelta,
    ModelCatalog,
    ModelInfo,
    ProviderEvent,
    RealCallBudget,
    ReasoningDelta,
    ResponseOpened,
    StreamCompleted,
    TokenUsage,
    UsageReported,
)
from relay.providers.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderError,
    ProviderPayloadTooLargeError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderStreamIncompleteError,
    ProviderTimeoutError,
    RateLimitMetric,
)
from relay.providers.headers import (
    RateLimitSnapshot,
    ResponseMeta,
    build_response_meta,
    parse_duration_s,
)
from relay.providers.sse import SSEDecoder, SSEMessage
from relay.redaction import redact_text
from relay.schemas import Source
from relay.telemetry.spans import MonotonicClock

CHAT_PATH: Final = "chat/completions"
MODELS_PATH: Final = "models"
DONE_SENTINEL: Final = "[DONE]"
GROQ_CAPACITY_STATUS: Final = 498  # Groq "flex tier capacity exceeded"
DEFAULT_MAX_ERROR_BODY_CHARS: Final = 2000

PositiveSeconds = Annotated[float, Field(gt=0.0)]


class ProviderTimeouts(BaseModel):
    """[PROPOSED] defaults; tuned after P02-LIVE latency evidence."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    connect_s: PositiveSeconds = 5.0
    read_s: PositiveSeconds = 30.0
    write_s: PositiveSeconds = 10.0
    pool_s: PositiveSeconds = 5.0
    stall_s: PositiveSeconds = 30.0
    total_s: PositiveSeconds = 120.0

    def as_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_s, read=self.read_s, write=self.write_s, pool=self.pool_s
        )


# --- Wire formats (validated, unknown fields ignored) ----------------------------------

_WIRE = ConfigDict(strict=True, extra="ignore", frozen=True)


class _WireUsageDetails(BaseModel):
    model_config = _WIRE
    reasoning_tokens: int | None = None


class _WireUsage(BaseModel):
    model_config = _WIRE
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens_details: _WireUsageDetails | None = None
    queue_time: float | None = None
    prompt_time: float | None = None
    completion_time: float | None = None
    total_time: float | None = None


class _WireXGroq(BaseModel):
    model_config = _WIRE
    usage: _WireUsage | None = None


class _WireDelta(BaseModel):
    model_config = _WIRE
    content: str | None = None
    reasoning: str | None = None  # Groq reasoning models
    reasoning_content: str | None = None  # other OpenAI-compatible servers


class _WireChunkChoice(BaseModel):
    model_config = _WIRE
    index: int = 0
    delta: _WireDelta = _WireDelta()
    finish_reason: str | None = None


class _WireChunk(BaseModel):
    model_config = _WIRE
    id: str | None = None
    model: str | None = None
    choices: list[_WireChunkChoice] = []
    usage: _WireUsage | None = None
    x_groq: _WireXGroq | None = None


class _WireMessage(BaseModel):
    model_config = _WIRE
    content: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None


class _WireChoice(BaseModel):
    model_config = _WIRE
    index: int = 0
    message: _WireMessage
    finish_reason: str | None = None


class _WireCompletion(BaseModel):
    model_config = _WIRE
    id: str | None = None
    model: str | None = None
    choices: Annotated[list[_WireChoice], Field(min_length=1)]
    usage: _WireUsage | None = None
    x_groq: _WireXGroq | None = None


def _float_or_none(value: float | None) -> float | None:
    return None if value is None else float(value)


def _to_usage(wire: _WireUsage, location: Literal["usage", "x_groq"]) -> TokenUsage:
    details = wire.completion_tokens_details
    return TokenUsage(
        prompt_tokens=wire.prompt_tokens,
        completion_tokens=wire.completion_tokens,
        total_tokens=wire.total_tokens,
        reasoning_tokens=None if details is None else details.reasoning_tokens,
        queue_time_s=_float_or_none(wire.queue_time),
        prompt_time_s=_float_or_none(wire.prompt_time),
        completion_time_s=_float_or_none(wire.completion_time),
        total_time_s=_float_or_none(wire.total_time),
        location=location,
    )


def _pick_usage(usage: _WireUsage | None, x_groq: _WireXGroq | None) -> TokenUsage | None:
    """Prefer OpenAI-style top-level ``usage``; fall back to Groq's ``x_groq.usage``."""
    if usage is not None:
        return _to_usage(usage, "usage")
    if x_groq is not None and x_groq.usage is not None:
        return _to_usage(x_groq.usage, "x_groq")
    return None


# --- Error mapping ------------------------------------------------------------------

_METRIC = re.compile(r"\((RPM|TPM|RPD|TPD)\)")
_DAILY_WORDING = re.compile(r"per day", re.IGNORECASE)
_MINUTE_WORDING = re.compile(r"per minute", re.IGNORECASE)
_BODY_NUMBER = re.compile(r"\b(Limit|Used|Requested)\s*:?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_TRY_AGAIN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+)")


@dataclass(frozen=True)
class _ErrorBody:
    type: str | None
    code: str | None
    message: str


def _parse_error_body(body: bytes) -> _ErrorBody:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _ErrorBody(None, None, "")
    error = data.get("error", data) if isinstance(data, dict) else None
    if isinstance(error, str):
        return _ErrorBody(None, None, error)
    if not isinstance(error, dict):
        return _ErrorBody(None, None, "")

    def text(key: str) -> str | None:
        value = error.get(key)
        return value if isinstance(value, str) else None

    return _ErrorBody(text("type"), text("code"), text("message") or "")


def _classify_rate_limit(
    message: str, snapshot: RateLimitSnapshot
) -> tuple[Literal["minute", "daily", "unknown"], str, RateLimitMetric | None]:
    """Return (scope, basis, metric). Body wording wins; headers are a documented fallback."""
    match = _METRIC.search(message)
    metric: RateLimitMetric | None = match.group(1) if match else None  # type: ignore[assignment]
    if metric in ("RPD", "TPD") or _DAILY_WORDING.search(message):
        return "daily", "body", metric
    if metric in ("RPM", "TPM") or _MINUTE_WORDING.search(message):
        return "minute", "body", metric
    if snapshot.remaining_requests == 0:  # documented as RPD; unverified
        return "daily", "headers_documented_unverified", None
    if snapshot.remaining_tokens == 0:  # documented as TPM; unverified
        return "minute", "headers_documented_unverified", None
    return "unknown", "none", None


def _rate_limit_error(
    message: str, meta: ResponseMeta, common: dict[str, Any], what: str
) -> ProviderRateLimitError:
    scope, basis, metric = _classify_rate_limit(message, meta.rate_limits)
    retry_after = meta.rate_limits.retry_after_s
    try_again = _TRY_AGAIN.search(message)
    if retry_after is None and try_again is not None:
        retry_after = parse_duration_s(try_again.group(1))
    detail = {name.lower(): float(value) for name, value in _BODY_NUMBER.findall(message)}
    return ProviderRateLimitError(
        f"{what}: rate limited (scope={scope}, metric={metric})",
        scope=scope,
        basis=basis,  # type: ignore[arg-type]
        metric=metric,
        retry_after_s=retry_after,
        detail=detail,
        **common,
    )


def map_error_response(
    meta: ResponseMeta, body: bytes, max_body_chars: int = DEFAULT_MAX_ERROR_BODY_CHARS
) -> ProviderError:
    """Translate a non-2xx response into a typed error (never raises itself)."""
    parsed = _parse_error_body(body)
    excerpt = redact_text(body.decode("utf-8", errors="replace"))[:max_body_chars] or None
    common: dict[str, Any] = {
        "meta": meta,
        "body_excerpt": excerpt,
        "provider_error_type": parsed.type,
        "provider_error_code": parsed.code,
    }
    status = meta.status_code
    what = f"provider HTTP {status}"
    if parsed.type or parsed.code:
        what = f"{what} ({parsed.code or parsed.type})"
    if status == 429:
        return _rate_limit_error(parsed.message, meta, common, what)
    if status in (401, 403):
        return ProviderAuthError(f"{what}: credentials rejected", **common)
    if status == 413:
        return ProviderPayloadTooLargeError(f"{what}: request too large", **common)
    if status == GROQ_CAPACITY_STATUS or status >= 500:
        return ProviderServerError(f"{what}: server-side failure", **common)
    if 400 <= status < 500:
        return ProviderBadRequestError(f"{what}: request rejected", **common)
    return ProviderProtocolError(f"{what}: unexpected status", **common)


def _map_stream_error(payload: Any, meta: ResponseMeta) -> ProviderError:
    body = json.dumps({"error": payload}).encode("utf-8")
    parsed = _parse_error_body(body)
    common: dict[str, Any] = {
        "meta": meta,
        "body_excerpt": redact_text(body.decode("utf-8"))[:DEFAULT_MAX_ERROR_BODY_CHARS],
        "provider_error_type": parsed.type,
        "provider_error_code": parsed.code,
    }
    labels = " ".join(filter(None, (parsed.type, parsed.code))).lower()
    if "rate_limit" in labels or _METRIC.search(parsed.message):
        return _rate_limit_error(parsed.message, meta, common, "in-stream error")
    return ProviderServerError("in-stream error event from provider", **common)


def _map_transport_error(exc: httpx.HTTPError, meta: ResponseMeta | None = None) -> ProviderError:
    name = type(exc).__name__
    if isinstance(exc, httpx.ReadTimeout):
        return ProviderTimeoutError("provider read timed out", phase="read", meta=meta)
    if isinstance(exc, httpx.DecodingError):
        return ProviderProtocolError("response body could not be decoded", meta=meta)
    return ProviderConnectionError(f"provider transport failure: {name}", reason=name, meta=meta)


# --- Model catalogue ----------------------------------------------------------------

_MODEL_FIELDS: Final[tuple[tuple[str, type], ...]] = (
    ("owned_by", str),
    ("created", int),
    ("active", bool),
    ("context_window", int),
    ("max_completion_tokens", int),
)


def _field_ok(value: Any, kind: type) -> bool:
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


def parse_model_catalog(body: bytes, meta: ResponseMeta) -> ModelCatalog:
    """Validate ``GET /models``. Per-model metadata is optional and type-checked per field."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderProtocolError("model list is not valid JSON", meta=meta) from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ProviderProtocolError("model list must be an object with a 'data' array", meta=meta)
    models: dict[str, ModelInfo] = {}
    issues: list[str] = []
    for position, entry in enumerate(data["data"]):
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(model_id, str) or not model_id:
            issues.append(f"entry {position}: no string 'id'; skipped")
            continue
        if model_id in models:
            issues.append(f"{model_id}: listed more than once; first entry kept")
            continue
        fields: dict[str, Any] = {}
        for name, kind in _MODEL_FIELDS:
            value = entry.get(name)
            if value is None:
                continue
            if _field_ok(value, kind):
                fields[name] = value
            else:
                issues.append(f"{model_id}: '{name}' has unexpected type {type(value).__name__}")
        models[model_id] = ModelInfo(id=model_id, metadata=entry, **fields)
    return ModelCatalog(
        models=tuple(models[key] for key in sorted(models)),
        meta=meta,
        metadata_issues=tuple(issues),
    )


# --- Provider -----------------------------------------------------------------------


@dataclass
class _StreamState:
    model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    usage_seen: bool = False
    done: bool = False


class HttpOpenAIProvider:
    """Async OpenAI-compatible chat client. One instance owns one ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        base_url: str,
        source: Source,
        api_key: SecretStr | None = None,
        timeouts: ProviderTimeouts | None = None,
        call_budget: RealCallBudget | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: MonotonicClock = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_error_body_chars: int = DEFAULT_MAX_ERROR_BODY_CHARS,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host:
            raise ConfigurationError("provider base_url must be an absolute http(s) URL")
        if source == "real" and call_budget is None:
            raise ConfigurationError("a real provider requires a RealCallBudget (hard call cap)")
        self._source: Source = source
        self._timeouts = timeouts or ProviderTimeouts()
        self._budget = call_budget
        self._clock = clock
        self._wall_clock = wall_clock
        self._max_error_body_chars = max_error_body_chars
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0)
        headers = {"accept": "application/json", "user-agent": "the-relay/0.1"}
        if api_key is not None:
            headers["authorization"] = f"Bearer {api_key.get_secret_value()}"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=self._transport,
            timeout=self._timeouts.as_httpx(),
            headers=headers,
            follow_redirects=False,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        target: Literal["groq", "standin"],
        call_budget: RealCallBudget | None = None,
        timeouts: ProviderTimeouts | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> HttpOpenAIProvider:
        if target == "groq":
            if settings.groq_api_key is None:
                raise ConfigurationError("GROQ_API_KEY is required for the groq target")
            return cls(
                base_url=settings.groq_base_url,
                source="real",
                api_key=settings.groq_api_key,
                timeouts=timeouts,
                call_budget=call_budget,
                transport=transport,
            )
        return cls(
            base_url=settings.standin_url,
            source="stand-in",
            timeouts=timeouts,
            call_budget=call_budget,
            transport=transport,
        )

    @property
    def source(self) -> Source:
        return self._source

    @property
    def timeouts(self) -> ProviderTimeouts:
        return self._timeouts

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpOpenAIProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # -- public calls --

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeouts.total_s
        http_request = self._client.build_request(
            "POST",
            CHAT_PATH,
            json=request.to_payload(stream=True),
            headers={"accept": "text/event-stream"},
        )
        response, sent_ns = await self._open(http_request, deadline)
        try:
            meta = self._meta(response)
            if not response.is_success:
                raise map_error_response(
                    meta, await self._read_body(response, deadline, meta),
                    self._max_error_body_chars,
                )
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type.lower():
                shown = content_type.split(";")[0].strip() or "none"
                raise ProviderProtocolError(
                    f"expected text/event-stream, got content-type {shown}", meta=meta
                )
            yield ResponseOpened(at_ns=self._clock(), sent_ns=sent_ns, meta=meta)
            state = _StreamState()
            decoder = SSEDecoder()
            stall_deadline = loop.time() + self._timeouts.stall_s
            async with aclosing(response.aiter_lines()) as lines:
                while not state.done:
                    line = await self._next_line(lines, deadline, stall_deadline, meta)
                    message = decoder.flush() if line is None else decoder.feed_line(line)
                    if message is not None:
                        stall_deadline = loop.time() + self._timeouts.stall_s
                        for event in self._events_from_message(message, meta, state):
                            yield event
                    if line is None:
                        break
            if not state.done:
                raise ProviderStreamIncompleteError(
                    "stream ended before the [DONE] sentinel", meta=meta
                )
            yield StreamCompleted(
                at_ns=self._clock(),
                finish_reason=state.finish_reason,
                model=state.model,
                response_id=state.response_id,
            )
        finally:
            await response.aclose()

    async def complete_chat(self, request: ChatRequest) -> CompletionResult:
        """Non-streaming call (``stream=false``). No time-to-first-token is available."""
        deadline = asyncio.get_running_loop().time() + self._timeouts.total_s
        http_request = self._client.build_request(
            "POST", CHAT_PATH, json=request.to_payload(stream=False)
        )
        response, sent_ns = await self._open(http_request, deadline)
        try:
            meta = self._meta(response)
            opened_ns = self._clock()
            body = await self._read_body(response, deadline, meta)
            if not response.is_success:
                raise map_error_response(meta, body, self._max_error_body_chars)
            try:
                wire = _WireCompletion.model_validate_json(body)
            except ValidationError:
                raise ProviderProtocolError(
                    "chat completion body does not match the expected schema", meta=meta
                ) from None
            choice = wire.choices[0]
            if choice.index != 0 or len(wire.choices) != 1:
                raise ProviderProtocolError("expected exactly one choice (index 0)", meta=meta)
            message = choice.message
            return CompletionResult(
                content=message.content or "",
                reasoning=message.reasoning or message.reasoning_content or "",
                finish_reason=choice.finish_reason,
                model=wire.model,
                response_id=wire.id,
                usage=_pick_usage(wire.usage, wire.x_groq),
                meta=meta,
                streamed=False,
                content_deltas=0,
                sent_ns=sent_ns,
                opened_ns=opened_ns,
                first_reasoning_ns=None,
                first_content_ns=None,
                finished_ns=self._clock(),
            )
        finally:
            await response.aclose()

    async def list_models(self) -> ModelCatalog:
        deadline = asyncio.get_running_loop().time() + self._timeouts.total_s
        response, _ = await self._open(self._client.build_request("GET", MODELS_PATH), deadline)
        try:
            meta = self._meta(response)
            body = await self._read_body(response, deadline, meta)
            if not response.is_success:
                raise map_error_response(meta, body, self._max_error_body_chars)
            return parse_model_catalog(body, meta)
        finally:
            await response.aclose()

    # -- internals --

    def _meta(self, response: httpx.Response) -> ResponseMeta:
        return build_response_meta(
            response.status_code, response.headers.multi_items(), self._wall_clock()
        )

    async def _open(
        self, http_request: httpx.Request, deadline: float
    ) -> tuple[httpx.Response, int]:
        """Send exactly one request (budget consumed first) and wait for response headers."""
        if self._budget is not None:
            self._budget.consume()
        sent_ns = self._clock()
        try:
            async with asyncio.timeout_at(deadline):
                response = await self._client.send(http_request, stream=True)
        except TimeoutError:
            raise ProviderTimeoutError(
                "no response headers before the total timeout", phase="total"
            ) from None
        except httpx.HTTPError as exc:
            raise _map_transport_error(exc) from exc
        return response, sent_ns

    async def _read_body(
        self, response: httpx.Response, deadline: float, meta: ResponseMeta
    ) -> bytes:
        try:
            async with asyncio.timeout_at(deadline):
                return await response.aread()
        except TimeoutError:
            raise ProviderTimeoutError(
                "response body not received before the total timeout", phase="total", meta=meta
            ) from None
        except httpx.HTTPError as exc:
            raise _map_transport_error(exc, meta) from exc

    async def _next_line(
        self,
        lines: AsyncIterator[str],
        deadline: float,
        stall_deadline: float,
        meta: ResponseMeta,
    ) -> str | None:
        """Next line of the body, or ``None`` at end of stream."""
        phase: Literal["stall", "total"] = "stall" if stall_deadline < deadline else "total"
        try:
            async with asyncio.timeout_at(min(deadline, stall_deadline)):
                return await anext(lines)
        except StopAsyncIteration:
            return None
        except TimeoutError:
            raise ProviderTimeoutError(
                f"stream {phase} timeout: no SSE data event in time", phase=phase, meta=meta
            ) from None
        except httpx.HTTPError as exc:
            raise _map_transport_error(exc, meta) from exc

    def _events_from_message(
        self, message: SSEMessage, meta: ResponseMeta, state: _StreamState
    ) -> list[ProviderEvent]:
        if message.data.strip() == DONE_SENTINEL:
            state.done = True
            return []
        at_ns = self._clock()
        try:
            data = json.loads(message.data)
        except json.JSONDecodeError:
            raise ProviderProtocolError("stream chunk is not valid JSON", meta=meta) from None
        if not isinstance(data, dict):
            raise ProviderProtocolError("stream chunk is not a JSON object", meta=meta)
        if message.event == "error" or "error" in data:
            raise _map_stream_error(data.get("error", data), meta)
        try:
            chunk = _WireChunk.model_validate(data)
        except ValidationError:
            raise ProviderProtocolError(
                "stream chunk does not match the chat.completion.chunk schema", meta=meta
            ) from None
        state.model = chunk.model or state.model
        state.response_id = chunk.id or state.response_id
        events: list[ProviderEvent] = []
        for choice in chunk.choices:
            if choice.index != 0:
                raise ProviderProtocolError("unexpected choice index in stream", meta=meta)
            reasoning = choice.delta.reasoning or choice.delta.reasoning_content
            if reasoning:
                events.append(ReasoningDelta(at_ns=at_ns, text=reasoning))
            if choice.delta.content:
                events.append(ContentDelta(at_ns=at_ns, text=choice.delta.content))
            if choice.finish_reason is not None:
                state.finish_reason = choice.finish_reason
        usage = _pick_usage(chunk.usage, chunk.x_groq)
        if usage is not None and not state.usage_seen:
            state.usage_seen = True  # first report wins (Groq may repeat it in x_groq)
            events.append(UsageReported(at_ns=at_ns, usage=usage))
        return events


__all__ = [
    "HttpOpenAIProvider",
    "ProviderTimeouts",
    "map_error_response",
    "parse_model_catalog",
]
