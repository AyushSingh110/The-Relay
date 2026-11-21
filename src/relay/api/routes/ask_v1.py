"""POST /v1/ask: verified streaming over SSE (plan V2-05; docs/ARCHITECTURE_V2.md §6).

Events, in order, on one SSE connection:

    accepted  request_id, queue_position, expected_wait_ms, conversation_id   (first, before
              any work)
    status    rewriting (V2-08, follow-ups only): sent at once; then again with
              rewritten_question, original_question, rewritten, fallback
    status    stage: queued | retrieving | generating | verifying   (+ keepalives)
    sources   passages: [{id, chunk_id, paper_id, short_name, page_range, section,
                          text_excerpt, text}]   (text: the whole passage, V2-06)
    sentence  text, citations: ["P1", ...], confidence   (only sentences that passed the
              verifier; confidence: the advisory L4 entailment score, 0-1, or null when
              the collection has L4 off or no model is installed; V2-07)
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
- Several workers (V2-13, ``[state] backend = "redis"``): the first worker to claim a
  question's flight lock leads it and mirrors every event to the shared store; a request
  on another worker follows that flight over Pub/Sub instead of calling the model. The
  lock has a TTL that the leader keeps renewing; when a leader dies, its lock expires and
  its remote followers end with an explicit error (``shed_reason: leader_lost``).
- A collection with ``buffered = true`` uses the buffered path (one model call, the
  whole-answer verifier, then every sentence at once), with the same event sequence.

API keys are stubbed as for the other /v1 routes: any Bearer key is the dev tenant.
``POST /ask`` (v1, used by the load harness) is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Hashable
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.api.app import UNAVAILABLE_MESSAGES, AskRequest, AskTarget, RelayService
from relay.api.auth import Authenticator
from relay.app.bootstrap import V1_COLLECTION_ID
from relay.app.routes.rejected import Rejected
from relay.control.admission import AdmissionRejectedError, Priority
from relay.control.cache import CachedAnswer, CachedSource, normalise_question, question_hash
from relay.control.pricing import total_cost
from relay.control.state import DEFAULT_FLIGHT_TTL_S, StateStore
from relay.errors import sanitize_error
from relay.ingestion.registry import CollectionNotFoundError
from relay.orchestrator.pipeline import (
    MAX_QUESTION_CHARS,
    PipelineOutcome,
    PipelineRequest,
    StreamPlan,
)
from relay.orchestrator.rewriter import HISTORY_TURNS, Rewrite
from relay.orchestrator.streaming import (
    SentenceStream,
    StoppedEvent,
    StreamRefusal,
    VerifiedSentence,
    score_sentence,
)
from relay.orchestrator.verifier import TAG_PATTERN, EntailmentScore, split_sentences
from relay.providers.probing import CONSERVATIVE_CHARS_PER_TOKEN, status_for_error_code
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.registry import CollectionNotReadyError
from relay.store import Store
from relay.store.repositories import Turn
from relay.telemetry.metrics import layer_of
from relay.telemetry.spans import ServerSpanTracker
from relay.telemetry.tracing import RequestTrace, Span

logger = logging.getLogger(__name__)

DEFAULT_KEEPALIVE_S: Final = 4.0  # under the 5 s silence limit, with margin
DEFAULT_LEASE_POLL_S: Final = 1.0  # V2-13: how often a silent remote flight's lease is checked
EXCERPT_CHARS: Final = 240
_NS_PER_MS: Final = 1_000_000
_NS_PER_S: Final = 1_000_000_000
_NUMBER = re.compile(r"\d+")
_SUMMARY: Final = "_summary"  # internal: the flight's result, turned into each final
Event = tuple[str, dict[str, Any]]


class AskV1Body(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
    collection_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    conversation_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    # V2-14: as in v1's POST /ask. The queue sheds a request predicted to wait past it;
    # it bounds the provider guard's quota waits and retries, and a coalesced follower's
    # wait. It does not cut off an answer already being generated. None: no deadline.
    deadline_ms: Annotated[int, Field(gt=0)] | None = None
    # V2-10: interactive (the UI, the default) is admitted before batch, and batch is shed
    # first when the queue is full
    priority: Literal["interactive", "batch"] = "interactive"


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
        # V2-13: a leader shared across workers mirrors its events, and is not cancelled
        # while followers on other workers still listen
        self.mirror: Callable[[str, dict[str, Any]], None] | None = None
        self.on_abandoned: Callable[[], None] | None = None
        self.remote_followers = False  # set when the leader is kept for other workers

    def _wake(self) -> None:
        signal, self._signal = self._signal, asyncio.Event()
        signal.set()

    def publish(self, name: str, data: dict[str, Any]) -> None:
        if name == "status":
            self.stage = data["stage"]
        self.events.append((name, data))
        if self.mirror is not None:
            self.mirror(name, data)
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
            if self.on_abandoned is not None:  # V2-13: ask the other workers first
                self.on_abandoned()
                return
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
    auth: Authenticator | None = None  # V2-10; None: one over ``store`` (dev key on)
    # V2-10: collections deleted while this process runs. An answer still in flight for
    # one is delivered, but neither cached nor logged (its logs were deleted with it).
    deleted: set[str] = field(default_factory=set)
    # V2-13: the store shared by several workers (None: this process coalesces alone)
    state: StateStore | None = None
    flight_ttl_s: float = DEFAULT_FLIGHT_TTL_S
    lease_poll_s: float = DEFAULT_LEASE_POLL_S
    background: set[asyncio.Task[None]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.auth is None and self.store is not None:
            self.auth = Authenticator(self.store)

    def forget(self, collection_id: str) -> None:
        self.deleted.add(collection_id)

    def record(self, record: dict[str, Any], *, final: dict[str, Any] | None = None,
               tenant: str | None = None) -> None:
        """A request trace (and, V2-11, its metrics), unless its collection is deleted."""
        if record.get("collection_id") in self.deleted:
            return
        self.service.record_trace(record)
        if final is not None:
            self.service.metrics.observe_request(final, tenant=tenant)

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
        if self.background:  # V2-13: last events, lock releases and lease checks
            _, pending = await asyncio.wait(list(self.background), timeout=2.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def spawn(self, work: Any, name: str) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(work, name=name)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task


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


def _checked(trace: RequestTrace | None, parent: Span | None, index: int | None, *,
             layer: str | None = None, l4: float | None = None) -> None:
    """One ``verifier.sentence`` span (V2-11): which layers it passed, its L4 score."""
    if trace is None:
        return
    order = ("L1", "L2", "L3")
    failed = order.index(layer) if layer in order else None
    passes: dict[str, bool | None] = {
        f"{name.lower()}_pass": None if failed is not None and i > failed
        else failed is None or i < failed
        for i, name in enumerate(order)}
    trace.instant("verifier.sentence", parent=parent, sentence_idx=index, l4_score=l4,
                  blocked_layer=layer, **passes)


async def _relay_stream(channel: ReplayChannel, stream: SentenceStream,
                        live: dict[str, Any], trace: RequestTrace | None = None,
                        parent: Span | None = None) -> tuple[StoppedEvent | None, bool]:
    """Publish a sentence stream's verified sentences; (its stop, whether it refused)."""
    stop: StoppedEvent | None = None
    refused = False
    async for event in stream:
        if isinstance(event, VerifiedSentence):
            live.setdefault("l4", []).append(event.confidence)
            live["checked"] = live.get("checked", 0) + 1
            live["passed"] = live.get("passed", 0) + 1
            _checked(trace, parent, event.index, l4=event.confidence)
            channel.publish("sentence", {"text": event.text,
                                         "citations": [f"P{c.tag}" for c in event.citations],
                                         "confidence": event.confidence})
        elif isinstance(event, StoppedEvent):
            stop = event
            layer = layer_of(event.reason_class, [f.code for f in event.failures])
            if event.sentence_index is not None and layer is not None:
                live["checked"] = live.get("checked", 0) + 1
                _checked(trace, parent, event.sentence_index, layer=layer)
        elif isinstance(event, StreamRefusal):
            refused = True
    live["stop"] = stop
    return stop, refused


