"""OpenTelemetry request tracing (plan V2-11; docs/ARCHITECTURE_V2.md §11).

One ``RequestTrace`` per ``POST /v1/ask`` request: a root ``request`` span and its children,
created with explicit parents (the work runs in several tasks and async generators, where
the "current span" context would not follow it reliably):

    request                 request_id, tenant_id, question_hash, collection_id, ...
      pipeline.rewrite      follow-ups only
      queue.wait            priority, queue_position
      pipeline.retrieve     chunks_found, preset
      pipeline.generate     lane, model, tokens_in, tokens_out (escalated, fallback)
        verifier.sentence   sentence_idx, l1_pass, l2_pass, l3_pass, l4_score (sync)
      pipeline.verify       the whole-answer pass (and buffered L4 scoring, sync)
      verifier.l4           sentence_idx, l4_score (l4_mode "async": after the queue slot
                            was released, one per sent sentence; F-02c)

The v1 span kinds map onto them: ``queue.wait`` is v1's *waiting*, the pipeline spans are
its *working*, and the root span carries v1's waiting/working/retrying totals.

The latency waterfall is computed from these spans' own timestamps, so it always matches
the recorded trace. Finished spans are also kept in a bounded in-memory store (the local
exporter); another exporter (OTLP, console) can be added with ``Tracing.add_exporter``.

Privacy: attributes are ids, hashes, counts, booleans and closed-set names. A question is
only ever its SHA-256 (``question_hash``); no text, passage or key is set on a span.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

SERVICE_NAME: Final = "the-relay"
DEFAULT_KEPT_SPANS: Final = 2000
_NS_PER_MS: Final = 1_000_000
Attribute = str | bool | int | float

# The waterfall's stages and the span each one is measured from.
WATERFALL: Final = (("rewrite_ms", "pipeline.rewrite"), ("queue_ms", "queue.wait"),
                    ("retrieval_ms", "pipeline.retrieve"),
                    ("generation_ms", "pipeline.generate"),
                    ("verification_ms", "pipeline.verify"))


class RecentSpans(SpanProcessor):
    """The last ``limit`` finished spans, in memory (bounded: it never grows without end)."""

    def __init__(self, limit: int = DEFAULT_KEPT_SPANS) -> None:
        self._spans: deque[ReadableSpan] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._spans.append(span)

    def spans(self) -> list[ReadableSpan]:
        with self._lock:
            return list(self._spans)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


class Tracing:
    """The app's tracer provider (not the global one: apps and tests stay independent)."""

    def __init__(self, *, keep: int = DEFAULT_KEPT_SPANS) -> None:
        self.provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        self.recent = RecentSpans(keep)
        self.provider.add_span_processor(self.recent)
        self.tracer = self.provider.get_tracer("relay")

    def add_exporter(self, exporter: SpanExporter) -> None:
        self.provider.add_span_processor(SimpleSpanProcessor(exporter))

    def request(self, **attributes: Attribute | None) -> RequestTrace:
        return RequestTrace(self, attributes)


def _clean(attributes: dict[str, Attribute | None]) -> dict[str, Attribute]:
    return {key: value for key, value in attributes.items() if value is not None}


def _ms(span: ReadableSpan) -> float:
    if span.start_time is None or span.end_time is None:
        return 0.0
    return round((span.end_time - span.start_time) / _NS_PER_MS, 3)


class RequestTrace:
    """One request's spans."""

    def __init__(self, tracing: Tracing, attributes: dict[str, Attribute | None]) -> None:
        self._tracer = tracing.tracer
        self.root: Span = self._tracer.start_span("request",  # type: ignore[assignment]
                                                  attributes=_clean(attributes))
        self._context: Context = trace.set_span_in_context(self.root)
        self._spans: list[Span] = []
        self._lock = threading.Lock()
        self.finished = False

    def start(self, name: str, *, parent: Span | None = None,
              **attributes: Attribute | None) -> Span:
        """A child span, started now; end it with ``end(span)`` (or use ``span()``). Once
        the trace is finished (the client left while a follower kept the flight going),
        this is a non-recording span that is neither kept nor exported."""
        if self.finished:
            return trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)  # type: ignore[return-value]
        context = self._context if parent is None else trace.set_span_in_context(parent)
        span: Span = self._tracer.start_span(name, context=context,  # type: ignore[assignment]
                                             attributes=_clean(attributes))
        with self._lock:
            self._spans.append(span)
        return span

    @contextmanager
    def span(self, name: str, **attributes: Attribute | None) -> Generator[Span]:
        span = self.start(name, **attributes)
        try:
            yield span
        finally:
            self.end(span)

    def end(self, span: Span | None, **attributes: Attribute | None) -> None:
        """Set ``attributes`` on ``span`` and end it, unless it has ended already (its trace
        was finished first): then nothing happens, and OTel logs no warning."""
        if span is None or not span.is_recording():
            return
        span.set_attributes(_clean(attributes))
        span.end()

    def instant(self, name: str, *, parent: Span | None = None,
                **attributes: Attribute | None) -> None:
        """A zero-length span (a check made at one moment, e.g. one sentence released)."""
        self.end(self.start(name, parent=parent, **attributes))

    def set(self, **attributes: Attribute | None) -> None:
        if self.root.is_recording():
            self.root.set_attributes(_clean(attributes))

    def finish(self, **attributes: Attribute | None) -> dict[str, Any]:
        """End the request (once) and return its breakdown: the waterfall and the spans."""
        if not self.finished:
            self.set(**attributes)
            self.finished = True
            with self._lock:
                for span in self._spans:
                    if span.end_time is None:
                        span.end()
            self.root.end()
        return self.breakdown()

    def close(self) -> None:
        """End whatever is still open (the client left, or the service is stopping)."""
        if not self.finished:
            self.finish(abandoned=True)

    def breakdown(self) -> dict[str, Any]:
        with self._lock:
            spans: list[ReadableSpan] = [*self._spans]
        root: ReadableSpan = self.root
        start = root.start_time or 0
        totals: dict[str, float] = {}
        for key, name in WATERFALL:
            totals[key] = round(float(sum(_ms(s) for s in spans if s.name == name)), 3)
        waterfall = {**totals, "total_ms": _ms(root)}
        return {"waterfall": waterfall, "spans": [
            {"name": s.name, "start_ms": round(((s.start_time or start) - start) / _NS_PER_MS, 3),
             "duration_ms": _ms(s), "attributes": dict(s.attributes or {})}
            for s in [root, *spans]]}


__all__ = ["SERVICE_NAME", "WATERFALL", "RecentSpans", "RequestTrace", "Span", "Tracing"]
