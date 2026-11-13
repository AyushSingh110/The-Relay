"""The stand-in model service: an OpenAI-compatible FastAPI app (ARCHITECTURE.md section 12).

    POST /openai/v1/chat/completions   streamed (SSE + usage chunk) or plain JSON
    GET  /openai/v1/models
    GET  /admin/config     POST /admin/config   (partial override, validated)
    POST /admin/reset      GET  /admin/stats

Behaviour comes from a :class:`StandinProfile`: calibrated latency, a concurrency knee,
seeded 5xx errors, slow stragglers and stalled streams, and token-bucket RPM / TPM / RPD /
TPD limits. A limited request gets ``429`` with ``retry-after``, a Groq-style error body
(documented format; never observed on Groq) and the same six ``x-ratelimit-*`` headers
Groq sends, with the meanings the P02-LIVE evidence is consistent with:
``*-requests`` = the daily request bucket, ``*-tokens`` = the per-minute token bucket,
``reset-*`` = time until that bucket is full again.

Answers are extractive: the passage sentences with the most words in common with the
question, each tagged ``[Pi]``; ``INSUFFICIENT_EVIDENCE`` when nothing overlaps; ``OK``
for prompts without passages. They test orchestration, timing and citation plumbing,
**not** answer quality.

Rate-limit windows run on the injected ``clock`` (seconds), so tests can move time.
Latency uses the injected ``sleep``.

Run as its own process:  python -m relay.standin.server --port 8001 [--profile FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import random
import re
import socket
import time
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import Receive, Scope, Send

from relay.standin.profile import (
    SECONDS_PER_DAY,
    ModelProfile,
    StandinProfile,
    load_factor,
    output_tokens,
    prompt_tokens,
    request_rng,
    sample_header_delay_ms,
    uncalibrated_defaults,
)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]
Metric = Literal["RPM", "TPM", "RPD", "TPD"]

API_PREFIX: Final = "/openai/v1"
REFUSAL: Final = "INSUFFICIENT_EVIDENCE"
MAX_ANSWER_SENTENCES: Final = 2
REASONING_PIECE: Final = "thinking "
_WINDOW_S: Final[dict[Metric, float]] = {"RPM": 60.0, "TPM": 60.0, "RPD": SECONDS_PER_DAY,
                                         "TPD": SECONDS_PER_DAY}
_METRIC_WORDS: Final[dict[Metric, str]] = {
    "RPM": "requests per minute", "TPM": "tokens per minute",
    "RPD": "requests per day", "TPD": "tokens per day",
}
_STOPWORDS: Final = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "does", "do", "for", "from", "has",
    "have", "how", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "with", "use", "used", "uses", "using",
    "paper", "model",
})
_WORD = re.compile(r"[a-z0-9_]+")
_PASSAGE_HEAD = re.compile(r"^\[P(\d+)\] .*$", re.MULTILINE)
_QUESTION = re.compile(r"\n\nQuestion:\s*(.+?)(?:\n\nAnswer using|\Z)", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


# --- Request contract ------------------------------------------------------------------


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant"]
    content: str


class ChatPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: Annotated[str, Field(min_length=1)]
    messages: Annotated[list[_Message], Field(min_length=1)]
    max_completion_tokens: Annotated[int, Field(ge=1)] | None = None
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None

    @property
    def prompt_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    @property
    def include_usage(self) -> bool:
        return bool((self.stream_options or {}).get("include_usage"))


def _error(status: int, message: str, *, type_: str, code: str | None = None,
           headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, "code": code}},
                        status_code=status, headers=dict(headers or {}))


# --- Extractive answers ----------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


def parse_rag_prompt(text: str) -> tuple[str, list[tuple[int, str]]]:
    """(question, [(tag number, passage text)]) from a P05-style user message."""
    question_match = _QUESTION.search(text)
    question = question_match.group(1).strip() if question_match else ""
    body = text[: question_match.start()] if question_match else text
    heads = list(_PASSAGE_HEAD.finditer(body))
    passages: list[tuple[int, str]] = []
    for position, head in enumerate(heads):
        end = heads[position + 1].start() if position + 1 < len(heads) else len(body)
        passages.append((int(head.group(1)), body[head.end():end].strip()))
    return question, passages


def extractive_answer(messages: Sequence[_Message]) -> str:
    user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    question, passages = parse_rag_prompt(user)
    if not passages:
        return "OK"
    wanted = _words(question)
    scored: list[tuple[int, int, int, str]] = []
    for order, (tag, text) in enumerate(passages):
        for position, sentence in enumerate(_SENTENCE_END.split(text)):
            sentence = " ".join(sentence.split())
            if len(sentence.split()) < 4:
                continue
            overlap = len(wanted & _words(sentence))
            if overlap:
                scored.append((-overlap, order * 1000 + position, tag, sentence))
    if not scored:
        return REFUSAL
    chosen = sorted(scored)[:MAX_ANSWER_SENTENCES]
    chosen.sort(key=lambda item: item[1])  # keep passage order
    return " ".join(f"{s.rstrip('.!?;:, ')} [P{tag}]." for _, _, tag, s in chosen)


# --- Rate limiting ---------------------------------------------------------------------


class TokenBucket:
    """Continuous refill at ``capacity / window_s`` per second, starting full."""

    def __init__(self, capacity: int, window_s: float, now: float) -> None:
        self.capacity = capacity
        self.rate = capacity / window_s
        self.level = float(capacity)
        self._at = now

    def refill(self, now: float) -> None:
        self.level = min(float(self.capacity), self.level + (now - self._at) * self.rate)
        self._at = now

    def wait_s(self, amount: float) -> float:
        return max(0.0, (amount - self.level) / self.rate)

    def until_full_s(self) -> float:
        return (self.capacity - self.level) / self.rate

    @property
    def remaining(self) -> int:
        return max(0, math.floor(self.level))


def format_duration(seconds: float) -> str:
    """Groq-style duration: ``2.28s``, ``1m26.4s``, ``7m12s``, ``1h2m3s``."""
    seconds = max(0.0, round(seconds, 3))
    hours, rest = divmod(seconds, 3600.0)
    minutes, secs = divmod(rest, 60.0)
    text = ""
    if hours:
        text += f"{int(hours)}h"
    if minutes:
        text += f"{int(minutes)}m"
    shown = f"{secs:.3f}".rstrip("0").rstrip(".")
    if shown != "0" or not text:
        text += f"{shown}s"
    return text


@dataclass
class _Charge:
    tokens: int
    buckets: dict[Metric, TokenBucket]


class RateLimiter:
    def __init__(self, profile: StandinProfile, clock: Clock) -> None:
        self._clock = clock
        now = clock()
        limits = profile.limits
        configured: dict[Metric, int | None] = {"RPM": limits.rpm, "TPM": limits.tpm,
                                                "RPD": limits.rpd, "TPD": limits.tpd}
        self.buckets: dict[Metric, TokenBucket] = {
            metric: TokenBucket(limit, _WINDOW_S[metric], now)
            for metric, limit in configured.items() if limit is not None
        }

    def _refill(self) -> None:
        now = self._clock()
        for bucket in self.buckets.values():
            bucket.refill(now)

    def exhaust(self, metric: Metric) -> None:
        if metric not in self.buckets:
            raise ValueError(f"{metric} is not enforced, so it cannot be exhausted")
        self._refill()
        self.buckets[metric].level = 0.0

    def too_large(self, tokens: int) -> Metric | None:
        for metric in ("TPM", "TPD"):
            bucket = self.buckets.get(metric)  # type: ignore[call-overload]
            if bucket is not None and tokens > bucket.capacity:
                return metric  # type: ignore[return-value]
        return None

    def check(self, tokens: int) -> tuple[Metric, float, int, int] | None:
        """The first limit this request would break: (metric, wait_s, used, requested).
        Daily limits are checked first: their waits are the long ones."""
        self._refill()
        for metric in ("RPD", "TPD", "RPM", "TPM"):
            bucket = self.buckets.get(metric)  # type: ignore[call-overload]
            if bucket is None:
                continue
            amount = 1 if metric in ("RPD", "RPM") else tokens
            if bucket.level < amount:
                used = bucket.capacity - bucket.remaining
                return metric, bucket.wait_s(amount), used, amount  # type: ignore[return-value]
        return None

    def take(self, tokens: int) -> _Charge:
        for metric, bucket in self.buckets.items():
            bucket.level -= 1 if metric in ("RPD", "RPM") else tokens
        return _Charge(tokens, {m: b for m, b in self.buckets.items() if m in ("TPM", "TPD")})

    def settle(self, charge: _Charge, actual_tokens: int) -> None:
        """Give back the part of the reservation that was not used."""
        unused = charge.tokens - actual_tokens
        if unused <= 0:
            return
        self._refill()
        for bucket in charge.buckets.values():
            bucket.level = min(float(bucket.capacity), bucket.level + unused)

    def headers(self) -> dict[str, str]:
        """Groq's six headers: requests = the daily bucket, tokens = the minute bucket."""
        self._refill()
        out: dict[str, str] = {}
        for metric, name in (("RPD", "requests"), ("TPM", "tokens")):
            bucket = self.buckets.get(metric)  # type: ignore[call-overload]
            if bucket is None:
                continue
            out[f"x-ratelimit-limit-{name}"] = str(bucket.capacity)
            out[f"x-ratelimit-remaining-{name}"] = str(bucket.remaining)
            out[f"x-ratelimit-reset-{name}"] = format_duration(bucket.until_full_s())
        return out

    def levels(self) -> dict[str, dict[str, float]]:
        self._refill()
        return {m: {"capacity": b.capacity, "level": round(b.level, 3)}
                for m, b in self.buckets.items()}