async def _streamed(ctx: AskV1Context, channel: ReplayChannel, plan: StreamPlan,
                    spans: ServerSpanTracker, live: dict[str, Any],
                    deadline_ns: int | None,
                    scorer: EntailmentScore | None,
                    trace: RequestTrace | None = None) -> tuple[PipelineOutcome, int]:
    pipeline = ctx.service.pipeline
    channel.status("generating")
    generate = None if trace is None else trace.start("pipeline.generate", mode="streaming")
    tokens = pipeline.run_streaming(plan, spans, deadline_ns=deadline_ns)
    live["tokens"], live["chat"] = tokens, plan.chat  # for the cancel record's spend
    stream = SentenceStream(tokens, plan.passages, plan.streaming_verifier, scorer=scorer)
    stop, refused = await _relay_stream(channel, stream, live, trace, generate)
    # V2-09: a truncated or empty answer is retried once on the escalation lane, but only
    # when nothing was delivered yet (the client never sees two different answers).
    if not stream.sentences and not refused and (stop is None
                                                 or stop.reason_class == "truncated"):
        retry = pipeline.escalate_streaming(plan, tokens, spans, deadline_ns=deadline_ns)
        if retry is not None:
            channel.publish("status", {"stage": "generating", "queue_position": 0,
                                       "escalated": True, "reason": retry.reason})
            tokens = live["tokens"] = retry
            stream = SentenceStream(tokens, plan.passages, plan.streaming_verifier,
                                    scorer=scorer)
            stop, refused = await _relay_stream(channel, stream, live, trace, generate)
    if trace is not None:
        trace.end(generate, **_generation_attributes(tokens.result))
    if stop is None:
        channel.status("verifying")  # the whole-answer pass
    verify = None if trace is None else trace.start("pipeline.verify", mode="streaming")
    outcome = await pipeline.complete_streaming(plan, tokens, stream, stop=stop,
                                                refused=refused)
    if trace is not None:
        trace.end(verify, verdict=outcome.verification_verdict)
    reason = _stop_reason(outcome, stop, len(stream.sentences))
    if reason is not None:
        channel.publish("stopped", {"reason_class": reason[0], "partial": reason[1]})
    return outcome, len(stream.sentences)


async def _buffered(ctx: AskV1Context, channel: ReplayChannel, plan: StreamPlan,
                    spans: ServerSpanTracker, live: dict[str, Any],
                    deadline_ns: int | None,
                    scorer: EntailmentScore | None,
                    trace: RequestTrace | None = None) -> tuple[PipelineOutcome, int]:
    channel.status("generating")
    # buffered: the model call and the whole-answer check are one pipeline step
    generate = None if trace is None else trace.start("pipeline.generate", mode="buffered")
    outcome = await ctx.service.pipeline.complete_buffered(plan, spans,
                                                           deadline_ns=deadline_ns)
    if trace is not None:
        trace.end(generate, **_outcome_attributes(outcome))
    live["outcome"] = outcome  # its usage is known: a cancel during L4 scoring records it
    channel.status("verifying")
    verify = None if trace is None else trace.start("pipeline.verify", mode="buffered")
    released = 0
    verification = outcome.trace.verification
    live["checked"] = 0 if verification is None else verification.sentences
    if outcome.kind == "answer" and outcome.delivered_text is not None:
        for index, sentence in enumerate(split_sentences(outcome.delivered_text), start=1):
            confidence = (None if scorer is None
                          else await score_sentence(scorer, sentence, plan.passages))
            live.setdefault("l4", []).append(confidence)
            _checked(trace, verify, index, l4=confidence)
            channel.publish("sentence", {"text": sentence, "citations": tags_in(sentence),
                                         "confidence": confidence})
            released += 1
    elif verification is not None and verification.verdict == "BLOCKED" and verification.failures:
        failure = verification.failures[0]
        _checked(trace, verify, failure.sentence, layer=layer_of(None, [failure.code]))
    live["passed"] = released
    if trace is not None:
        trace.end(verify, verdict=outcome.verification_verdict)
    return outcome, released


