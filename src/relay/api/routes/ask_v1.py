"""POST /v1/ask: verified streaming over SSE (plan V2-05; docs/ARCHITECTURE_V2.md §6).

Events, in order, on one SSE connection:

    accepted  request_id, queue_position, expected_wait_ms   (first, before any work)
    status    stage: queued | retrieving | generating | verifying   (+ keepalives)
    sources   passages: [{id, chunk_id, paper_id, short_name, page_range, section,
                          text_excerpt, text}]   (text: the whole passage, V2-06)
    sentence  text, citations: ["P1", ...]   (only sentences that passed the verifier)
    stopped   reason_class, partial           (the answer stopped; no unverified text)
    final     status, verdict, tokens_in, tokens_out, ttft_ms, total_ms, cache_status,
              coalesced, partial, refused, message, ...   (always the last event)

How the rules are kept:

- ``accepted`` is the first thing the response sends; the work starts only afterwards.
- While nothing else happens, a ``status`` with the current stage (and live queue
  position) is repeated every ``keepalive_s`` (default 4 s), so the connection is never
  silent for 5 s.
- ``sentence`` events come only from ``SentenceStream``, which releases a sentence only
  after it passed the per-sentence verifier; a failed sentence ends the stream (generation
  is cancelled) and is never sent. When every sentence passed, the v1 whole-answer verifier
  runs as the final pass; if it fails, ``stopped`` (``final_verification_failed``) is sent.
- Only fully verified, complete answers are cached.
- Coalescing: requests for the same question and collection state share one flight. The
  flight's events are recorded in a ``ReplayChannel``; a follower that joins mid-stream
  receives every event so far, then the live ones. Each request gets its own ``accepted``
  and ``final``. When the last listener disconnects, the flight (and generation) is
  cancelled and its queue slot freed.
- A collection with ``buffered = true`` uses the buffered path (one model call, the
  whole-answer verifier, then every sentence at once), with the same event sequence.

API keys are stubbed as for the other /v1 routes: any Bearer key is the dev tenant.
``POST /ask`` (v1, used by the load harness) is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from collections.abc import AsyncIterator, Callable, Hashable
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.api.app import UNAVAILABLE_MESSAGES, AskRequest, AskTarget, RelayService
from relay.app.bootstrap import V1_COLLECTION_ID
from relay.app.routes.ingestion import Rejected, caller_tenant
from relay.control.admission import AdmissionRejectedError
from relay.control.cache import CachedAnswer, CachedSource, normalise_question, question_hash
from relay.errors import sanitize_error
from relay.ingestion.registry import CollectionNotFoundError
from relay.orchestrator.pipeline import (
    MAX_QUESTION_CHARS,
    PipelineOutcome,
    PipelineRequest,
    StreamPlan,
)
from relay.orchestrator.streaming import (
    SentenceStream,
    StoppedEvent,
    StreamRefusal,
    VerifiedSentence,
)
from relay.orchestrator.verifier import TAG_PATTERN, split_sentences
from relay.providers.probing import CONSERVATIVE_CHARS_PER_TOKEN, status_for_error_code
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.registry import CollectionNotReadyError
from relay.store import Store
from relay.telemetry.spans import ServerSpanTracker

logger = logging.getLogger(__name__)

DEFAULT_KEEPALIVE_S: Final = 4.0  # under the 5 s silence limit, with margin
EXCERPT_CHARS: Final = 240
_NS_PER_MS: Final = 1_000_000
_NUMBER = re.compile(r"\d+")
_SUMMARY: Final = "_summary"  # internal: the flight's result, turned into each final
Event = tuple[str, dict[str, Any]]


class AskV1Body(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
    collection_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    conversation_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


# --- Replay channel ----------------------------------------------------------------------


class ReplayChannel:
    """The events of one flight. Subscribers read from the start (the replay), then wait
    for live events. The flight's task is cancelled when the last subscriber leaves."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.closed = False
        self.stage = "queued"
        self.subscribers = 0
        self.task: asyncio.Task[None] | None = None
        self._position: Callable[[], int] = lambda: 0
        self._signal = asyncio.Event()

    def _wake(self) -> None:
        signal, self._signal = self._signal, asyncio.Event()
        signal.set()

    def publish(self, name: str, data: dict[str, Any]) -> None:
        if name == "status":
            self.stage = data["stage"]
        self.events.append((name, data))
        self._wake()

    def status(self, stage: str) -> None:
        self.publish("status", {"stage": stage, "queue_position": self.position()})

    def queued(self, position: Callable[[], int]) -> None:
        self._position = position
        self.status("queued")

    def position(self) -> int:
        return self._position() if self.stage == "queued" else 0

    def close(self) -> None:
        self.closed = True
        self._wake()

    def join(self) -> None:
        self.subscribers += 1

    def leave(self) -> None:
        self.subscribers -= 1
        if self.subscribers == 0 and not self.closed and self.task is not None:
            self.close()  # at once: a request arriving now must start a new flight
            self.task.cancel()  # nobody is listening: stop the work (v1 semantics)

    async def next(self, index: int, timeout_s: float) -> Event | None:
        """The event at ``index``; None on timeout; ``("", {})`` once closed and drained."""
        while index >= len(self.events):
            if self.closed:
                return ("", {})
            signal = self._signal
            try:
                await asyncio.wait_for(signal.wait(), timeout_s)
            except TimeoutError:
                return None
        return self.events[index]