# --- Service state ---------------------------------------------------------------------


@dataclass
class Stats:
    requests: int = 0
    by_status: Counter[int] = field(default_factory=Counter)
    rate_limited: Counter[str] = field(default_factory=Counter)
    errors_injected: int = 0
    stragglers: int = 0
    stalls: int = 0
    completed_streams: int = 0
    disconnects: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    peak_active: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "by_status": {str(k): v for k, v in sorted(self.by_status.items())},
            "rate_limited": dict(sorted(self.rate_limited.items())),
            "errors_injected": self.errors_injected, "stragglers": self.stragglers,
            "stalls": self.stalls, "completed_streams": self.completed_streams,
            "disconnects": self.disconnects, "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens, "peak_active": self.peak_active,
        }


class _Slot:
    """One in-flight request's share of ``active``. Releasing twice is harmless."""

    def __init__(self, state: StandinState) -> None:
        self._state = state
        self._held = True
        state.active += 1
        state.stats.peak_active = max(state.stats.peak_active, state.active)

    def release(self) -> None:
        if self._held:
            self._held = False
            self._state.active -= 1


class StandinState:
    def __init__(self, profile: StandinProfile, clock: Clock) -> None:
        self.clock = clock
        self.overrides: list[dict[str, Any]] = []
        self.base_profile = profile
        self.reset(profile)

    def reset(self, profile: StandinProfile | None = None) -> None:
        if profile is not None:
            self.profile = profile
        self.limiter = RateLimiter(self.profile, self.clock)
        self.stats = Stats()
        self.active = 0
        self._indices = itertools.count()

    def next_rng(self) -> random.Random:
        return request_rng(self.profile.seed, next(self._indices))


