"""Offline tests for the provider layer: SSE, usage, headers, errors, timeouts, no retries.

Every HTTP exchange goes through ``httpx.MockTransport``. Header values below are
synthetic fixtures for parser tests, not Groq measurements.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from relay.errors import ConfigurationError, sanitize_error
from relay.providers.base import (
    ChatMessage,
    ChatRequest,
    RealCallBudget,
    ScriptedProvider,
    ScriptedReply,
    TokenUsage,
    collect_stream,
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
    RealCallBudgetExceededError,
    ScriptExhaustedError,
)
from relay.providers.headers import (
    capture_headers,
    parse_duration_s,
    parse_rate_limits,
    parse_retry_after_s,
)
from relay.providers.http_provider import HttpOpenAIProvider, ProviderTimeouts
from relay.providers.sse import SSEDecoder, SSEMessage
from relay.redaction import REDACTED, REDACTED_ORG
from tests.factories import FAKE_KEY

BASE_URL = "https://provider.test/openai/v1"
# Synthetic header fixture (shape follows Groq docs; values are invented for tests).
RATE_HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-remaining-requests": "999",
    "x-ratelimit-reset-requests": "1m26.4s",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "7800",
    "x-ratelimit-reset-tokens": "1.5s",
    "x-request-id": "req_test_1",
}
SSE_HEADERS = {"content-type": "text/event-stream", **RATE_HEADERS}

Handler = Callable[[httpx.Request], Any]


class Recorder:
    """MockTransport handler wrapper that records every request it receives."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self._handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    @property
    def count(self) -> int:
        return len(self.requests)


def make_provider(
    handler: Handler,
    *,
    timeouts: ProviderTimeouts | None = None,
    budget: RealCallBudget | None = None,
    source: str = "stand-in",
) -> tuple[HttpOpenAIProvider, Recorder]:
    recorder = Recorder(handler)
    provider = HttpOpenAIProvider(
        base_url=BASE_URL,
        source=source,  # type: ignore[arg-type]
        api_key=SecretStr(FAKE_KEY),
        timeouts=timeouts,
        call_budget=budget,
        transport=httpx.MockTransport(recorder),
    )
    return provider, recorder


def chat_request(**overrides: Any) -> ChatRequest:
    values: dict[str, Any] = {
        "model": "test-model",
        "messages": (ChatMessage(role="user", content="What is d_model?"),),
        "max_completion_tokens": 64,
    }
    values.update(overrides)
    return ChatRequest(**values)


def sse_body(*chunks: dict[str, Any] | str, done: bool = True, newline: str = "\n") -> bytes:
    parts = [
        f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}{newline}{newline}"
        for chunk in chunks
    ]
    if done:
        parts.append(f"data: [DONE]{newline}{newline}")
    return "".join(parts).encode("utf-8")


def delta(content: str | None = None, *, reasoning: str | None = None,
          finish: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if content is not None:
        body["content"] = content
    if reasoning is not None:
        body["reasoning"] = reasoning
    return {
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "delta": body, "finish_reason": finish}],
    }


USAGE = {
    "prompt_tokens": 12,
    "completion_tokens": 7,
    "total_tokens": 19,
    "completion_tokens_details": {"reasoning_tokens": 3},
    "queue_time": 0.012,
    "prompt_time": 0.003,
    "completion_time": 0.05,
    "total_time": 0.065,
}