@dataclass
class Flight:
    key: Hashable
    channel: ReplayChannel = field(default_factory=ReplayChannel)


@dataclass
class AskV1Context:
    service: RelayService
    store: Store
    keepalive_s: float = DEFAULT_KEEPALIVE_S
    default_collection_id: str = V1_COLLECTION_ID
    flights: dict[Hashable, Flight] = field(default_factory=dict)

    async def aclose(self) -> None:
        """Shutdown: cancel every live flight (its generation and queue slot) and wait."""
        tasks = [f.channel.task for f in self.flights.values()
                 if f.channel.task is not None and not f.channel.task.done()]
        for flight in self.flights.values():
            flight.channel.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.flights.clear()


# --- Payloads ----------------------------------------------------------------------------


def sse(name: str, data: dict[str, Any]) -> bytes:
    return (f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, sort_keys=True)}\n\n"
            ).encode()


def _page_range(start: int, end: int) -> str:
    return f"{start}" if start == end else f"{start}-{end}"


def sources_payload(passages: tuple[RetrievedPassage, ...]) -> dict[str, Any]:
    return {"passages": [{
        "id": f"P{i}", "chunk_id": p.chunk_id, "paper_id": p.paper_id,
        "short_name": p.short_name, "page_range": _page_range(p.page_start, p.page_end),
        "section": (f"{p.section_number} {p.section_title}" if p.section_number
                    else p.section_title),
        "text_excerpt": p.text[:EXCERPT_CHARS],
        "text": p.text,  # the source panel shows and highlights the whole passage
    } for i, p in enumerate(passages, start=1)]}


def tags_in(text: str) -> list[str]:
    """Every passage tag in ``text``, in order, once each: "[P1, P2]" gives P1 and P2 (the
    verifier's own tag pattern, so a multi-tag bracket is not cut to its first tag)."""
    numbers = (n for tag in TAG_PATTERN.finditer(text) for n in _NUMBER.findall(tag.group()))
    return [f"P{n}" for n in dict.fromkeys(numbers)]


def _message(outcome: PipelineOutcome) -> str | None:
    if outcome.refused:
        return outcome.delivered_text
    if outcome.status != "ok" and outcome.error_code in UNAVAILABLE_MESSAGES:
        return UNAVAILABLE_MESSAGES[outcome.error_code]
    return None


def _stop_reason(outcome: PipelineOutcome, stop: StoppedEvent | None,
                 released: int) -> tuple[str, bool] | None:
    """(reason_class, partial) when the answer stopped; None for a normal end."""
    if outcome.error_code not in (None, "citation_verification_failed") and released:
        return ("provider_error" if outcome.error_code != "client_gave_up"
                else "client_gave_up"), True
    if stop is not None:
        return stop.reason_class, stop.partial
    if outcome.verification_verdict == "BLOCKED" and outcome.kind in ("partial",
                                                                      "safe_refusal"):
        return "final_verification_failed", released > 0
    return None


# --- The flight (leader work) ------------------------------------------------------------


