"""The Relay HTTP service (ARCHITECTURE.md sections 5, 7 and 8)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Hashable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, Final, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.config import CacheMode
from relay.control.admission import AdmissionQueue, AdmissionRejectedError
from relay.control.cache import (
    CachedAnswer,
    CacheScope,
    ExactCache,
    Flight,
    SingleFlight,
    normalise_question,
    question_hash,
)
from relay.control.provider_guard import GuardedGenerator, ProviderGuard, Sleep
from relay.control.retry import RetryPolicy
from relay.errors import ContractViolationError, RunStoreError, sanitize_error
from relay.ingestion.registry import CollectionNotFoundError
from relay.orchestrator.pipeline import (
    MAX_QUESTION_CHARS,
    CollectionResolver,
    Pipeline,
    PipelineConfig,
    PipelineOutcome,
    PipelineRequest,
)
from relay.orchestrator.prompt import PROMPT_VERSION
from relay.orchestrator.rewriter import QueryRewriter, RuleQueryRewriter
from relay.orchestrator.tools import DEFAULT_TOOL_TIMEOUT_S, PassageRetriever, ToolGateway
from relay.orchestrator.verifier import CitedPassage, EntailmentScore
from relay.providers.base import (
    ChatProvider,
    ChatRequest,
    CompletionResult,
    ContentDelta,
    ModelCatalog,
    ProviderEvent,
    ReasoningDelta,
)
from relay.providers.probing import CONSERVATIVE_CHARS_PER_TOKEN, status_for_error_code
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.registry import CollectionNotReadyError
from relay.schemas import (
    CacheStatus,
    ErrorCode,
    Source,
    Status,
    UsageSource,
    VerificationVerdict,
)
from relay.telemetry.spans import MonotonicClock, ServerSpanTracker, SpanSummary

logger = logging.getLogger(__name__)

_NS_PER_MS: Final = 1_000_000
_NS_PER_S: Final = 1_000_000_000
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")

HTTP_STATUS: Final[dict[str, int]] = {"ok": 200, "rate_limited": 429, "timeout": 504,
                                      "error": 502}
CLIENT_CLOSED_REQUEST: Final = 499  # nobody is listening; recorded for server logs only

UNAVAILABLE_MESSAGES: Final[dict[str, str]] = {
    "queue_full": "The service is at capacity and did not accept this request.",
    "predicted_wait_exceeds_deadline":
        "The service could not answer within the requested deadline, so it did not start.",
    "upstream_rate_limited":
        "The model provider's rate limit was reached and the retries did not get through.",
    "daily_budget_exhausted":
        "The daily model budget for this service is used up, so no answer can be generated "
        "right now.",
    "deadline_expired": "The deadline for this request passed before an answer was ready.",
    "client_gave_up": "The client stopped waiting for this request.",
    "provider_stalled": "The model provider stopped responding while generating the answer.",
    "upstream_error": "The model provider failed while generating the answer.",
    "circuit_open":
        "The model provider is failing, so calls to it are paused and no answer can be "
        "generated right now.",
    "provider_auth_error": "The service could not authenticate with the model provider.",
    "payload_too_large": "The request is too large for the model provider's limits.",
    "internal_error": "The service failed unexpectedly while handling this request.",
}
POINTER_NOTE: Final = (
    " The passages listed here were the most relevant ones found for the question; no "
    "answer was generated from them."
)


# --- Wire contracts --------------------------------------------------------------------


class AskRequest(BaseModel):
    model_config = _STRICT

    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
    # Required on the v1 path; with a collection_id the collection's preset decides it.
    retrieval_setting: Annotated[str, Field(pattern=r"^[a-z0-9_]+$", max_length=64)] | None = None
    # Milliseconds the server may spend, counted from the request's arrival.
    deadline_ms: Annotated[int, Field(gt=0)] | None = None
    # V2-03: answer from this collection; None = the v1 three-paper path (unchanged).
    collection_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


def _setting(ask: AskRequest) -> str:
    """The resolved retrieval setting (``RelayService`` resolves it before any work)."""
    if ask.retrieval_setting is None:
        raise ContractViolationError(f"request {ask.request_id}: retrieval_setting unresolved")
    return ask.retrieval_setting


class PassagePointer(BaseModel):
    """Where to look in the papers. Carries no passage text."""

    model_config = _STRICT

    chunk_id: str
    paper_id: str
    page_start: int
    page_end: int
    citation: str


class AskReply(BaseModel):
    """Everything the service knows about one finished request."""

    model_config = _STRICT

    request_id: str
    retrieval_setting: str
    status: Status
    error_code: ErrorCode | None
    cache_status: CacheStatus
    verification_verdict: VerificationVerdict
    delivered_text: str | None
    refused: bool
    citations: tuple[CitedPassage, ...]
    message: str | None
    pointers: tuple[PassagePointer, ...]
    provider_called: bool
    degraded: bool
    retries: int
    tokens_in: int
    tokens_out: int
    usage_source: UsageSource
    model_id: str | None
    retrieval_ms: float | None
    queue_depth_at_entry: int | None
    spans: SpanSummary

    def final_event(self) -> dict[str, Any]:
        """Metadata for the ``final`` event, named after the ``results.csv`` columns."""
        return {
            "request_id": self.request_id,
            "retrieval_setting": self.retrieval_setting,
            "status": self.status,
            "error_code": self.error_code,
            "cache_status": self.cache_status,
            "verification_verdict": self.verification_verdict,
            "provider_called": self.provider_called,
            "degraded": self.degraded,
            "retries": self.retries,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usage_source": self.usage_source,
            "model_id": self.model_id,
            "retrieval_ms": self.retrieval_ms,
            "queue_depth_at_entry": self.queue_depth_at_entry,
            **self.spans.model_dump(),
        }


def _sse_event(name: str, data: Mapping[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, sort_keys=True)}\n\n"


def render_sse(reply: AskReply) -> bytes:
    if reply.status == "ok":
        first = _sse_event("answer", {
            "text": reply.delivered_text,
            "refused": reply.refused,
            "citations": [c.model_dump() for c in reply.citations],
        })
    else:
        first = _sse_event("unavailable", {
            "message": reply.message,
            "error_code": reply.error_code,
            "passages": [p.model_dump() for p in reply.pointers],
        })
    return (first + _sse_event("final", reply.final_event())).encode("utf-8")


def http_status_for(reply: AskReply) -> int:
    return 500 if reply.error_code == "internal_error" else HTTP_STATUS[reply.status]


# --- Provider metering -----------------------------------------------------------------


@dataclass
class _CallLedger:
    """What one flight sent to and received from the provider, counted in characters."""

    calls: int = 0
    prompt_chars: int = 0
    received_chars: int = 0


_LEDGER: ContextVar[_CallLedger | None] = ContextVar("relay_call_ledger", default=None)


class MeteredProvider:
    """Counts provider calls per flight, so that cancelled work still reports its spend."""

    def __init__(self, inner: ChatProvider) -> None:
        self._inner = inner

    @property
    def source(self) -> Source:
        return self._inner.source

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        ledger = _LEDGER.get()
        if ledger is not None:
            ledger.calls += 1
            ledger.prompt_chars += request.prompt_chars
        async for event in self._inner.stream_chat(request):
            if ledger is not None and isinstance(event, ContentDelta | ReasoningDelta):
                ledger.received_chars += len(event.text)
            yield event

    async def complete_chat(self, request: ChatRequest) -> CompletionResult:
        return await self._inner.complete_chat(request)

    async def list_models(self) -> ModelCatalog:
        return await self._inner.list_models()

    async def aclose(self) -> None:
        await self._inner.aclose()


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CONSERVATIVE_CHARS_PER_TOKEN)


# --- Service ---------------------------------------------------------------------------


def _pointer(passage: RetrievedPassage) -> PassagePointer:
    return PassagePointer(chunk_id=passage.chunk_id, paper_id=passage.paper_id,
                          page_start=passage.page_start, page_end=passage.page_end,
                          citation=passage.citation)


def _unavailable_message(error_code: str, *, has_pointers: bool) -> str:
    return UNAVAILABLE_MESSAGES[error_code] + (POINTER_NOTE if has_pointers else "")


def _reply_from_outcome(outcome: PipelineOutcome, spans: SpanSummary, *, depth: int | None,
                        cache_status: CacheStatus) -> AskReply:
    delivered = outcome.status == "ok"
    pointers = () if delivered else tuple(_pointer(p) for p in outcome.passages)
    message = None
    if not delivered and outcome.error_code is not None:
        message = _unavailable_message(outcome.error_code, has_pointers=bool(pointers))
    return AskReply(
        request_id=outcome.request_id, retrieval_setting=outcome.retrieval_setting,
        status=outcome.status, error_code=outcome.error_code, cache_status=cache_status,
        verification_verdict=outcome.verification_verdict,
        delivered_text=outcome.delivered_text, refused=outcome.refused,
        citations=outcome.citations, message=message, pointers=pointers,
        provider_called=outcome.provider_called, degraded=outcome.degraded,
        retries=outcome.retries, tokens_in=outcome.tokens_in, tokens_out=outcome.tokens_out,
        usage_source=outcome.usage_source, model_id=outcome.model_id,
        retrieval_ms=outcome.retrieval_ms, queue_depth_at_entry=depth, spans=spans,
    )


def _failure_reply(ask: AskRequest, error_code: ErrorCode, spans: SpanSummary, *,
                   cache_status: CacheStatus, depth: int | None = None) -> AskReply:
    """A request that produced nothing and, as far as this reply knows, cost nothing."""
    return AskReply(
        request_id=ask.request_id, retrieval_setting=_setting(ask),
        status=status_for_error_code(error_code), error_code=error_code,
        cache_status=cache_status, verification_verdict="NOT_RUN", delivered_text=None,
        refused=False, citations=(),
        message=_unavailable_message(error_code, has_pointers=False), pointers=(),
        provider_called=False, degraded=False, retries=0, tokens_in=0, tokens_out=0,
        usage_source="none", model_id=None, retrieval_ms=None, queue_depth_at_entry=depth,
        spans=spans,
    )


def _without_spend(reply: AskReply, ask: AskRequest, spans: SpanSummary,
                   cache_status: CacheStatus) -> AskReply:
    """The same result seen by a request that did not pay for it (a coalesced follower)."""
    return reply.model_copy(update={
        "request_id": ask.request_id, "cache_status": cache_status, "provider_called": False,
        "retries": 0, "tokens_in": 0, "tokens_out": 0, "usage_source": "none",
        "retrieval_ms": None, "queue_depth_at_entry": None, "spans": spans,
    })


@dataclass(frozen=True)
class _Led:
    """Result of one flight, shared by the leader and its followers."""

    flight_id: str
    reply: AskReply


class TraceSink(Protocol):
    """Where the service sends its trace records: a v1 ``RunHandle`` (evidence runs) or
    the v2 request-log writer (``relay.app.request_log.RequestLogSink``)."""

    @property
    def run_id(self) -> str | None: ...

    def append_trace(self, record: Mapping[str, Any]) -> None: ...


class CollectionDirectory(Protocol):
    """What the service needs to know about a collection before any work starts
    (``relay.app.collections.StoreCollectionResolver``)."""

    async def describe(self, collection_id: str) -> tuple[int, str, str]:
        """(version, retrieval setting name, cache tag); raises ``CollectionNotFoundError``
        or ``CollectionNotReadyError``."""
        ...


class Collections(CollectionResolver, CollectionDirectory, Protocol):
    """Both views of the collections: the pipeline's and the service's."""


@dataclass(frozen=True)
class AskTarget:
    """A request with its retrieval setting resolved, and the key its answers are cached
    and coalesced under: the setting on the v1 path; the setting, collection and
    collection version for a collection (a new document bumps the version, so an answer
    from an older version is never served, and two collections never share answers)."""

    ask: AskRequest
    cache_setting: str


class RelayService:
    """Cache -> SingleFlight -> admission -> pipeline, plus traces. One event loop."""

    def __init__(self, *, pipeline: Pipeline, queue: AdmissionQueue,
                 retrieval_settings: frozenset[str], cache: ExactCache | None,
                 model_id: str, guard: ProviderGuard | None = None,
                 run: TraceSink | None = None,
                 clock: MonotonicClock = time.perf_counter_ns,
                 collections: CollectionDirectory | None = None) -> None:
        self.pipeline = pipeline
        self.collections = collections
        self.queue = queue
        self.retrieval_settings = retrieval_settings
        self.cache = cache
        self.model_id = model_id
        self.guard = guard
        self.clock = clock
        self.flights: SingleFlight[_Led] = SingleFlight()
        self.trace_failures = 0
        # V2-07 L4 (advisory): the entailment scorer, set at start-up when the model is
        # installed (compose.py); None means no confidence is computed.
        self.entailment: EntailmentScore | None = None
        # V2-08: standalone-question rewriting for follow-ups on POST /v1/ask (compose.py
        # sets the LLM rewriter for a Groq lane; the rule rewriter is the safe default).
        self.rewriter: QueryRewriter = RuleQueryRewriter()
        self._run = run
        self._flight_ids = itertools.count(1)

    @property
    def cache_mode(self) -> CacheMode:
        return "off" if self.cache is None else "exact"

    @property
    def run_id(self) -> str | None:
        return None if self._run is None else self._run.run_id

    def start_run(self, run: TraceSink | None) -> None:
        """Point traces at ``run`` and empty the cache, so runs cannot contaminate each other."""
        self._run = run
        if self.cache is not None:
            self.cache.clear()

    # --- traces ---

    def record_trace(self, record: Mapping[str, Any]) -> None:
        """Send one trace record to the sink (also used by ``POST /v1/ask``)."""
        self._trace(dict(record))

    def _trace(self, record: dict[str, Any]) -> None:
        if self._run is None:
            return
        try:
            self._run.append_trace(record)
        except (RunStoreError, OSError) as exc:
            # Trace loss is counted (see /healthz) and logged; the answer stays valid.
            self.trace_failures += 1
            logger.error("trace write failed: %s", sanitize_error(exc))

    def _trace_request(self, ask: AskRequest, *, role: str, flight_id: str | None,
                       final: Mapping[str, Any], abandoned: bool = False,
                       source_request_id: str | None = None) -> None:
        self._trace({
            "record": "request", "request_id": ask.request_id, "role": role,
            "collection_id": ask.collection_id,
            "flight_id": flight_id, "source_request_id": source_request_id,
            "question_sha256": question_hash(ask.question), "deadline_ms": ask.deadline_ms,
            "abandoned": abandoned, "final": dict(final),
        })

    def _trace_flight(self, flight_id: str, ask: AskRequest, *, end: str,
                      final: Mapping[str, Any], outcome: PipelineOutcome | None = None,
                      cached: bool = False, error: str | None = None) -> None:
        record: dict[str, Any] = {
            "record": "flight", "flight_id": flight_id, "leader_request_id": ask.request_id,
            "question": ask.question, "question_sha256": question_hash(ask.question),
            "retrieval_setting": _setting(ask), "collection_id": ask.collection_id,
            "cache_mode": self.cache_mode,
            "end": end, "cached": cached, "error": error, "final": dict(final),
        }
        if outcome is not None:
            record.update({
                "kind": outcome.kind, "delivered_text": outcome.delivered_text,
                "refused": outcome.refused,
                "citations": [c.model_dump() for c in outcome.citations],
                "passage_chunk_ids": [p.chunk_id for p in outcome.passages],
                "pipeline": outcome.trace.model_dump(mode="json"),
            })
        if self.guard is not None:
            record["guard"] = self.guard.snapshot().model_dump(mode="json")
        self._trace(record)

    # --- leader work ---

    async def _lead(self, target: AskTarget, spans: ServerSpanTracker,
                    deadline_ns: int | None, flight: Flight[_Led],
                    cache_status: CacheStatus) -> _Led:
        """Admission and pipeline for one flight. Runs detached from any single client."""
        ask = target.ask
        flight_id = f"f{next(self._flight_ids):06d}"
        ledger = _CallLedger()
        _LEDGER.set(ledger)
        request = PipelineRequest(request_id=ask.request_id, question=ask.question,
                                  retrieval_setting=_setting(ask),
                                  collection_id=ask.collection_id)
        depth: int | None = None
        try:
            async with self.queue.admit(deadline_ns=deadline_ns,
                                        is_connected=lambda: flight.has_waiters) as ticket:
                depth = ticket.queue_depth_at_entry
                outcome = await self.pipeline.run(request, spans, deadline_ns=deadline_ns)
        except AdmissionRejectedError as shed:
            reply = _failure_reply(ask, shed.error_code, spans.finish(),
                                   cache_status=cache_status, depth=shed.queue_depth_at_entry)
            self._trace_flight(flight_id, ask, end="shed", final=reply.final_event())
            return _Led(flight_id, reply)
        except asyncio.CancelledError:
            # No waiter is left. Calls already sent may have spent tokens; report them.
            called = ledger.calls > 0
            self._trace_flight(flight_id, ask, end="cancelled", final={
                "request_id": ask.request_id, "retrieval_setting": _setting(ask),
                "status": "timeout", "error_code": "client_gave_up",
                "cache_status": cache_status, "verification_verdict": "NOT_RUN",
                "provider_called": called, "degraded": False,
                "retries": max(0, ledger.calls - 1),
                "tokens_in": _estimate_tokens(ledger.prompt_chars),
                "tokens_out": _estimate_tokens(ledger.received_chars),
                "usage_source": "estimated" if called else "none",
                "model_id": self.model_id if called else None,
                "retrieval_ms": None, "queue_depth_at_entry": depth,
                **spans.snapshot().model_dump(),
            })
            raise
        except Exception as exc:
            logger.error("request %s failed unexpectedly: %s", ask.request_id,
                         sanitize_error(exc))
            reply = _failure_reply(ask, "internal_error", spans.finish(),
                                   cache_status=cache_status, depth=depth)
            if ledger.calls:  # the failure came after provider calls: keep their cost
                reply = reply.model_copy(update={
                    "provider_called": True, "retries": ledger.calls - 1,
                    "tokens_in": _estimate_tokens(ledger.prompt_chars),
                    "tokens_out": _estimate_tokens(ledger.received_chars),
                    "usage_source": "estimated", "model_id": self.model_id,
                })
            self._trace_flight(flight_id, ask, end="internal_error",
                               final=reply.final_event(), error=sanitize_error(exc))
            return _Led(flight_id, reply)

        reply = _reply_from_outcome(outcome, spans.finish(), depth=depth,
                                    cache_status=cache_status)
        cached = False
        if self.cache is not None and outcome.delivered_text is not None:
            cached = self.cache.put(
                ask.question, target.cache_setting,
                CachedAnswer(delivered_text=outcome.delivered_text, refused=outcome.refused,
                             citations=outcome.citations,
                             verification_verdict=outcome.verification_verdict,
                             model_id=outcome.model_id, source_request_id=ask.request_id),
                degraded=outcome.degraded,
            )
        self._trace_flight(flight_id, ask, end="completed", final=reply.final_event(),
                           outcome=outcome, cached=cached)
        return _Led(flight_id, reply)

    # --- one request ---

    def _serve_hit(self, ask: AskRequest, spans: ServerSpanTracker,
                   hit: CachedAnswer) -> AskReply:
        spans.switch("working")
        reply = AskReply(
            request_id=ask.request_id, retrieval_setting=_setting(ask), status="ok",
            error_code=None, cache_status="exact_hit",
            verification_verdict=hit.verification_verdict, delivered_text=hit.delivered_text,
            refused=hit.refused, citations=hit.citations, message=None, pointers=(),
            provider_called=False, degraded=False, retries=0, tokens_in=0, tokens_out=0,
            usage_source="none", model_id=hit.model_id, retrieval_ms=None,
            queue_depth_at_entry=None, spans=spans.finish(),
        )
        self._trace_request(ask, role="cache_hit", flight_id=None, final=reply.final_event(),
                            source_request_id=hit.source_request_id)
        return reply

    async def target(self, ask: AskRequest) -> AskTarget:
        """Resolve the request's collection, or keep its v1 setting. Raises
        ``CollectionNotFoundError`` / ``CollectionNotReadyError`` before any work."""
        if ask.collection_id is None:
            return AskTarget(ask, _setting(ask))
        if self.collections is None:
            raise ContractViolationError("this service has no collections (it needs the "
                                         "metadata store, cache.mode = sqlite)")
        version, setting, tag = await self.collections.describe(ask.collection_id)
        resolved = ask.model_copy(update={"retrieval_setting": setting})
        return AskTarget(resolved, f"{setting}@{ask.collection_id}:v{version}:{tag}")

    async def ask(self, ask: AskRequest, spans: ServerSpanTracker, arrived_ns: int, *,
                  target: AskTarget | None = None) -> AskReply:
        """Serve one request (``target`` = the already resolved ``self.target(ask)``)."""
        target = target if target is not None else await self.target(ask)
        ask = target.ask
        deadline_ns = (None if ask.deadline_ms is None
                       else arrived_ns + ask.deadline_ms * _NS_PER_MS)
        key: Hashable
        if self.cache is None:
            led_status: CacheStatus = "off"
            key = ("off", ask.request_id, id(spans))  # unique: nothing is ever coalesced
        else:
            hit = self.cache.get(ask.question, target.cache_setting)
            if hit is not None:
                return self._serve_hit(ask, spans, hit)
            led_status = "miss"
            key = (normalise_question(ask.question), target.cache_setting)

        leading = not self.flights.in_flight(key)
        my_status: CacheStatus = led_status if leading else "coalesced"
        role = "leader" if leading else "follower"
        wait_s = (None if deadline_ns is None
                  else (deadline_ns - self.clock()) / _NS_PER_S)
        try:
            result = await self.flights.do(
                key, lambda flight: self._lead(target, spans, deadline_ns, flight, led_status),
                follower_timeout_s=wait_s,
            )
        except TimeoutError:
            # Only a follower's own wait is bounded here: its deadline passed first.
            reply = _failure_reply(ask, "deadline_expired", spans.finish(),
                                   cache_status=my_status)
            self._trace_request(ask, role=role, flight_id=None, final=reply.final_event())
            return reply
        except asyncio.CancelledError:
            # A leader's tracker belongs to its flight, which may still be running.
            summary = spans.snapshot() if leading else spans.finish()
            final: dict[str, Any] = {
                "request_id": ask.request_id, "retrieval_setting": _setting(ask),
                "status": "timeout", "error_code": "client_gave_up",
                "cache_status": my_status, "verification_verdict": "NOT_RUN",
                **summary.model_dump(),
            }
            if leading:
                final["spend"] = "see the flight record for this leader_request_id"
            else:
                final.update({"provider_called": False, "retries": 0, "tokens_in": 0,
                              "tokens_out": 0, "usage_source": "none"})
            self._trace_request(ask, role=role, flight_id=None, final=final, abandoned=True)
            raise

        led = result.value
        reply = led.reply if leading else _without_spend(led.reply, ask, spans.finish(),
                                                         "coalesced")
        self._trace_request(ask, role=role, flight_id=led.flight_id,
                            final=reply.final_event(),
                            source_request_id=led.reply.request_id)
        return reply

    def health(self) -> dict[str, Any]:
        guard = None if self.guard is None else self.guard.snapshot()
        report: dict[str, Any] = {
            "status": "ok", "run_id": self.run_id, "cache_mode": self.cache_mode,
            "cache_entries": 0 if self.cache is None else len(self.cache),
            "in_flight": len(self.flights), "queue_depth": self.queue.depth,
            "queue_active": self.queue.active, "queue_capacity": self.queue.capacity,
            "daily_stop": None if guard is None else guard.daily_stop,
            "circuit": None if guard is None else guard.circuit,
            "trace_failures": self.trace_failures,
        }
        stats = getattr(self._run, "stats", None)  # v2 sinks report their own counters
        if callable(stats):
            report["trace_sink"] = stats()
        return report


# --- HTTP layer ------------------------------------------------------------------------


async def _client_disconnected(request: Request) -> None:
    """Return when the ASGI server reports that the client went away."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _invalid(details: list[dict[str, Any]]) -> JSONResponse:
    return JSONResponse({"error": "invalid_request", "details": details}, status_code=422)