def standard_stream() -> bytes:
    return sse_body(
        {"id": "chatcmpl-1", "model": "test-model",
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        delta(reasoning="thinking"),
        delta("Hel"),
        delta("lo"),
        delta(finish="stop"),
        {"id": "chatcmpl-1", "model": "test-model", "choices": [], "usage": USAGE},
    )


def sse_response(body: bytes | AsyncIterator[bytes], headers: dict[str, str] | None = None,
                 status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers=headers or SSE_HEADERS, content=body)


async def drain(provider: HttpOpenAIProvider, request: ChatRequest | None = None) -> list[Any]:
    return [event async for event in provider.stream_chat(request or chat_request())]


# --- Streaming happy path -----------------------------------------------------------


async def test_stream_parses_reasoning_content_usage_and_headers() -> None:
    provider, recorder = make_provider(lambda request: sse_response(standard_stream()))
    async with provider:
        result = await collect_stream(provider.stream_chat(chat_request()))

    assert result.content == "Hello"
    assert result.reasoning == "thinking"
    assert result.finish_reason == "stop"
    assert (result.model, result.response_id) == ("test-model", "chatcmpl-1")
    assert result.content_deltas == 2
    assert result.usage == TokenUsage(
        prompt_tokens=12, completion_tokens=7, total_tokens=19, reasoning_tokens=3,
        queue_time_s=0.012, prompt_time_s=0.003, completion_time_s=0.05, total_time_s=0.065,
        location="usage",
    )
    assert result.first_reasoning_ns is not None and result.first_content_ns is not None
    assert result.first_reasoning_ns <= result.first_content_ns <= result.finished_ns
    assert result.first_content_ms is not None and result.first_content_ms <= result.total_ms

    limits = result.rate_limits
    assert limits.semantics == "documented_unverified"
    assert (limits.limit_requests, limits.remaining_requests) == (1000, 999)
    assert (limits.limit_tokens, limits.remaining_tokens) == (8000, 7800)
    assert (limits.reset_requests_s, limits.reset_tokens_s) == (86.4, 1.5)
    assert limits.raw == {k: v for k, v in RATE_HEADERS.items() if k.startswith("x-ratelimit")}
    assert result.meta.headers["x-request-id"] == "req_test_1"
    assert result.meta.provider_request_id == "req_test_1"

    sent = recorder.requests[0]
    assert recorder.count == 1
    assert sent.method == "POST"
    assert sent.url.path == "/openai/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert sent.headers["accept"] == "text/event-stream"
    payload = json.loads(sent.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["max_completion_tokens"] == 64
    assert "temperature" not in payload  # unset optionals are omitted


async def test_event_order_is_opened_deltas_usage_completed() -> None:
    provider, _ = make_provider(lambda request: sse_response(standard_stream()))
    async with provider:
        kinds = [event.kind for event in await drain(provider)]
    assert kinds == ["opened", "reasoning", "content", "content", "usage", "completed"]


async def test_usage_falls_back_to_x_groq_and_first_report_wins() -> None:
    x_groq_usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    body = sse_body(
        delta("A", finish="stop"),
        {"id": "c", "choices": [], "x_groq": {"usage": x_groq_usage}},
        {"id": "c", "choices": [], "x_groq": {"usage": {**x_groq_usage, "prompt_tokens": 99}}},
    )
    provider, _ = make_provider(lambda request: sse_response(body))
    async with provider:
        result = await collect_stream(provider.stream_chat(chat_request()))
    assert result.usage is not None
    assert (result.usage.prompt_tokens, result.usage.location) == (5, "x_groq")


async def test_stream_without_usage_reports_none() -> None:
    provider, _ = make_provider(lambda request: sse_response(sse_body(delta("A", finish="stop"))))
    async with provider:
        result = await collect_stream(provider.stream_chat(chat_request()))
    assert result.usage is None
    assert result.content == "A"


async def test_sse_split_across_network_reads_and_crlf() -> None:
    raw = sse_body(delta("Hel"), delta("lo", finish="stop"), newline="\r\n")

    async def pieces() -> AsyncIterator[bytes]:
        for start in range(0, len(raw), 7):  # cut lines and JSON mid-way
            yield raw[start:start + 7]

    provider, _ = make_provider(lambda request: sse_response(pieces()))
    async with provider:
        result = await collect_stream(provider.stream_chat(chat_request()))
    assert result.content == "Hello"


async def test_done_without_trailing_blank_line_is_accepted() -> None:
    raw = sse_body(delta("ok", finish="stop"), done=False) + b"data: [DONE]"
    provider, _ = make_provider(lambda request: sse_response(raw))
    async with provider:
        result = await collect_stream(provider.stream_chat(chat_request()))
    assert result.content == "ok"


async def test_comments_and_keepalives_are_ignored() -> None:
    raw = b": keep-alive\n\n" + sse_body(delta("x", finish="stop"))
    provider, _ = make_provider(lambda request: sse_response(raw))
    async with provider:
        assert (await collect_stream(provider.stream_chat(chat_request()))).content == "x"


# --- SSE decoder unit tests ---------------------------------------------------------


def test_sse_decoder_joins_multiline_data_and_reads_event_names() -> None:
    decoder = SSEDecoder()
    assert decoder.feed_line("event: message") is None
    assert decoder.feed_line("data: first") is None
    assert decoder.feed_line("data:second") is None  # no space after the colon
    assert decoder.feed_line(": a comment") is None
    assert decoder.feed_line("id: 7") is None
    assert decoder.feed_line("") == SSEMessage(event="message", data="first\nsecond")
    assert decoder.feed_line("") is None  # blank line with nothing pending
    assert decoder.feed_line("data:  two spaces") is None
    assert decoder.flush() == SSEMessage(event=None, data=" two spaces")
    assert decoder.flush() is None


# --- Malformed responses ------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"data: {not json\n\n",
        b"data: [1, 2]\n\n",
        b'data: {"choices": "nope"}\n\n',
        b'data: {"choices": [{"index": 1, "delta": {"content": "x"}}]}\n\n',
        b'data: {"choices": [{"index": 0, "delta": {"content": 5}}]}\n\n',
    ],
    ids=["invalid-json", "not-object", "choices-not-list", "second-choice", "content-not-str"],
)
async def test_malformed_chunks_raise_protocol_error(body: bytes) -> None:
    provider, recorder = make_provider(lambda request: sse_response(body))
    async with provider:
        with pytest.raises(ProviderProtocolError) as info:
            await drain(provider)
    assert not info.value.retryable
    assert recorder.count == 1