async def _streamed(ctx: AskV1Context, channel: ReplayChannel, plan: StreamPlan,
                    spans: ServerSpanTracker,
                    live: dict[str, Any]) -> tuple[PipelineOutcome, int]:
    pipeline = ctx.service.pipeline
    channel.status("generating")
    tokens = pipeline.run_streaming(plan, spans)
    live["tokens"], live["chat"] = tokens, plan.chat  # for the cancel record's spend
    stream = SentenceStream(tokens, plan.passages, plan.streaming_verifier)
    stop: StoppedEvent | None = None
    refused = False
    async for event in stream:
        if isinstance(event, VerifiedSentence):
            channel.publish("sentence", {"text": event.text,
                                         "citations": [f"P{c.tag}" for c in event.citations]})
        elif isinstance(event, StoppedEvent):
            stop = event
        elif isinstance(event, StreamRefusal):
            refused = True
    if stop is None:
        channel.status("verifying")  # the whole-answer pass
    outcome = await pipeline.complete_streaming(plan, tokens, stream, stop=stop,
                                                refused=refused)
    reason = _stop_reason(outcome, stop, len(stream.sentences))
    if reason is not None:
        channel.publish("stopped", {"reason_class": reason[0], "partial": reason[1]})
    return outcome, len(stream.sentences)


async def _buffered(ctx: AskV1Context, channel: ReplayChannel, plan: StreamPlan,
                    spans: ServerSpanTracker) -> tuple[PipelineOutcome, int]:
    channel.status("generating")
    outcome = await ctx.service.pipeline.complete_buffered(plan, spans)
    channel.status("verifying")
    released = 0
    if outcome.kind == "answer" and outcome.delivered_text is not None:
        for sentence in split_sentences(outcome.delivered_text):
            channel.publish("sentence", {"text": sentence, "citations": tags_in(sentence)})
            released += 1
    return outcome, released


def _summary(outcome: PipelineOutcome, *, released: int, buffered: bool,
             spans: ServerSpanTracker, depth: int | None) -> dict[str, Any]:
    partial = outcome.kind == "partial" or (released > 0 and outcome.kind != "answer")
    return {
        "status": outcome.status, "verdict": outcome.verification_verdict,
        "error_code": outcome.error_code, "partial": partial, "refused": outcome.refused,
        "message": _message(outcome), "tokens_in": outcome.tokens_in,
        "tokens_out": outcome.tokens_out, "usage_source": outcome.usage_source,
        "provider_called": outcome.provider_called, "model_id": outcome.model_id,
        "retrieval_setting": outcome.retrieval_setting, "buffered": buffered,
        "queue_depth_at_entry": depth,
        "provider_first_token_ms": spans.snapshot().provider_first_token_ms,
    }


def _shed_summary(error_code: str, *, setting: str, buffered: bool,
                  depth: int | None) -> dict[str, Any]:
    return {"status": status_for_error_code(error_code), "verdict": "NOT_RUN",  # type: ignore[arg-type]
            "error_code": error_code, "partial": False, "refused": False,
            "message": UNAVAILABLE_MESSAGES.get(error_code), "tokens_in": 0, "tokens_out": 0,
            "usage_source": "none", "provider_called": False, "model_id": None,
            "retrieval_setting": setting, "buffered": buffered, "queue_depth_at_entry": depth,
            "provider_first_token_ms": None}