def _generation_attributes(result: Any) -> dict[str, Any]:
    """The ``pipeline.generate`` span's lane, model and tokens (from the routed result)."""
    route = None if result is None else result.route
    tokens = None if result is None else result.tokens
    return {key: value for key, value in {
        "lane": None if route is None else route.lane,
        "model": None if route is None else route.model,
        "escalated": None if route is None else route.escalated,
        "fallback": None if route is None else route.fallback,
        "tokens_in": None if tokens is None else tokens.tokens_in,
        "tokens_out": None if tokens is None else tokens.tokens_out,
        "cancelled": result is None}.items() if value is not None}


def _outcome_attributes(outcome: PipelineOutcome) -> dict[str, Any]:
    route = outcome.route
    return {key: value for key, value in {
        "lane": None if route is None else route.lane,
        "model": outcome.model_id, "tokens_in": outcome.tokens_in,
        "tokens_out": outcome.tokens_out,
        "escalated": None if route is None else route.escalated}.items() if value is not None}


def _block_layer(outcome: PipelineOutcome, live: dict[str, Any]) -> str | None:
    """The verifier layer that blocked this answer (V2-11 metrics), or None."""
    stop = live.get("stop")
    if isinstance(stop, StoppedEvent):
        return layer_of(stop.reason_class, [f.code for f in stop.failures])
    verification = outcome.trace.verification
    if verification is not None and verification.verdict == "BLOCKED":
        return layer_of(None, [f.code for f in verification.failures])
    return None


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
        **_route_fields(outcome),
    }


def _verifier_counts(live: dict[str, Any]) -> dict[str, int]:
    return {"sentences_checked": int(live.get("checked", 0)),
            "sentences_passed": int(live.get("passed", 0))}


def _route_fields(outcome: PipelineOutcome | None) -> dict[str, Any]:
    """V2-09: which lane answered, what every call cost, and whether it was escalated."""
    route = None if outcome is None else outcome.route
    if route is None:  # no model call (early refusal), or an unrouted generator
        called = outcome is not None and outcome.provider_called
        return {"lane_name": None, "provider": None, "model": None, "escalated": False,
                "fallback": False, "cost_usd": None if called else 0.0, "ledger": [],
                "retry_after_ms": None}
    return {"lane_name": route.lane, "provider": route.provider, "model": route.model,
            "escalated": route.escalated, "fallback": route.fallback,
            "cost_usd": route.cost_usd,
            "ledger": [use.model_dump(mode="json") for use in route.uses],
            "retry_after_ms": route.retry_after_ms}


def _charge(final: dict[str, Any], rewrite: Rewrite | None) -> dict[str, Any]:
    """Add the follow-up rewrite's call to this request's tokens, ledger and cost (the
    request's full lifecycle; a follower or cache hit pays only for its own rewrite)."""
    ledger = list(final.get("ledger") or [])
    if rewrite is not None and rewrite.usage is not None:
        use = rewrite.usage
        ledger.insert(0, use.model_dump(mode="json"))
        final["tokens_in"] = final.get("tokens_in", 0) + use.tokens_in
        final["tokens_out"] = final.get("tokens_out", 0) + use.tokens_out
    final["ledger"] = ledger
    known = final.get("cost_usd", 0.0) is not None or not final.get("provider_called")
    final["cost_usd"] = total_cost([u["cost_usd"] for u in ledger]) if known else None
    return final


def _shed_summary(error_code: str, *, setting: str, buffered: bool,
                  depth: int | None) -> dict[str, Any]:
    return {"status": status_for_error_code(error_code), "verdict": "NOT_RUN",  # type: ignore[arg-type]
            "error_code": error_code, "partial": False, "refused": False,
            "message": UNAVAILABLE_MESSAGES.get(error_code), "tokens_in": 0, "tokens_out": 0,
            "usage_source": "none", "provider_called": False, "model_id": None,
            "retrieval_setting": setting, "buffered": buffered, "queue_depth_at_entry": depth,
            "provider_first_token_ms": None, **_route_fields(None)}