async def test_stream_ending_before_done_is_incomplete_and_retryable_hint() -> None:
    body = sse_body(delta("partial"), done=False)
    provider, _ = make_provider(lambda request: sse_response(body))
    seen: list[Any] = []
    async with provider:
        with pytest.raises(ProviderStreamIncompleteError) as info:
            async for event in provider.stream_chat(chat_request()):
                seen.append(event)
    assert [event.kind for event in seen] == ["opened", "content"]
    assert info.value.retryable


async def test_wrong_content_type_is_protocol_error() -> None:
    response = httpx.Response(200, headers={"content-type": "application/json"}, json={"a": 1})
    provider, _ = make_provider(lambda request: response)
    async with provider:
        with pytest.raises(ProviderProtocolError, match="text/event-stream"):
            await drain(provider)


async def test_in_stream_error_events_are_typed() -> None:
    server = sse_body(delta("a"), {"error": {"message": "boom", "type": "server_error"}})
    limited = sse_body({"error": {"message": "Rate limit reached ... on tokens per minute (TPM)",
                                  "type": "tokens", "code": "rate_limit_exceeded"}})
    for body, expected in ((server, ProviderServerError), (limited, ProviderRateLimitError)):
        provider, _ = make_provider(lambda request, body=body: sse_response(body))
        async with provider:
            with pytest.raises(expected):
                await drain(provider)


# --- HTTP error mapping and no hidden retries ---------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "error_code", "retryable"),
    [
        (400, ProviderBadRequestError, "internal_error", False),
        (401, ProviderAuthError, "provider_auth_error", False),
        (403, ProviderAuthError, "provider_auth_error", False),
        (404, ProviderBadRequestError, "internal_error", False),
        (413, ProviderPayloadTooLargeError, "payload_too_large", False),
        (498, ProviderServerError, "upstream_error", True),
        (500, ProviderServerError, "upstream_error", True),
        (503, ProviderServerError, "upstream_error", True),
    ],
)
async def test_status_codes_map_to_typed_errors_with_exactly_one_request(
    status: int, error_type: type[ProviderError], error_code: str, retryable: bool
) -> None:
    body = {"error": {"message": "nope", "type": "invalid_request_error", "code": "x"}}
    provider, recorder = make_provider(
        lambda request: httpx.Response(status, json=body, headers=RATE_HEADERS)
    )
    async with provider:
        with pytest.raises(error_type) as info:
            await drain(provider)
    error = info.value
    assert type(error) is error_type
    assert (error.status_code, error.error_code, error.retryable) == (status, error_code, retryable)
    assert error.meta is not None and error.meta.rate_limits.limit_requests == 1000
    assert sanitize_error(error) == f"{error_type.__name__}(status_code={status})"
    assert recorder.count == 1  # the client never retries