async def _lead(ctx: AskV1Context, flight: Flight, target: AskTarget, *, request_id: str,
                collection_id: str, buffered: bool, go: asyncio.Event) -> None:
    channel, service = flight.channel, ctx.service
    ask = target.ask
    setting = ask.retrieval_setting or ""
    spans = ServerSpanTracker(service.clock)
    depth: int | None = None
    live: dict[str, Any] = {}
    try:
        await go.wait()  # no work before "accepted" has been sent
        try:
            async with service.queue.admit(is_connected=lambda: channel.subscribers > 0,
                                           on_queued=channel.queued) as ticket:
                depth = ticket.queue_depth_at_entry
                channel.status("retrieving")
                plan = await service.pipeline.prepare_streaming(PipelineRequest(
                    request_id=request_id, question=ask.question, retrieval_setting=setting,
                    collection_id=collection_id), spans)
                channel.publish("sources", sources_payload(plan.passages))
                if plan.early is not None:
                    outcome, released = plan.early, 0
                elif buffered:
                    outcome, released = await _buffered(ctx, channel, plan, spans)
                else:
                    outcome, released = await _streamed(ctx, channel, plan, spans, live)
        except AdmissionRejectedError as shed:
            channel.publish(_SUMMARY, _shed_summary(shed.error_code, setting=setting,
                                                    buffered=buffered,
                                                    depth=shed.queue_depth_at_entry))
            return
        # Fully verified answers and verified refusals only (never a partial answer).
        if (service.cache is not None and outcome.delivered_text is not None
                and (outcome.kind, outcome.verification_verdict) in (
                    ("answer", "VERIFIED"), ("llm_refusal", "REFUSAL_VERIFIED"))):
            try:
                service.cache.put(ask.question, target.cache_setting, CachedAnswer(
                    delivered_text=outcome.delivered_text, refused=outcome.refused,
                    citations=outcome.citations,
                    verification_verdict=outcome.verification_verdict,
                    model_id=outcome.model_id, source_request_id=request_id,
                    sources=tuple(CachedSource(**p) for p in
                                  sources_payload(plan.passages)["passages"])),
                    degraded=outcome.degraded)
            except Exception as exc:  # a lost cache entry must not change the answer
                logger.warning("/v1/ask request %s: answer not cached: %s", request_id,
                               sanitize_error(exc))
        service.record_trace({"record": "flight", "endpoint": "/v1/ask",
                              "leader_request_id": request_id, "collection_id": collection_id,
                              "question_sha256": question_hash(ask.question),
                              "kind": outcome.kind, "buffered": buffered,
                              "provider_called": outcome.provider_called,
                              "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
                              "usage_source": outcome.usage_source,
                              "pipeline": outcome.trace.model_dump(mode="json")})
        channel.publish(_SUMMARY, _summary(outcome, released=released, buffered=buffered,
                                           spans=spans, depth=depth))
    except asyncio.CancelledError:
        service.record_trace({"record": "flight", "endpoint": "/v1/ask",
                              "leader_request_id": request_id, "end": "cancelled",
                              **_cancelled_spend(live)})
        raise
    except Exception as exc:  # the stream must still end with a final event
        logger.error("/v1/ask request %s failed unexpectedly: %s", request_id,
                     sanitize_error(exc))
        summary = _shed_summary("internal_error", setting=setting, buffered=buffered,
                                depth=depth)
        if any(name == "sentence" for name, _ in channel.events):
            channel.publish("stopped", {"reason_class": "internal_error", "partial": True})
            summary["partial"] = True
        channel.publish(_SUMMARY, summary)
    finally:
        channel.close()
        if ctx.flights.get(flight.key) is flight:
            del ctx.flights[flight.key]


# --- One request (one SSE response) ------------------------------------------------------


def _forget(ctx: AskV1Context, flight: Flight) -> None:
    """Drop a finished flight from the registry (also one cancelled before it started)."""
    flight.channel.close()
    if ctx.flights.get(flight.key) is flight:
        del ctx.flights[flight.key]


def _cancelled_spend(live: dict[str, Any]) -> dict[str, Any]:
    """What a cancelled flight had spent, estimated (v1 does the same for /ask)."""
    tokens, chat = live.get("tokens"), live.get("chat")
    if tokens is None or chat is None:
        return {"provider_called": False, "tokens_in": 0, "tokens_out": 0,
                "usage_source": "none"}
    return {"provider_called": True,
            "tokens_in": math.ceil(chat.prompt_chars / CONSERVATIVE_CHARS_PER_TOKEN),
            "tokens_out": math.ceil(tokens.received_chars / CONSERVATIVE_CHARS_PER_TOKEN),
            "usage_source": "estimated"}


def _accepted(ctx: AskV1Context, channel: ReplayChannel, request_id: str, *,
              follower: bool) -> dict[str, Any]:
    queue = ctx.service.queue
    if follower:  # it waits for the flight it joined
        position = channel.position()
    else:
        busy = queue.active >= queue.max_active or queue.depth > 0
        position = queue.depth + 1 if busy else 0
    return {"request_id": request_id, "queue_position": position,
            "expected_wait_ms": round(position / queue.service_rate_per_s * 1000, 1)}