class _SlotStreamingResponse(StreamingResponse):
    """Frees the request's slot even when the client leaves before the body starts."""

    def __init__(self, content: AsyncIterator[str], slot: _Slot, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._slot = slot

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slot.release()


class _Pacer:
    """Paces a stream against cumulative deadlines on a high-resolution clock.

    Sleeping a fixed ~1-2 ms per token is inaccurate wherever the event loop's clock is
    coarse (15.6 ms on Windows): sleeps return early or late by up to one tick, and the
    error adds up over a stream. Waiting for "start + tokens so far x gap" instead makes
    each error self-correcting, so a whole stream is off by at most about one tick.
    """

    def __init__(self, sleep: Sleep, clock: Callable[[], float] = time.perf_counter) -> None:
        self._sleep = sleep
        self._clock = clock
        self._start = clock()
        self._due_s = 0.0

    async def wait(self, seconds: float) -> None:
        self._due_s += seconds
        delay = self._start + self._due_s - self._clock()
        if delay > 0:
            await self._sleep(delay)


# --- Generation plan -------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    header_delay_s: float
    token_delay_s: float
    reasoning_tokens: int
    pieces: tuple[tuple[str, int], ...]  # (text, tokens)
    finish_reason: str
    stall: bool
    prompt_tokens: int

    @property
    def completion_tokens(self) -> int:
        return self.reasoning_tokens + sum(tokens for _, tokens in self.pieces)


def _plan(state: StandinState, model: ModelProfile, payload: ChatPayload, n_prompt: int,
          max_completion: int, rng: random.Random) -> _Plan:
    profile = state.profile
    scale = profile.time_scale * load_factor(profile, state.active)
    straggler = rng.random() < profile.faults.straggler_rate
    stall = rng.random() < profile.faults.stall_rate
    if straggler:
        scale *= profile.faults.straggler_multiplier
        state.stats.stragglers += 1
    header_ms = sample_header_delay_ms(model, n_prompt, rng)
    reasoning = min(model.latency.reasoning_tokens, max_completion)
    budget = max_completion - reasoning
    pieces: list[tuple[str, int]] = []
    finish = "stop"
    for piece in re.findall(r"\S+\s*", extractive_answer(payload.messages)):
        tokens = output_tokens(model, piece.strip())
        if tokens > budget:
            finish = "length"
            break
        pieces.append((piece, tokens))
        budget -= tokens
    return _Plan(header_delay_s=header_ms / 1000.0 * scale,
                 token_delay_s=model.latency.inter_token_ms / 1000.0 * scale,
                 reasoning_tokens=reasoning, pieces=tuple(pieces), finish_reason=finish,
                 stall=stall, prompt_tokens=n_prompt)


def _usage(plan: _Plan) -> dict[str, Any]:
    completion_time = plan.completion_tokens * plan.token_delay_s
    return {
        "prompt_tokens": plan.prompt_tokens, "completion_tokens": plan.completion_tokens,
        "total_tokens": plan.prompt_tokens + plan.completion_tokens,
        "completion_tokens_details": {"reasoning_tokens": plan.reasoning_tokens},
        "completion_time": round(completion_time, 6),
        "total_time": round(plan.header_delay_s + completion_time, 6),
    }


def _chunk(response_id: str, model_id: str, created: int, delta: dict[str, Any],
           finish: str | None = None) -> str:
    data = {"id": response_id, "object": "chat.completion.chunk", "created": created,
            "model": model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(data)}\n\n"


# --- App -------------------------------------------------------------------------------


def create_standin_app(profile: StandinProfile | None = None, *,
                       clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep) -> FastAPI:
    state = StandinState(profile or uncalibrated_defaults(), clock)
    app = FastAPI(title="The Relay stand-in", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.standin = state
    ids = itertools.count(1)

    @app.get(f"{API_PREFIX}/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [
            {"id": model_id, "object": "model", "owned_by": "relay-standin", "created": 0,
             "active": True, "context_window": m.context_window,
             "max_completion_tokens": m.max_completion_tokens}
            for model_id, m in sorted(state.profile.models.items())
        ]}

    @app.post(f"{API_PREFIX}/chat/completions")
    async def chat(request: Request) -> Response:
        state.stats.requests += 1
        response = await _chat(request)
        state.stats.by_status[response.status_code] += 1
        return response

    async def _chat(request: Request) -> Response:
        try:
            payload = ChatPayload.model_validate_json(await request.body())
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            return _error(400, f"invalid request body: {', '.join(fields)}",
                          type_="invalid_request_error")
        model = state.profile.models.get(payload.model)
        if model is None:
            return _error(404, f"The model `{payload.model}` does not exist.",
                          type_="invalid_request_error", code="model_not_found")
        max_completion = (payload.max_completion_tokens or payload.max_tokens
                          or state.profile.default_max_completion_tokens)
        n_prompt = prompt_tokens(model, payload.prompt_chars)
        charge_tokens = n_prompt + (max_completion if state.profile.limits.tpm_charge
                                    == "prompt_plus_max_completion" else 0)
        limiter = state.limiter

        too_large = limiter.too_large(charge_tokens)
        if too_large is not None:
            capacity = limiter.buckets[too_large].capacity
            return _error(413, (
                f"Request too large for model `{payload.model}` on "
                f"{_METRIC_WORDS[too_large]} ({too_large}): Limit {capacity}, Requested "
                f"{charge_tokens}, please reduce your message size and try again."),
                type_="tokens", code="rate_limit_exceeded", headers=limiter.headers())

        breach = limiter.check(charge_tokens)
        if breach is not None:
            metric, wait_s, used, requested = breach
            state.stats.rate_limited[metric] += 1
            headers = limiter.headers()
            headers["retry-after"] = str(max(1, math.ceil(wait_s)))
            return _error(429, (
                f"Rate limit reached for model `{payload.model}` in organization `standin` "
                f"service tier `on_demand` on {_METRIC_WORDS[metric]} ({metric}): Limit "
                f"{limiter.buckets[metric].capacity}, Used {used}, Requested {requested}. "
                f"Please try again in {format_duration(wait_s)}."),
                type_="requests" if metric in ("RPM", "RPD") else "tokens",
                code="rate_limit_exceeded", headers=headers)

        rng = state.next_rng()
        if rng.random() < state.profile.faults.error_rate:
            state.stats.errors_injected += 1
            status = state.profile.faults.error_status
            return _error(status, "injected stand-in server error",
                          type_="internal_server_error" if status == 500 else "service_unavailable")

        charge = limiter.take(charge_tokens)
        slot = _Slot(state)
        try:
            plan = _plan(state, model, payload, n_prompt, max_completion, rng)
            await sleep(plan.header_delay_s)
        except BaseException:
            slot.release()
            raise
        headers = limiter.headers()
        response_id = f"chatcmpl-standin-{next(ids):06d}"
        if not payload.stream:
            return await _plain(payload, plan, charge, slot, response_id, headers)
        return _SlotStreamingResponse(
            _stream(payload, plan, charge, slot, response_id), slot,
            media_type="text/event-stream",
            headers={**headers, "cache-control": "no-cache"},
        )

    async def _plain(payload: ChatPayload, plan: _Plan, charge: _Charge, slot: _Slot,
                     response_id: str, headers: dict[str, str]) -> Response:
        try:
            await sleep(plan.token_delay_s * plan.completion_tokens)
            content = "".join(text for text, _ in plan.pieces)
            reasoning = REASONING_PIECE * plan.reasoning_tokens
            _finish(plan, charge)
            return JSONResponse({
                "id": response_id, "object": "chat.completion", "created": 0,
                "model": payload.model,
                "choices": [{"index": 0, "finish_reason": plan.finish_reason,
                             "message": {"role": "assistant", "content": content,
                                         **({"reasoning": reasoning} if reasoning else {})}}],
                "usage": _usage(plan),
            }, headers=headers)
        finally:
            slot.release()

    def _finish(plan: _Plan, charge: _Charge) -> None:
        if state.profile.limits.settle_to_usage:
            state.limiter.settle(charge, plan.prompt_tokens + plan.completion_tokens)
        state.stats.prompt_tokens += plan.prompt_tokens
        state.stats.completion_tokens += plan.completion_tokens

    async def _stream(payload: ChatPayload, plan: _Plan, charge: _Charge, slot: _Slot,
                      response_id: str) -> AsyncIterator[str]:
        faults = state.profile.faults
        completed = False
        pace = _Pacer(sleep)
        try:
            yield _chunk(response_id, payload.model, 0, {"role": "assistant", "content": ""})
            for _ in range(plan.reasoning_tokens):
                await pace.wait(plan.token_delay_s)
                yield _chunk(response_id, payload.model, 0, {"reasoning": REASONING_PIECE})
            for position, (text, tokens) in enumerate(plan.pieces):
                if plan.stall and position == faults.stall_after_pieces:
                    state.stats.stalls += 1
                    await sleep(faults.stall_s * state.profile.time_scale)
                    return  # the connection closes without [DONE]
                await pace.wait(plan.token_delay_s * tokens)
                yield _chunk(response_id, payload.model, 0, {"content": text})
            if plan.stall and len(plan.pieces) <= faults.stall_after_pieces:
                state.stats.stalls += 1
                await sleep(faults.stall_s * state.profile.time_scale)
                return
            yield _chunk(response_id, payload.model, 0, {}, finish=plan.finish_reason)
            usage = _usage(plan)
            _finish(plan, charge)
            final: dict[str, Any] = {"id": response_id, "object": "chat.completion.chunk",
                                     "created": 0, "model": payload.model, "choices": [],
                                     "x_groq": {"id": response_id, "usage": usage}}
            if payload.include_usage:
                final["usage"] = usage
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"
            completed = True
            state.stats.completed_streams += 1
        finally:
            if not completed and not plan.stall:
                state.stats.disconnects += 1
            slot.release()

    @app.get("/admin/config")
    async def get_config() -> dict[str, Any]:
        return {"profile": state.profile.model_dump(mode="json"),
                "overrides": state.overrides}

    @app.post("/admin/config")
    async def set_config(request: Request) -> Response:
        try:
            patch = json.loads(await request.body() or b"{}")
            if not isinstance(patch, dict):
                raise ValueError("expected a JSON object")
            profile = state.profile.with_overrides(patch)
        except (ValueError, ValidationError) as exc:
            detail = (sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
                      if isinstance(exc, ValidationError) else [str(exc)])
            return JSONResponse({"error": "invalid_config", "details": detail},
                                status_code=422)
        limits_changed = profile.limits != state.profile.limits
        state.profile = profile
        state.overrides.append(patch)
        if limits_changed:
            state.limiter = RateLimiter(profile, state.clock)  # new limits start full
        return JSONResponse({"profile": profile.model_dump(mode="json"),
                             "overrides": state.overrides})

    @app.post("/admin/reset")
    async def reset(request: Request) -> Response:
        """Full buckets, zero counters, restart the seeded sequence. Optional body:
        {"exhaust": ["RPD", "TPD", ...], "restore_profile": true}."""
        try:
            body = json.loads(await request.body() or b"{}")
            exhaust = body.get("exhaust", []) if isinstance(body, dict) else None
            if not isinstance(exhaust, list) or not all(m in _WINDOW_S for m in exhaust):
                raise ValueError("exhaust must be a list of RPM, TPM, RPD, TPD")
            restore = bool(body.get("restore_profile", False))
            state.reset(state.base_profile if restore else None)
            if restore:
                state.overrides = []
            for metric in exhaust:
                state.limiter.exhaust(metric)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_reset", "details": [str(exc)]},
                                status_code=422)
        return JSONResponse({"reset": True, "exhausted": exhaust,
                             "limits": state.limiter.levels()})

    @app.get("/admin/stats")
    async def stats() -> dict[str, Any]:
        return {"label": state.profile.label, "active": state.active,
                **state.stats.as_dict(), "limits": state.limiter.levels()}

    return app


@asynccontextmanager
async def serve_on_loopback(app: FastAPI) -> AsyncGenerator[str]:
    """Run ``app`` under uvicorn on 127.0.0.1 (a free port); yields the API base URL."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()  # surfaces a startup failure
                raise RuntimeError("stand-in server stopped during startup")
            await asyncio.sleep(0.005)
        yield f"http://127.0.0.1:{port}{API_PREFIX}"
    finally:
        server.should_exit = True
        await task
        sock.close()


def load_profile(path: Path) -> StandinProfile:
    return StandinProfile.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the stand-in model service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--profile", type=Path, default=None,
                        help="profile.json from a calibration run (default: "
                             "uncalibrated_defaults)")
    args = parser.parse_args(argv)
    profile = load_profile(args.profile) if args.profile else uncalibrated_defaults()
    print(f"stand-in profile: {profile.label} "
          f"(calibration run: {profile.calibration_run_id or 'none'})")
    uvicorn.run(create_standin_app(profile), host=args.host, port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ChatPayload",
    "RateLimiter",
    "StandinState",
    "TokenBucket",
    "create_standin_app",
    "extractive_answer",
    "format_duration",
    "load_profile",
    "main",
    "parse_rag_prompt",
    "serve_on_loopback",
]