async def test_minute_rate_limit_from_body_and_retry_after_header() -> None:
    message = (
        "Rate limit reached for model `m` in organization `org_01abcdefgh` service tier "
        "`on_demand` on tokens per minute (TPM): Limit 6000, Used 5900, Requested 300. "
        "Please try again in 2.5s."
    )
    response = httpx.Response(
        429,
        json={"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}},
        headers={**RATE_HEADERS, "retry-after": "3"},
    )
    provider, recorder = make_provider(lambda request: response)
    async with provider:
        with pytest.raises(ProviderRateLimitError) as info:
            await drain(provider)
    error = info.value
    assert (error.scope, error.basis, error.metric) == ("minute", "body", "TPM")
    assert error.error_code == "upstream_rate_limited" and error.retryable
    assert error.retry_after_s == 3.0  # header wins over the body hint
    assert error.detail == {"limit": 6000.0, "used": 5900.0, "requested": 300.0}
    assert error.body_excerpt is not None
    assert "org_01abcdefgh" not in error.body_excerpt and REDACTED_ORG in error.body_excerpt
    assert "org_" not in str(error) and "Rate limit reached" not in str(error)
    assert recorder.count == 1


async def test_daily_rate_limit_is_not_retryable() -> None:
    message = "Rate limit reached on requests per day (RPD): Limit 1000, Used 1000. " \
              "Please try again in 1m2.5s."
    response = httpx.Response(429, json={"error": {"message": message}})
    provider, _ = make_provider(lambda request: response)
    async with provider:
        with pytest.raises(ProviderRateLimitError) as info:
            await drain(provider)
    error = info.value
    assert (error.scope, error.metric) == ("daily", "RPD")
    assert error.error_code == "daily_budget_exhausted"
    assert not error.retryable
    assert error.retry_after_s == 62.5  # from the body when no retry-after header


@pytest.mark.parametrize(
    ("headers", "scope", "basis"),
    [
        ({"x-ratelimit-remaining-requests": "0"}, "daily", "headers_documented_unverified"),
        ({"x-ratelimit-remaining-tokens": "0"}, "minute", "headers_documented_unverified"),
        ({}, "unknown", "none"),
    ],
)
async def test_rate_limit_scope_falls_back_to_headers(
    headers: dict[str, str], scope: str, basis: str
) -> None:
    response = httpx.Response(429, text="slow down", headers=headers)
    provider, _ = make_provider(lambda request: response)
    async with provider:
        with pytest.raises(ProviderRateLimitError) as info:
            await drain(provider)
    assert (info.value.scope, info.value.basis) == (scope, basis)


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (httpx.ReadTimeout("t"), ProviderTimeoutError),
        (httpx.ConnectTimeout("t"), ProviderConnectionError),
        (httpx.ConnectError("refused"), ProviderConnectionError),
        (httpx.RemoteProtocolError("bad"), ProviderConnectionError),
    ],
)
async def test_transport_failures_are_typed_and_not_retried(
    exc: Exception, error_type: type[ProviderError]
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise exc

    provider, recorder = make_provider(fail)
    async with provider:
        with pytest.raises(error_type) as info:
            await drain(provider)
    assert recorder.count == 1
    if isinstance(info.value, ProviderTimeoutError):
        assert (info.value.phase, info.value.error_code) == ("read", "provider_stalled")
    if isinstance(info.value, ProviderConnectionError):
        assert info.value.reason == type(exc).__name__
        assert info.value.error_code == "upstream_error"


async def test_connection_dropped_mid_stream_after_partial_content() -> None:
    async def pieces() -> AsyncIterator[bytes]:
        yield sse_body(delta("Hel"), done=False)
        raise httpx.ReadError("connection reset")

    provider, recorder = make_provider(lambda request: sse_response(pieces()))
    seen: list[Any] = []
    async with provider:
        with pytest.raises(ProviderConnectionError):
            async for event in provider.stream_chat(chat_request()):
                seen.append(event)
    assert [event.kind for event in seen] == ["opened", "content"]
    assert recorder.count == 1


def test_default_transport_has_zero_retries() -> None:
    provider = HttpOpenAIProvider(base_url=BASE_URL, source="stand-in")
    transport = provider._transport  # introspection: the real transport, not a mock
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert transport._pool._retries == 0
    asyncio.run(provider.aclose())


async def test_real_transport_cannot_reach_external_hosts_in_tests() -> None:
    provider = HttpOpenAIProvider(base_url="https://api.groq.com/openai/v1", source="stand-in")
    async with provider:
        with pytest.raises(RuntimeError, match="network access is blocked"):
            await drain(provider)


# --- Stall and total timeouts -------------------------------------------------------


async def test_stall_timeout_after_partial_stream() -> None:
    async def pieces() -> AsyncIterator[bytes]:
        yield sse_body(delta("Hel"), done=False)
        await asyncio.sleep(5)
        yield b""

    timeouts = ProviderTimeouts(stall_s=0.05, total_s=5.0)
    provider, recorder = make_provider(lambda request: sse_response(pieces()), timeouts=timeouts)
    seen: list[Any] = []
    async with provider:
        with pytest.raises(ProviderTimeoutError) as info:
            async for event in provider.stream_chat(chat_request()):
                seen.append(event)
    assert info.value.phase == "stall"
    assert info.value.error_code == "provider_stalled"
    assert [event.kind for event in seen] == ["opened", "content"]
    assert recorder.count == 1


async def test_keepalive_comments_do_not_reset_the_stall_timer() -> None:
    async def keepalives() -> AsyncIterator[bytes]:
        for _ in range(200):
            yield b": keep-alive\n\n"
            await asyncio.sleep(0.01)

    timeouts = ProviderTimeouts(stall_s=0.1, total_s=5.0)
    provider, _ = make_provider(lambda request: sse_response(keepalives()), timeouts=timeouts)
    async with provider:
        with pytest.raises(ProviderTimeoutError) as info:
            await drain(provider)
    assert info.value.phase == "stall"


async def test_total_timeout_caps_a_slow_but_steady_stream() -> None:
    async def trickle() -> AsyncIterator[bytes]:
        for _ in range(200):
            yield sse_body(delta("x"), done=False)
            await asyncio.sleep(0.01)

    timeouts = ProviderTimeouts(stall_s=1.0, total_s=0.1)
    provider, _ = make_provider(lambda request: sse_response(trickle()), timeouts=timeouts)
    async with provider:
        with pytest.raises(ProviderTimeoutError) as info:
            await drain(provider)
    assert info.value.phase == "total"


async def test_total_timeout_while_waiting_for_headers() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return sse_response(standard_stream())

    provider, _ = make_provider(slow, timeouts=ProviderTimeouts(total_s=0.05))
    async with provider:
        with pytest.raises(ProviderTimeoutError) as info:
            await drain(provider)
    assert info.value.phase == "total"


async def test_consumer_stopping_early_closes_the_response() -> None:
    class TrackedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse_body(delta("a"), done=False)
            await asyncio.sleep(5)
            yield b""

        async def aclose(self) -> None:
            TrackedStream.closed = True

    provider, _ = make_provider(
        lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=TrackedStream())
    )
    async with provider:
        stream = provider.stream_chat(chat_request())
        async for event in stream:
            if event.kind == "content":
                break
        await stream.aclose()  # type: ignore[attr-defined]
    assert TrackedStream.closed