async def _lead(ctx: AskV1Context, flight: Flight, target: AskTarget, *, request_id: str,
                collection_id: str, buffered: bool, go: asyncio.Event,
                deadline_ns: int | None = None,
                scorer: EntailmentScore | None = None, tenant: str | None = None,
                priority: str = Priority.INTERACTIVE,
                trace: RequestTrace | None = None) -> None:
    channel, service = flight.channel, ctx.service
    ask = target.ask
    setting = ask.retrieval_setting or ""
    spans = ServerSpanTracker(service.clock)
    depth: int | None = None
    live: dict[str, Any] = {}
    try:
        await go.wait()  # no work before "accepted" has been sent
        queued = None if trace is None else trace.start(
            "queue.wait", priority=str(priority), queue_position=service.queue.depth)
        try:
            async with service.queue.admit(deadline_ns=deadline_ns,
                                           is_connected=lambda: channel.subscribers > 0
                                           or channel.remote_followers,
                                           on_queued=channel.queued, tenant=tenant,
                                           priority=priority) as ticket:
                depth = ticket.queue_depth_at_entry
                if trace is not None:
                    trace.end(queued, over_rate=ticket.over_rate)
                channel.status("retrieving")
                retrieve = None if trace is None else trace.start("pipeline.retrieve",
                                                                  preset=setting)
                plan = await service.pipeline.prepare_streaming(PipelineRequest(
                    request_id=request_id, question=ask.question, retrieval_setting=setting,
                    collection_id=collection_id), spans)
                if trace is not None:
                    trace.end(retrieve, chunks_found=len(plan.passages),
                              early=plan.early is not None)
                channel.publish("sources", sources_payload(plan.passages))
                if plan.early is not None:
                    outcome, released = plan.early, 0
                elif buffered:
                    outcome, released = await _buffered(ctx, channel, plan, spans, live,
                                                        deadline_ns, scorer, trace)
                else:
                    outcome, released = await _streamed(ctx, channel, plan, spans, live,
                                                        deadline_ns, scorer, trace)
        except AdmissionRejectedError as shed:
            if trace is not None:
                trace.end(queued, shed=True, error_code=shed.error_code)
            summary = _shed_summary(shed.error_code, setting=setting, buffered=buffered,
                                    depth=shed.queue_depth_at_entry)
            summary["shed_reason"] = shed.reason  # V2-10: e.g. shed_for_priority
            channel.publish(_SUMMARY, summary)
            return
        service.metrics.observe_block(_block_layer(outcome, live))  # V2-11
        if trace is not None:  # v1's span kinds, on the root
            span_totals = spans.snapshot()
            trace.set(waiting_ms=span_totals.waiting_ms, working_ms=span_totals.working_ms,
                      retrying_ms=span_totals.retrying_ms)
        if outcome.provider_called and outcome.usage_source == "provider":
            # V2-10: the queue's capacity follows the measured tokens per answer (EWMA)
            service.queue.observe_tokens(outcome.tokens_in + outcome.tokens_out)
        # Fully verified answers and verified refusals only (never a partial answer), and
        # never for a collection deleted while it was being answered.
        if (service.cache is not None and outcome.delivered_text is not None
                and collection_id not in ctx.deleted
                and (outcome.kind, outcome.verification_verdict) in (
                    ("answer", "VERIFIED"), ("llm_refusal", "REFUSAL_VERIFIED"))):
            try:
                await service.cache.admit(ask.question, target.cache_setting, CachedAnswer(
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
                              "l4_confidences": live.get("l4", []),  # V2-07, advisory
                              "pipeline": outcome.trace.model_dump(mode="json")})
        channel.publish(_SUMMARY, {**_summary(outcome, released=released, buffered=buffered,
                                              spans=spans, depth=depth),
                                   "verifier": _verifier_counts(live)})
    except asyncio.CancelledError:
        spent = _cancelled_spend(live)
        service.record_trace({"record": "flight", "endpoint": "/v1/ask",
                              "leader_request_id": request_id, "end": "cancelled", **spent})
        # V2-11: an abandoned answer's tokens were spent all the same
        service.metrics.observe_spend(spent.get("lane_name"), spent.get("tokens_in", 0),
                                      spent.get("tokens_out", 0))
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


# --- Flights shared across workers (V2-13) ---------------------------------------------


def flight_key_text(key: Hashable) -> str:
    """The shared store's name for a flight: a hash of (question, setting, buffered)."""
    return hashlib.sha256(json.dumps(key, default=str).encode("utf-8")).hexdigest()


class _Mirror:
    """A leading flight's events, sent to the shared store in order. The flight lock is
    renewed while the flight runs and released after its last event."""

    def __init__(self, ctx: AskV1Context, flight_key: str, token: str) -> None:
        self._ctx, self._key, self._token = ctx, flight_key, token
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        ctx.spawn(self._pump(), f"v1-ask-mirror-{flight_key[:12]}")

    def push(self, name: str, data: dict[str, Any]) -> None:
        self._queue.put_nowait((name, data))

    def finish(self) -> None:
        self._queue.put_nowait(None)

    async def _pump(self) -> None:
        state = self._ctx.state
        if state is None:
            return
        ttl_ms = int(self._ctx.flight_ttl_s * 1000)
        beat_s = self._ctx.flight_ttl_s / 3
        renewed = time.monotonic()
        seq = 0
        try:
            while True:
                idle = False
                item: Event | None = None
                try:
                    item = await asyncio.wait_for(self._queue.get(), beat_s)
                except TimeoutError:
                    idle = True  # nothing to send: only renew the lock
                if time.monotonic() - renewed >= beat_s:
                    if not await state.refresh_flight_lock(self._key, self._token, ttl_ms):
                        logger.warning("flight %s: lock lost while leading", self._key[:12])
                    renewed = time.monotonic()
                if idle:
                    continue
                name, data = item if item is not None else ("", {})
                await state.publish_flight_event(self._key, self._token,
                                                 {"seq": seq, "name": name, "data": data})
                seq += 1
                if item is None:
                    return
        except Exception as exc:  # followers elsewhere notice the lease expire
            logger.warning("flight %s: events not shared any more: %s", self._key[:12],
                           sanitize_error(exc))
        finally:
            try:
                await state.release_flight_lock(self._key, self._token)
            except Exception as exc:  # the lock expires by its TTL
                logger.warning("flight %s: lock not released (%s); it expires in %.0f s",
                               self._key[:12], type(exc).__name__, self._ctx.flight_ttl_s)


def _abandon(ctx: AskV1Context, flight: Flight, flight_key: str, token: str) -> None:
    """The leader's last local listener left: cancel the work only when no follower on
    another worker listens either (otherwise it finishes, and its answer is cached)."""
    state = ctx.state

    async def check() -> None:
        try:
            followers = 0 if state is None else await state.flight_followers(flight_key, token)
        except Exception as exc:
            logger.warning("flight %s: followers unknown (%s)", flight_key[:12],
                           type(exc).__name__)
            followers = 0
        channel = flight.channel
        channel.remote_followers = followers > 0  # V2-13: the queue must keep it, too
        if (channel.subscribers == 0 and not channel.closed and channel.task is not None
                and followers == 0):
            channel.close()
            channel.task.cancel()

    ctx.spawn(check(), f"v1-ask-abandon-{flight_key[:12]}")