async def _cached(ctx: AskV1Context, hit: CachedAnswer, *, request_id: str,
                  arrived_ns: int, collection_id: str, buffered: bool,
                  setting: str) -> AsyncIterator[bytes]:
    clock = ctx.service.clock
    yield sse("accepted", {"request_id": request_id, "queue_position": 0,
                           "expected_wait_ms": 0.0})
    if hit.sources:  # cached by /v1/ask: the same passages the live answer sent
        yield sse("sources", {"passages": [s.model_dump() for s in hit.sources]})
    else:  # cached by v1's POST /ask, which keeps only the cited pages
        yield sse("sources", {"passages": [{
            "id": f"P{c.tag}", "chunk_id": c.chunk_id, "paper_id": c.paper_id,
            "short_name": None, "page_range": _page_range(c.page_start, c.page_end),
            "section": None, "text_excerpt": None, "text": None} for c in hit.citations]})
    first: float | None = None
    if not hit.refused:  # a refusal's text is a message, never a sentence
        for sentence in split_sentences(hit.delivered_text):
            first = first if first is not None else (clock() - arrived_ns) / _NS_PER_MS
            yield sse("sentence", {"text": sentence, "citations": tags_in(sentence)})
    total = (clock() - arrived_ns) / _NS_PER_MS
    final = {"request_id": request_id, "status": "ok", "verdict": hit.verification_verdict,
             "error_code": None, "partial": False, "refused": hit.refused,
             "message": hit.delivered_text if hit.refused else None,
             "tokens_in": 0, "tokens_out": 0, "usage_source": "none",
             "provider_called": False, "model_id": hit.model_id,
             "retrieval_setting": setting, "collection_id": collection_id,
             "buffered": buffered, "queue_depth_at_entry": None,
             "cache_status": "exact_hit", "coalesced": False,
             "ttft_ms": first, "total_ms": round(total, 3)}
    ctx.service.record_trace(_request_record(request_id, hit_question=None, final=final,
                                             collection_id=collection_id, role="cache_hit"))
    yield sse("final", final)


def _request_record(request_id: str, *, hit_question: str | None, final: dict[str, Any],
                    collection_id: str, role: str) -> dict[str, Any]:
    return {"record": "request", "endpoint": "/v1/ask", "request_id": request_id,
            "role": role, "collection_id": collection_id,
            "question_sha256": None if hit_question is None else question_hash(hit_question),
            "final": {"status": final["status"], "cache_status": final["cache_status"],
                      "verification_verdict": final["verdict"],
                      "provider_first_token_ms": final.get("provider_first_token_ms"),
                      "first_sentence_ms": final["ttft_ms"],
                      "server_total_ms": final["total_ms"], "tokens_in": final["tokens_in"],
                      "tokens_out": final["tokens_out"]}}