# --- Non-streaming --------------------------------------------------------------------


async def test_complete_chat_parses_message_and_usage() -> None:
    body = {
        "id": "chatcmpl-9",
        "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "Answer [P1]",
                                 "reasoning": "because"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }
    provider, recorder = make_provider(
        lambda request: httpx.Response(200, json=body, headers=RATE_HEADERS)
    )
    async with provider:
        result = await provider.complete_chat(chat_request(temperature=0.0))
    assert (result.content, result.reasoning, result.finish_reason) == ("Answer [P1]", "because",
                                                                        "stop")
    assert result.usage is not None and result.usage.total_tokens == 14
    assert not result.streamed and result.first_content_ms is None
    assert result.rate_limits.remaining_tokens == 7800
    payload = json.loads(recorder.requests[0].content)
    assert payload["stream"] is False and "stream_options" not in payload
    assert payload["temperature"] == 0.0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"index": 0}]}),
    ],
    ids=["not-json", "no-choices", "no-message"],
)
async def test_complete_chat_rejects_malformed_bodies(response: httpx.Response) -> None:
    provider, _ = make_provider(lambda request: response)
    async with provider:
        with pytest.raises(ProviderProtocolError):
            await provider.complete_chat(chat_request())


async def test_complete_chat_maps_errors() -> None:
    provider, recorder = make_provider(lambda request: httpx.Response(502, text="bad gateway"))
    async with provider:
        with pytest.raises(ProviderServerError):
            await provider.complete_chat(chat_request())
    assert recorder.count == 1