def _end_remote(channel: ReplayChannel, error_code: str, reason: str, *, sentences: bool,
                setting: str, buffered: bool) -> None:
    """End a followed flight with an explicit result (never silently)."""
    if sentences:
        channel.publish("stopped", {"reason_class": reason, "partial": True})
    summary = _shed_summary(error_code, setting=setting, buffered=buffered, depth=None)
    summary.update({"partial": sentences, "shed_reason": reason, "_remote": True})
    channel.publish(_SUMMARY, summary)


async def _follow_remote(ctx: AskV1Context, flight: Flight, flight_key: str, token: str, *,
                         setting: str, buffered: bool, deadline_ns: int | None) -> None:
    """Relay another worker's flight into this worker's channel."""
    state, channel, clock = ctx.state, flight.channel, ctx.service.clock
    sentences = suspect = False
    end: dict[str, Any] = {"setting": setting, "buffered": buffered}
    if state is None:
        raise RuntimeError("a remote flight needs a shared state store")
    try:
        async with contextlib.aclosing(state.subscribe_flight(
                flight_key, token, poll_s=ctx.lease_poll_s)) as events:
            async for event in events:
                if event is None:  # silence: is the leader still alive?
                    if deadline_ns is not None and clock() >= deadline_ns:
                        _end_remote(channel, "deadline_expired", "deadline_expired",
                                    sentences=sentences, **end)
                        return
                    if await state.flight_holder(flight_key) == token:
                        suspect = False
                        continue
                    # Gone twice in a row with nothing more in its log: a leader that
                    # finished would have logged its result before it released the lock.
                    if suspect:
                        _end_remote(channel, "internal_error", "leader_lost",
                                    sentences=sentences, **end)
                        return
                    suspect = True
                    continue
                name, data = str(event["name"]), dict(event["data"])
                if name == "":  # the leader stopped without a result (cancel, shutdown)
                    _end_remote(channel, "internal_error", "leader_stopped",
                                sentences=sentences, **end)
                    return
                sentences = sentences or name == "sentence"
                if name == _SUMMARY:
                    data["_remote"] = True
                channel.publish(name, data)
                if name == _SUMMARY:
                    return
    except Exception as exc:
        logger.warning("flight %s: following failed: %s", flight_key[:12], sanitize_error(exc))
        _end_remote(channel, "internal_error", "shared_state_unavailable",
                    sentences=sentences, **end)
    finally:
        channel.close()


async def _run_shared_flight(ctx: AskV1Context, flight: Flight, target: AskTarget,
                             **lead: Any) -> None:
    """Claim the flight across workers, then lead it here or follow it there. When the
    shared store cannot be reached, this worker leads alone (coalescing fails open; the
    shared quota still fails closed)."""
    go: asyncio.Event = lead["go"]
    await go.wait()
    state = ctx.state
    claimed: tuple[str, str] | None = None
    if state is not None:
        flight_key = flight_key_text(flight.key)
        try:
            lease = await state.acquire_flight_lock(flight_key, int(ctx.flight_ttl_s * 1000))
        except Exception as exc:
            logger.warning("flight %s: not shared (%s); led on this worker only",
                           flight_key[:12], type(exc).__name__)
        else:
            if not lease.owned:
                await _follow_remote(ctx, flight, flight_key, lease.token,
                                     setting=target.ask.retrieval_setting or "",
                                     buffered=lead["buffered"],
                                     deadline_ns=lead.get("deadline_ns"))
                return
            claimed = (flight_key, lease.token)
    mirror: _Mirror | None = None
    if claimed is not None:
        mirror = _Mirror(ctx, *claimed)
        flight.channel.mirror = mirror.push
        key, token = claimed
        flight.channel.on_abandoned = lambda: _abandon(ctx, flight, key, token)
    try:
        await _lead(ctx, flight, target, **lead)
    finally:
        if mirror is not None:
            mirror.finish()


# --- One request (one SSE response) ------------------------------------------------------


def _forget(ctx: AskV1Context, flight: Flight) -> None:
    """Drop a finished flight from the registry (also one cancelled before it started)."""
    flight.channel.close()
    if ctx.flights.get(flight.key) is flight:
        del ctx.flights[flight.key]


def _cancelled_spend(live: dict[str, Any]) -> dict[str, Any]:
    """What a cancelled flight had spent: the finished model call's own usage when it is
    known (a buffered answer cancelled while L4 scores it), otherwise estimated (v1 does
    the same for /ask)."""
    outcome = live.get("outcome")
    if isinstance(outcome, PipelineOutcome):
        return {"provider_called": outcome.provider_called, "tokens_in": outcome.tokens_in,
                "tokens_out": outcome.tokens_out, "usage_source": outcome.usage_source}
    tokens, chat = live.get("tokens"), live.get("chat")
    if tokens is None or chat is None:
        return {"provider_called": False, "tokens_in": 0, "tokens_out": 0,
                "usage_source": "none"}
    tokens_in = math.ceil(chat.prompt_chars / CONSERVATIVE_CHARS_PER_TOKEN)
    tokens_out = math.ceil(tokens.received_chars / CONSERVATIVE_CHARS_PER_TOKEN)
    route = tokens.estimated_route(tokens_in, tokens_out)
    spend: dict[str, Any] = {"provider_called": True, "tokens_in": tokens_in,
                             "tokens_out": tokens_out, "usage_source": "estimated"}
    if route is not None:  # V2-09: the lane, and an escalation's replaced call
        spend.update({"tokens_in": route.tokens_in, "tokens_out": route.tokens_out,
                      "lane_name": route.lane, "cost_usd": route.cost_usd})
    return spend


@dataclass(frozen=True)
class ConversationLog:
    """Where a request's turn is recorded (V2-08): the user's ORIGINAL question and the text
    actually delivered (its verified sentences, or the refusal message)."""

    conversation_id: str
    question: str