async def _respond(ctx: AskV1Context, target: AskTarget, *, request_id: str, arrived_ns: int,
                   collection_id: str, buffered: bool) -> AsyncIterator[bytes]:
    service, ask = ctx.service, target.ask
    clock = service.clock
    setting = ask.retrieval_setting or ""
    if service.cache is not None:
        hit = service.cache.get(ask.question, target.cache_setting)
        # A verified answer is replayed as sentences; a verified refusal as a message only.
        if hit is not None and (
                (hit.verification_verdict == "VERIFIED" and not hit.refused)
                or (hit.verification_verdict == "REFUSAL_VERIFIED" and hit.refused)):
            async for chunk in _cached(ctx, hit, request_id=request_id, arrived_ns=arrived_ns,
                                       collection_id=collection_id, buffered=buffered,
                                       setting=setting):
                yield chunk
            return
    key: Hashable = (normalise_question(ask.question), target.cache_setting, buffered)
    if service.cache is None:
        key = (key, request_id)  # cache off: nothing is coalesced (v1 behaviour)
    flight = ctx.flights.get(key)
    follower = flight is not None and not flight.channel.closed
    if flight is None or not follower:
        flight = ctx.flights[key] = Flight(key)
    channel = flight.channel
    channel.join()
    go = asyncio.Event()
    if not follower:  # created now (so a disconnect can cancel it), started after accepted
        channel.task = asyncio.create_task(_lead(
            ctx, flight, target, request_id=request_id, collection_id=collection_id,
            buffered=buffered, go=go), name=f"v1-ask-{request_id}")
        channel.task.add_done_callback(lambda _: _forget(ctx, flight))
    try:
        yield sse("accepted", _accepted(ctx, channel, request_id, follower=follower))
        go.set()
        index = 0
        first: float | None = None
        while True:
            event = await channel.next(index, ctx.keepalive_s)
            if event is None:  # silence: repeat the current stage
                yield sse("status", {"stage": channel.stage,
                                     "queue_position": channel.position(), "keepalive": True})
                continue
            name, data = event
            index += 1
            if name == "":  # closed without a summary: the service is shutting down
                logger.warning("/v1/ask request %s ended without a result (shutdown)",
                               request_id)
                data = _shed_summary("internal_error", setting=setting, buffered=buffered,
                                     depth=None)
                if first is not None:  # sentences went out: say the answer is partial
                    yield sse("stopped", {"reason_class": "service_shutdown",
                                          "partial": True})
                    data["partial"] = True
                name = _SUMMARY
            if name == _SUMMARY:
                total = (clock() - arrived_ns) / _NS_PER_MS
                final = {**data, "request_id": request_id, "collection_id": collection_id,
                         "cache_status": ("coalesced" if follower else
                                          "off" if service.cache is None else "miss"),
                         "coalesced": follower, "ttft_ms": first,
                         "total_ms": round(total, 3)}
                if follower:  # the leader paid for it
                    final.update({"tokens_in": 0, "tokens_out": 0, "usage_source": "none",
                                  "provider_called": False, "queue_depth_at_entry": None})
                service.record_trace(_request_record(
                    request_id, hit_question=ask.question, final=final,
                    collection_id=collection_id, role="follower" if follower else "leader"))
                yield sse("final", final)
                return
            if name == "sentence" and first is None:
                first = round((clock() - arrived_ns) / _NS_PER_MS, 3)
            yield sse(name, data)
    finally:
        go.set()  # "accepted" went out (or the client left): followers must not wait
        channel.leave()


# --- Router ------------------------------------------------------------------------------


def _error(status: int, code: str, message: str) -> JSONResponse:
    headers = {"www-authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": code, "message": message}, status_code=status,
                        headers=headers)


def ask_v1_router(ctx: AskV1Context) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/ask")
    async def ask_v1(request: Request) -> Response:
        service = ctx.service
        arrived_ns = service.clock()
        try:
            tenant = caller_tenant(request)
            body = AskV1Body.model_validate_json(await request.body())
            if not body.question.strip():
                raise Rejected(422, "invalid_request", "the question is blank")
            if service.collections is None:
                raise Rejected(503, "collections_disabled",
                               "/v1/ask needs the metadata store (cache.mode = sqlite)")
            collection_id = body.collection_id or ctx.default_collection_id
            collection = await ctx.store.collections.get_by_id(collection_id)
            if collection is None or collection.tenant_id != tenant:
                raise Rejected(404, "collection_not_found", "no such collection")
            if body.conversation_id is not None:
                conversation = await ctx.store.conversations.get_by_id(body.conversation_id)
                if conversation is None or conversation.collection_id != collection_id:
                    raise Rejected(404, "conversation_not_found", "no such conversation")
            request_id = f"v1ask-{uuid.uuid4().hex}"
            target = await service.target(AskRequest(
                request_id=request_id, question=body.question, collection_id=collection_id))
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        except ValidationError as exc:
            return JSONResponse({"error": "invalid_request", "details": [
                {"loc": [str(p) for p in err["loc"]], "type": err["type"]}
                for err in exc.errors()]}, status_code=422)
        except CollectionNotFoundError:
            return _error(404, "collection_not_found", "no such collection")
        except CollectionNotReadyError:
            return _error(409, "collection_not_ready",
                          "the collection has no indexed documents yet")
        return StreamingResponse(
            _respond(ctx, target, request_id=request_id, arrived_ns=arrived_ns,
                     collection_id=collection_id, buffered=collection.buffered),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"})

    return router


__all__ = ["DEFAULT_KEEPALIVE_S", "EXCERPT_CHARS",
           "AskV1Body", "AskV1Context", "Flight", "ReplayChannel",
           "ask_v1_router", "sources_payload", "sse"]