def _parse(raw: bytes, settings: frozenset[str], *,
           collections: bool = False) -> AskRequest | JSONResponse:
    try:
        ask = AskRequest.model_validate_json(raw)
    except ValidationError as exc:
        # Location and error type only: the rejected input is not echoed back.
        return _invalid([{"loc": [str(part) for part in err["loc"]], "type": err["type"]}
                         for err in exc.errors()])
    if not ask.question.strip():
        return _invalid([{"loc": ["question"], "type": "blank"}])
    if ask.collection_id is not None:
        if ask.retrieval_setting is not None:  # the collection's preset decides it
            return _invalid([{"loc": ["retrieval_setting"], "type": "set_by_collection"}])
        if not collections:
            return _invalid([{"loc": ["collection_id"], "type": "collections_disabled"}])
        return ask
    if ask.retrieval_setting is None:
        return _invalid([{"loc": ["retrieval_setting"], "type": "missing"}])
    if ask.retrieval_setting not in settings:
        return _invalid([{"loc": ["retrieval_setting"], "type": "unknown_setting",
                          "allowed": sorted(settings)}])
    return ask


def build_app(service: RelayService) -> FastAPI:
    app = FastAPI(title="The Relay", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.relay = service

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return service.health()

    @app.post("/ask")
    async def ask(request: Request) -> Response:
        arrived_ns = service.clock()
        spans = ServerSpanTracker(service.clock)
        parsed = _parse(await request.body(), service.retrieval_settings,
                        collections=service.collections is not None)
        if isinstance(parsed, JSONResponse):
            return parsed
        try:
            target = await service.target(parsed)
        except CollectionNotFoundError:
            return JSONResponse({"error": "collection_not_found",
                                 "message": "no such collection"}, status_code=404)
        except CollectionNotReadyError:
            return JSONResponse({"error": "collection_not_ready",
                                 "message": "the collection has no indexed documents yet"},
                                status_code=409)
        serve = asyncio.create_task(service.ask(parsed, spans, arrived_ns, target=target))
        gone = asyncio.create_task(_client_disconnected(request))
        try:
            await asyncio.wait({serve, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not serve.done():  # the client left first
                serve.cancel()
                await asyncio.gather(serve, return_exceptions=True)
                return Response(status_code=CLIENT_CLOSED_REQUEST)
            reply = serve.result()
        finally:
            for task in (serve, gone):
                if not task.done():
                    task.cancel()
        return Response(render_sse(reply), status_code=http_status_for(reply),
                        media_type="text/event-stream",
                        headers={"cache-control": "no-store"})

    return app


def create_app(*, provider: ChatProvider, retrievers: Mapping[str, PassageRetriever],
               top_k: Mapping[str, int], pipeline_config: PipelineConfig,
               guard: ProviderGuard, queue: AdmissionQueue, corpus_sha256: str,
               cache_mode: CacheMode = "exact", retry: RetryPolicy | None = None,
               vocabulary: frozenset[str] | None = None, run: TraceSink | None = None,
               tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S, sleep: Sleep = asyncio.sleep,
               clock: MonotonicClock = time.perf_counter_ns,
               collections: Collections | None = None) -> FastAPI:
    """Wire pipeline + ProviderGuard + admission + exact cache + SingleFlight into an app."""
    gateway = ToolGateway(retrievers, timeout_s=tool_timeout_s, clock=clock)
    generator = GuardedGenerator(MeteredProvider(provider), guard, retry, sleep=sleep,
                                 clock=clock)
    pipeline = Pipeline(gateway=gateway, generator=generator, config=pipeline_config,
                        top_k=top_k, vocabulary=vocabulary, budget=guard,
                        collections=collections)
    cache = None
    if cache_mode == "exact":
        cache = ExactCache(CacheScope(corpus_sha256=corpus_sha256,
                                      prompt_version=PROMPT_VERSION,
                                      model_id=pipeline_config.model_id))
    service = RelayService(pipeline=pipeline, queue=queue,
                           retrieval_settings=gateway.settings, cache=cache,
                           model_id=pipeline_config.model_id, guard=guard, run=run,
                           clock=clock, collections=collections)
    return build_app(service)


__all__ = [
    "AskReply",
    "AskRequest",
    "AskTarget",
    "CollectionDirectory",
    "Collections",
    "MeteredProvider",
    "PassagePointer",
    "RelayService",
    "build_app",
    "create_app",
    "http_status_for",
    "render_sse",
]