async def _record_turn(ctx: AskV1Context, log: ConversationLog | None, final: dict[str, Any],
                       sentences: list[str], request_id: str) -> None:
    """Append the turn when an answer reached the user (status ok). A store failure is
    logged and never changes the answer."""
    if log is None or final.get("status") != "ok":
        return
    text = " ".join(sentences) or final.get("message")
    if not text:
        return
    try:
        await ctx.store.conversations.append_turn(log.conversation_id, log.question, text)
    except Exception as exc:  # the answer is already decided: keep it
        logger.warning("/v1/ask request %s: conversation turn not recorded: %s", request_id,
                       sanitize_error(exc))


def _accepted(ctx: AskV1Context, channel: ReplayChannel | None, request_id: str, *,
              follower: bool) -> dict[str, Any]:
    queue = ctx.service.queue
    if follower and channel is not None:  # it waits for the flight it joined
        position = channel.position()
    else:
        busy = queue.active >= queue.max_active or queue.depth > 0
        position = queue.depth + 1 if busy else 0
    return {"request_id": request_id, "queue_position": position,
            "expected_wait_ms": round(position / queue.service_rate_per_s * 1000, 1)}


async def _cached(ctx: AskV1Context, hit: CachedAnswer, *, request_id: str,
                  arrived_ns: int, collection_id: str, buffered: bool,
                  setting: str, accepted_sent: bool = False,
                  conversation: ConversationLog | None = None,
                  rewrite: Rewrite | None = None,
                  tenant: str | None = None, trace: RequestTrace | None = None,
                  question: str | None = None) -> AsyncIterator[bytes]:
    clock = ctx.service.clock
    if not accepted_sent:
        yield sse("accepted", {"request_id": request_id, "queue_position": 0,
                               "expected_wait_ms": 0.0,
                               "conversation_id": None if conversation is None
                               else conversation.conversation_id})
    if hit.sources:  # cached by /v1/ask: the same passages the live answer sent
        yield sse("sources", {"passages": [s.model_dump() for s in hit.sources]})
    else:  # cached by v1's POST /ask, which keeps only the cited pages
        yield sse("sources", {"passages": [{
            "id": f"P{c.tag}", "chunk_id": c.chunk_id, "paper_id": c.paper_id,
            "short_name": None, "page_range": _page_range(c.page_start, c.page_end),
            "section": None, "text_excerpt": None, "text": None} for c in hit.citations]})
    first: float | None = None
    sentences: list[str] = []
    if not hit.refused:  # a refusal's text is a message, never a sentence
        for sentence in split_sentences(hit.delivered_text):
            first = first if first is not None else (clock() - arrived_ns) / _NS_PER_MS
            sentences.append(sentence)
            yield sse("sentence", {"text": sentence, "citations": tags_in(sentence),
                                   "confidence": None})  # not re-scored on a cache hit
    total = (clock() - arrived_ns) / _NS_PER_MS
    final = {"request_id": request_id, "status": "ok", "verdict": hit.verification_verdict,
             "error_code": None, "partial": False, "refused": hit.refused,
             "message": hit.delivered_text if hit.refused else None,
             "tokens_in": 0, "tokens_out": 0, "usage_source": "none",
             "provider_called": False, "model_id": hit.model_id,
             "retrieval_setting": setting, "collection_id": collection_id,
             "buffered": buffered, "queue_depth_at_entry": None,
             "cache_status": "exact_hit", "coalesced": False,
             "ttft_ms": first, "total_ms": round(total, 3),
             "conversation_id": None if conversation is None else conversation.conversation_id,
             "rewritten_question": None if rewrite is None else rewrite.question,
             **_route_fields(None)}
    _charge(final, rewrite)
    record = _request_record(request_id, hit_question=question, final=final,
                             collection_id=collection_id, role="cache_hit", tenant=tenant)
    record["breakdown"] = _breakdown(trace, final, {"sentences_checked": 0,
                                                    "sentences_passed": len(sentences)})
    ctx.record(record, final=final, tenant=tenant)
    await _record_turn(ctx, conversation, final, sentences, request_id)
    yield sse("final", final)


def _breakdown(trace: RequestTrace | None, final: dict[str, Any],
               verifier: dict[str, int]) -> dict[str, Any] | None:
    """End the request's trace and return what GET /v1/requests/{id} shows (V2-11)."""
    if trace is None:
        return None
    done = trace.finish(status=final.get("status"), error_code=final.get("error_code"),
                        cache_status=final.get("cache_status"), lane=final.get("lane_name"),
                        cost_usd=final.get("cost_usd"), escalated=final.get("escalated"),
                        coalesced=final.get("coalesced"), verdict=final.get("verdict"))
    return {**done, "verifier": {**verifier, "partial": bool(final.get("partial"))},
            "cache_status": final.get("cache_status"),
            "coalesced": bool(final.get("coalesced"))}


def _request_record(request_id: str, *, hit_question: str | None, final: dict[str, Any],
                    collection_id: str, role: str, tenant: str | None = None
                    ) -> dict[str, Any]:
    return {"record": "request", "endpoint": "/v1/ask", "request_id": request_id,
            "role": role, "collection_id": collection_id, "tenant_id": tenant,
            "question_sha256": None if hit_question is None else question_hash(hit_question),
            "final": {"status": final["status"], "cache_status": final["cache_status"],
                      "verification_verdict": final["verdict"],
                      "provider_first_token_ms": final.get("provider_first_token_ms"),
                      "first_sentence_ms": final["ttft_ms"],
                      "server_total_ms": final["total_ms"], "tokens_in": final["tokens_in"],
                      "tokens_out": final["tokens_out"],
                      "lane_name": final.get("lane_name"), "cost_usd": final.get("cost_usd"),
                      "escalated": final.get("escalated", False)}}