# --- Model discovery ----------------------------------------------------------------


async def test_list_models_tolerates_heterogeneous_metadata() -> None:
    listing = {
        "object": "list",
        "data": [
            {"id": "zeta", "object": "model", "owned_by": "Org", "created": 1700000000,
             "active": True, "context_window": 131072, "max_completion_tokens": 32768,
             "public_apps": None},
            {"id": "alpha"},
            {"id": "beta", "context_window": "big", "active": "yes"},
            {"object": "model"},
            {"id": "alpha", "owned_by": "dup"},
        ],
    }
    provider, recorder = make_provider(
        lambda request: httpx.Response(200, json=listing, headers=RATE_HEADERS)
    )
    async with provider:
        catalog = await provider.list_models()
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == "/openai/v1/models"
    assert catalog.ids == ("alpha", "beta", "zeta")
    zeta = catalog.models[2]
    assert (zeta.context_window, zeta.max_completion_tokens, zeta.active) == (131072, 32768, True)
    assert zeta.metadata["public_apps"] is None  # full entry preserved
    beta = catalog.models[1]
    assert beta.context_window is None and beta.active is None
    assert set(catalog.metadata_issues) == {
        "beta: 'context_window' has unexpected type str",
        "beta: 'active' has unexpected type str",
        "entry 3: no string 'id'; skipped",
        "alpha: listed more than once; first entry kept",
    }
    assert catalog.meta.rate_limits.limit_requests == 1000


@pytest.mark.parametrize("payload", [{"data": "x"}, [], {"models": []}])
async def test_list_models_rejects_wrong_shape(payload: Any) -> None:
    provider, _ = make_provider(lambda request: httpx.Response(200, json=payload))
    async with provider:
        with pytest.raises(ProviderProtocolError):
            await provider.list_models()


async def test_list_models_maps_auth_error() -> None:
    provider, _ = make_provider(lambda request: httpx.Response(401, json={"error": {}}))
    async with provider:
        with pytest.raises(ProviderAuthError):
            await provider.list_models()


# --- Call budget --------------------------------------------------------------------


def test_real_provider_requires_a_budget() -> None:
    with pytest.raises(ConfigurationError, match="RealCallBudget"):
        HttpOpenAIProvider(base_url=BASE_URL, source="real", transport=httpx.MockTransport(
            lambda request: httpx.Response(200)))


def test_base_url_must_be_absolute_http() -> None:
    with pytest.raises(ConfigurationError):
        HttpOpenAIProvider(base_url="api.groq.com/openai/v1", source="stand-in")


async def test_budget_is_enforced_before_any_request_is_sent() -> None:
    consumed: list[int] = []
    budget = RealCallBudget(2, on_consume=lambda: consumed.append(1))
    provider, recorder = make_provider(
        lambda request: sse_response(standard_stream()) if request.method == "POST"
        else httpx.Response(200, json={"data": []}),
        budget=budget,
        source="real",
    )
    async with provider:
        await drain(provider)
        await provider.list_models()  # model listing counts as a real request too
        with pytest.raises(RealCallBudgetExceededError):
            await drain(provider)
    assert recorder.count == 2
    assert (budget.used, budget.remaining, len(consumed)) == (2, 0, 2)


def test_budget_rejects_negative_cap() -> None:
    with pytest.raises(ValueError):
        RealCallBudget(-1)


# --- Header parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("2m59.56s", 179.56),
        ("7.66s", 7.66),
        ("120ms", 0.12),
        ("1h2m3s", 3723.0),
        ("30", 30.0),
        ("0s", 0.0),
        ("1.5", 1.5),
        ("500us", 0.0005),
        ("", None),
        ("abc", None),
        ("5x", None),
        ("1.5.5s", None),
        ("s", None),
        ("-3s", None),
    ],
)
def test_parse_duration(text: str, seconds: float | None) -> None:
    assert parse_duration_s(text) == seconds