async def _respond(ctx: AskV1Context, target: AskTarget, *, request_id: str, arrived_ns: int,
                   collection_id: str, buffered: bool,
                   deadline_ns: int | None = None,
                   scorer: EntailmentScore | None = None,
                   accepted_sent: bool = False,
                   conversation: ConversationLog | None = None,
                   rewrite: Rewrite | None = None, tenant: str | None = None,
                   priority: str = Priority.INTERACTIVE,
                   trace: RequestTrace | None = None) -> AsyncIterator[bytes]:
    """One request. ``target.ask.question`` is the standalone question (a follow-up arrives
    here already rewritten, with ``accepted`` already sent)."""
    try:
        # aclosing: when the client leaves, the inner generator's own finally (go.set,
        # channel.leave) runs at once, not whenever it is garbage-collected
        async with contextlib.aclosing(_answer(
                ctx, target, request_id=request_id, arrived_ns=arrived_ns,
                collection_id=collection_id, buffered=buffered, deadline_ns=deadline_ns,
                scorer=scorer, accepted_sent=accepted_sent, conversation=conversation,
                rewrite=rewrite, tenant=tenant, priority=priority, trace=trace)) as answers:
            async for chunk in answers:
                yield chunk
    finally:
        if trace is not None:
            trace.close()  # the client left, or the service stopped: end the spans


async def _answer(ctx: AskV1Context, target: AskTarget, *, request_id: str, arrived_ns: int,
                  collection_id: str, buffered: bool, deadline_ns: int | None,
                  scorer: EntailmentScore | None, accepted_sent: bool,
                  conversation: ConversationLog | None, rewrite: Rewrite | None,
                  tenant: str | None, priority: str,
                  trace: RequestTrace | None) -> AsyncIterator[bytes]:
    service, ask = ctx.service, target.ask
    clock = service.clock
    setting = ask.retrieval_setting or ""
    if service.cache is not None:
        hit = await service.cache.lookup(ask.question, target.cache_setting)
        # A verified answer is replayed as sentences; a verified refusal as a message only.
        if hit is not None and (
                (hit.verification_verdict == "VERIFIED" and not hit.refused)
                or (hit.verification_verdict == "REFUSAL_VERIFIED" and hit.refused)):
            async for chunk in _cached(ctx, hit, request_id=request_id, arrived_ns=arrived_ns,
                                       collection_id=collection_id, buffered=buffered,
                                       setting=setting, accepted_sent=accepted_sent,
                                       conversation=conversation, rewrite=rewrite,
                                       tenant=tenant, trace=trace, question=ask.question):
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
        # V2-13: with a shared store, the flight is first claimed across workers
        lead = _lead if ctx.state is None or service.cache is None else _run_shared_flight
        channel.task = asyncio.create_task(lead(
            ctx, flight, target, request_id=request_id, collection_id=collection_id,
            buffered=buffered, go=go, deadline_ns=deadline_ns, scorer=scorer,
            tenant=tenant, priority=priority, trace=trace),
            name=f"v1-ask-{request_id}")
        channel.task.add_done_callback(lambda _: _forget(ctx, flight))
    try:
        if not accepted_sent:
            yield sse("accepted", {**_accepted(ctx, channel, request_id, follower=follower),
                                   "conversation_id": None if conversation is None
                                   else conversation.conversation_id})
        go.set()
        index = 0
        first: float | None = None
        sentences: list[str] = []
        # A follower's own deadline bounds its wait (v1: follower_timeout); the leader's
        # deadline, if any, already bounds the flight itself.
        own_deadline = deadline_ns if follower else None
        while True:
            wait_s = ctx.keepalive_s
            if own_deadline is not None:
                wait_s = max(0.0, min(wait_s, (own_deadline - clock()) / _NS_PER_S))
            event = await channel.next(index, wait_s)
            if event is None and own_deadline is not None and clock() >= own_deadline:
                event = (_SUMMARY, _shed_summary("deadline_expired", setting=setting,
                                                 buffered=buffered, depth=None))
                if first is not None:  # sentences went out: the answer is partial
                    yield sse("stopped", {"reason_class": "deadline_expired",
                                          "partial": True})
                    event[1]["partial"] = True
                index -= 1  # not an event of the flight
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
                # V2-13: a flight led on another worker counts as coalesced here
                coalesced = follower or bool(data.get("_remote"))
                final = {**{k: v for k, v in data.items() if k != "_remote"},
                         "request_id": request_id, "collection_id": collection_id,
                         "cache_status": ("coalesced" if coalesced else
                                          "off" if service.cache is None else "miss"),
                         "coalesced": coalesced, "ttft_ms": first,
                         "total_ms": round(total, 3),
                         "conversation_id": None if conversation is None
                         else conversation.conversation_id,
                         "rewritten_question": None if rewrite is None else rewrite.question}
                if coalesced:  # the leader paid for it
                    final.update({"tokens_in": 0, "tokens_out": 0, "usage_source": "none",
                                  "provider_called": False, "queue_depth_at_entry": None,
                                  # its row must not count the leader's lane or escalation
                                  **_route_fields(None)})
                verifier = final.pop("verifier", None) or {"sentences_checked": 0,
                                                           "sentences_passed": 0}
                _charge(final, rewrite)
                record = _request_record(
                    request_id, hit_question=ask.question, final=final,
                    collection_id=collection_id, role="follower" if coalesced else "leader",
                    tenant=tenant)
                record["breakdown"] = _breakdown(trace, final, verifier)
                ctx.record(record, final=final, tenant=tenant)
                await _record_turn(ctx, conversation, final, sentences, request_id)
                yield sse("final", final)
                return
            if name == "sentence":
                sentences.append(data["text"])
                if first is None:
                    first = round((clock() - arrived_ns) / _NS_PER_MS, 3)
            yield sse(name, data)
    finally:
        go.set()  # "accepted" went out (or the client left): followers must not wait
        channel.leave()


async def _follow_up(ctx: AskV1Context, target: AskTarget, turns: list[Turn],
                     conversation_id: str, **respond: Any) -> AsyncIterator[bytes]:
    """A follow-up: ``accepted`` at once, then the rewrite (bounded by the rewriter's own
    timeout; any failure keeps the original), then the ordinary flow on the standalone
    question, so the cache and SingleFlight see the rewritten question."""
    request_id: str = respond["request_id"]
    original = target.ask.question
    yield sse("accepted", {**_accepted(ctx, None, request_id, follower=False),
                           "conversation_id": conversation_id})
    yield sse("status", {"stage": "rewriting", "queue_position": 0})
    trace: RequestTrace | None = respond.get("trace")
    span = None if trace is None else trace.start("pipeline.rewrite", turns=len(turns))
    try:
        rewrite = await ctx.service.rewriter.resolve(original, turns)
    except Exception as exc:  # never let a rewrite fail the question
        logger.warning("/v1/ask request %s: rewrite failed, original kept: %s", request_id,
                       sanitize_error(exc))
        rewrite = Rewrite(original, original, False, "error", fallback=type(exc).__name__)
    if trace is not None:
        trace.end(span, method=rewrite.method, rewritten=rewrite.rewritten,
                  fallback=rewrite.fallback, tokens=rewrite.tokens,
                  standalone_hash=question_hash(rewrite.question))
    yield sse("status", {"stage": "rewriting", "queue_position": 0,
                         "rewritten_question": rewrite.question, "original_question": original,
                         "rewritten": rewrite.rewritten, "method": rewrite.method,
                         "fallback": rewrite.fallback})
    ctx.service.record_trace({"record": "rewrite", "endpoint": "/v1/ask",
                              "request_id": request_id, "conversation_id": conversation_id,
                              "method": rewrite.method, "rewritten": rewrite.rewritten,
                              "fallback": rewrite.fallback, "tokens": rewrite.tokens,
                              "ms": rewrite.ms, "turns": len(turns)})
    standalone = AskTarget(target.ask.model_copy(update={"question": rewrite.question}),
                           target.cache_setting)
    async for chunk in _respond(ctx, standalone, accepted_sent=True,
                                conversation=ConversationLog(conversation_id, original),
                                rewrite=rewrite, **respond):
        yield chunk


def _authenticator(ctx: AskV1Context) -> Authenticator:
    if ctx.auth is None:
        raise RuntimeError("AskV1Context has no authenticator")
    return ctx.auth


class CreateConversation(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    collection_id: Annotated[str, Field(min_length=1, max_length=64)]


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
        auth = ctx.auth
        if auth is None:
            raise RuntimeError("AskV1Context has no authenticator")
        try:
            caller = await auth.authenticate(request)
            tenant = caller.tenant_id
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
            turns: list[Turn] = []
            if body.conversation_id is not None:
                conversation = await ctx.store.conversations.get_by_id(body.conversation_id)
                if conversation is None or conversation.collection_id != collection_id:
                    raise Rejected(404, "conversation_not_found", "no such conversation")
                turns = await ctx.store.conversations.get_recent_turns(
                    body.conversation_id, limit=HISTORY_TURNS)
            request_id = f"v1ask-{uuid.uuid4().hex}"
            target = await service.target(AskRequest(
                request_id=request_id, question=body.question, collection_id=collection_id))
            auth.charge(caller)  # the key's daily limit (429 past it), once it is accepted
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
        respond: dict[str, Any] = {
            "request_id": request_id, "arrived_ns": arrived_ns, "collection_id": collection_id,
            "buffered": collection.buffered,
            "scorer": service.entailment if collection.l4_enabled else None,
            "deadline_ns": None if body.deadline_ms is None
            else arrived_ns + body.deadline_ms * _NS_PER_MS,
            "tenant": tenant, "priority": body.priority,
            # V2-11: the request's trace (ids and hashes only, never the question text)
            "trace": service.tracing.request(
                request_id=request_id, tenant_id=tenant, collection_id=collection_id,
                question_hash=question_hash(body.question), priority=body.priority,
                follow_up=bool(turns))}
        if turns and body.conversation_id is not None:  # a follow-up: rewrite first
            stream = _follow_up(ctx, target, turns, body.conversation_id, **respond)
        else:  # a first question: no rewrite (no model call, no latency)
            log = None if body.conversation_id is None else ConversationLog(
                body.conversation_id, body.question)
            stream = _respond(ctx, target, conversation=log, **respond)
        return StreamingResponse(stream, media_type="text/event-stream",
                                 headers={"cache-control": "no-store",
                                          "x-accel-buffering": "no"})

    @router.post("/conversations")
    async def create_conversation(request: Request) -> Response:
        try:
            tenant = (await _authenticator(ctx).authenticate(request)).tenant_id
            body = CreateConversation.model_validate_json(await request.body())
            collection = await ctx.store.collections.get_by_id(body.collection_id)
            if collection is None or collection.tenant_id != tenant:
                raise Rejected(404, "collection_not_found", "no such collection")
            created = await ctx.store.conversations.create(collection.id)
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        except ValidationError:
            return _error(422, "invalid_request", "send JSON {\"collection_id\": ...}")
        return JSONResponse({"id": created.id, "collection_id": created.collection_id,
                             "created_at": created.created_at}, status_code=201)

    @router.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> Response:
        try:
            tenant = (await _authenticator(ctx).authenticate(request)).tenant_id
            found = await ctx.store.conversations.get_by_id(conversation_id)
            collection = None if found is None else await ctx.store.collections.get_by_id(
                found.collection_id)
            if found is None or collection is None or collection.tenant_id != tenant:
                raise Rejected(404, "conversation_not_found", "no such conversation")
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        messages = await ctx.store.conversations.list_messages(found.id)
        return JSONResponse({"id": found.id, "collection_id": found.collection_id,
                             "created_at": found.created_at,
                             "messages": [{"role": m.role, "content": m.content,
                                           "created_at": m.created_at} for m in messages]})

    return router


__all__ = ["DEFAULT_KEEPALIVE_S", "DEFAULT_LEASE_POLL_S", "EXCERPT_CHARS",
           "AskV1Body", "AskV1Context", "Flight", "ReplayChannel",
           "ask_v1_router", "flight_key_text", "sources_payload", "sse"]