def test_retry_after_accepts_http_date() -> None:
    now = datetime(2026, 11, 11, 8, 0, 0, tzinfo=UTC)
    assert parse_retry_after_s("Wed, 11 Nov 2026 08:00:30 GMT", now) == 30.0
    assert parse_retry_after_s("not a date", now) is None


def test_malformed_rate_limit_headers_are_kept_raw_and_reported() -> None:
    headers = capture_headers([
        ("X-RateLimit-Limit-Requests", "lots"),
        ("x-ratelimit-reset-tokens", "soon"),
        ("x-ratelimit-remaining-tokens", "12"),
        ("Retry-After", "later"),
        ("x-ratelimit-new-header", "7"),
    ])
    snapshot = parse_rate_limits(headers)
    assert snapshot.limit_requests is None and snapshot.reset_tokens_s is None
    assert snapshot.remaining_tokens == 12
    assert snapshot.raw["x-ratelimit-limit-requests"] == "lots"
    assert snapshot.raw["x-ratelimit-new-header"] == "7"  # unknown headers preserved
    assert len(snapshot.parse_issues) == 3


def test_capture_headers_lowercases_joins_repeats_and_redacts() -> None:
    headers = capture_headers([
        ("Set-Cookie", "a=1"),
        ("Via", "1.1 a"),
        ("via", "1.1 b"),
        ("X-Echo", f"Bearer {FAKE_KEY}"),
    ])
    assert headers["via"] == "1.1 a, 1.1 b"
    assert headers["set-cookie"] == REDACTED
    assert FAKE_KEY not in str(headers)


async def test_secrets_never_appear_in_errors_or_metadata() -> None:
    response = httpx.Response(
        401,
        text=f"invalid key {FAKE_KEY} for org_01abcdefgh",
        headers={"x-echo-authorization": f"Bearer {FAKE_KEY}"},
    )
    provider, _ = make_provider(lambda request: response)
    async with provider:
        with pytest.raises(ProviderAuthError) as info:
            await drain(provider)
    error = info.value
    dumped = " ".join([str(error), repr(error), str(error.body_excerpt),
                       error.meta.model_dump_json() if error.meta else ""])
    assert FAKE_KEY not in dumped
    assert "org_01abcdefgh" not in dumped
    assert FAKE_KEY not in repr(provider)


# --- Request model and scripted provider --------------------------------------------


def test_chat_request_payload_and_prompt_chars() -> None:
    request = chat_request(
        messages=(
            ChatMessage(role="system", content="abc"),
            ChatMessage(role="user", content="de"),
        ),
        reasoning_effort="low",
        seed=7,
    )
    assert request.prompt_chars == 5
    payload = request.to_payload(stream=False)
    assert payload["reasoning_effort"] == "low" and payload["seed"] == 7
    assert "reasoning_format" not in payload and "include_reasoning" not in payload


async def test_scripted_provider_replays_and_fails_on_demand() -> None:
    usage = TokenUsage(prompt_tokens=3, completion_tokens=2)
    failure = ProviderServerError("scripted outage")
    provider = ScriptedProvider(
        [
            ScriptedReply(content_chunks=("a", "b"), reasoning_chunks=("r",), usage=usage),
            ScriptedReply(content_chunks=("x", "y"), error=failure, fail_after_chunks=1),
            ScriptedReply(error=ProviderAuthError("scripted auth")),
        ],
        models=("m1",),
    )
    first = await collect_stream(provider.stream_chat(chat_request()))
    assert (first.content, first.reasoning, first.usage) == ("ab", "r", usage)

    seen: list[str] = []
    with pytest.raises(ProviderServerError):
        async for event in provider.stream_chat(chat_request()):
            seen.append(event.kind)
    assert seen == ["opened", "content"]

    with pytest.raises(ProviderAuthError):
        await provider.complete_chat(chat_request())
    with pytest.raises(ScriptExhaustedError):
        await drain(provider)  # type: ignore[arg-type]
    assert len(provider.requests) == 4
    assert (await provider.list_models()).ids == ("m1",)
