"""Prometheus metrics for ``GET /metrics`` (plan V2-11; docs/ARCHITECTURE_V2.md §11).

Every app has its own registry (tests and several apps in one process never collide).
Labels are closed sets or ids, never text: a status, an error code, a tenant id, a lane
name, a stage, a direction, a cache event, a verifier layer. No question, document text
or key ever becomes a label value.

Queue depth and quota headroom are read live when Prometheus scrapes (custom collectors),
so they are always current without anyone having to update them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

disable_created_metrics()  # no *_created series: they only add noise to a scrape

LATENCY_BUCKETS_S: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
LAYERS: Final = ("L1", "L2", "L3", "L4")
CACHE_EVENTS: Final = {"exact_hit": "hit", "miss": "miss", "coalesced": "coalesced"}
UNKNOWN: Final = "none"

# A stopped sentence's reason class -> the verifier layer that blocked it (V2-07 layers:
# L1 structure, L2 literal grounding, L3 refusal form; L4 is advisory and never blocks).
_REASON_LAYER: Final = {
    "uncited": "L1", "unknown_citation": "L2", "metadata_mismatch": "L2",
    "ungrounded": "L2", "refusal_as_claim": "L3", "refusal_with_content": "L3",
}
_CODE_LAYER: Final = {
    "empty_answer": "L1", "no_citations": "L1", "uncited_sentence": "L1",
    "unknown_passage_tag": "L2", "paper_mismatch": "L2", "arxiv_mismatch": "L2",
    "page_mismatch": "L2", "ungrounded_number": "L2", "ungrounded_identifier": "L2",
    "refusal_with_content": "L3", "refusal_as_claim": "L3",
}


def layer_of(reason_class: str | None, failure_codes: Iterable[str] = ()) -> str | None:
    """The layer that blocked a sentence or an answer; None when it was no verifier block
    (a truncated answer, a provider failure)."""
    for code in failure_codes:
        if code in _CODE_LAYER:
            return _CODE_LAYER[code]
    return None if reason_class is None else _REASON_LAYER.get(reason_class)


def _label(value: Any) -> str:
    return UNKNOWN if value is None or value == "" else str(value)


class _LiveGauges(Collector):
    """Gauges computed at scrape time from callables (queue depth, quota headroom)."""

    def __init__(self, queue_depth: Callable[[], Mapping[tuple[str, str], int]] | None,
                 headroom: Callable[[], Mapping[str, int | None]] | None) -> None:
        self.queue_depth = queue_depth
        self.headroom = headroom

    def collect(self) -> Iterable[Metric]:
        depth = GaugeMetricFamily("relay_queue_depth", "Requests waiting for a slot",
                                  labels=["priority", "tenant"])
        if self.queue_depth is not None:
            for (priority, tenant), count in sorted(self.queue_depth().items()):
                depth.add_metric([priority, tenant], count)
        yield depth
        room = GaugeMetricFamily("relay_quota_headroom_tokens",
                                 "Tokens the lane's guard can still admit this minute",
                                 labels=["lane"])
        if self.headroom is not None:
            for lane, tokens in sorted(self.headroom().items()):
                if tokens is not None:  # no TPM limit known: nothing to report
                    room.add_metric([lane], tokens)
        yield room


class RelayMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter("relay_requests_total", "Requests by outcome",
                                ["status", "error_code", "tenant", "lane"],
                                registry=self.registry)
        self.latency = Histogram("relay_request_latency_seconds", "Request latency by stage",
                                 ["stage", "lane"], buckets=LATENCY_BUCKETS_S,
                                 registry=self.registry)
        self.tokens = Counter("relay_tokens_total", "Model tokens by direction and lane",
                              ["direction", "lane"], registry=self.registry)
        self.cache = Counter("relay_cache_events_total", "Cache and coalescing outcomes",
                             ["event"], registry=self.registry)
        self.blocks = Counter("relay_verifier_blocks_total",
                              "Answers or sentences blocked, by verifier layer", ["layer"],
                              registry=self.registry)
        for layer in LAYERS:  # every layer is visible, at 0 until it blocks (L4 never does)
            self.blocks.labels(layer=layer)
        for event in CACHE_EVENTS.values():
            self.cache.labels(event=event)
        self._live = _LiveGauges(None, None)
        self.registry.register(self._live)

    def watch(self, *, queue_depth: Callable[[], Mapping[tuple[str, str], int]] | None = None,
              headroom: Callable[[], Mapping[str, int | None]] | None = None) -> None:
        """Where the live gauges read from (set once the queue and router exist)."""
        if queue_depth is not None:
            self._live.queue_depth = queue_depth
        if headroom is not None:
            self._live.headroom = headroom

    def observe_request(self, final: Mapping[str, Any], *, tenant: str | None) -> None:
        """One finished request (its ``final`` event)."""
        lane = _label(final.get("lane_name"))
        self.requests.labels(status=_label(final.get("status")),
                             error_code=_label(final.get("error_code")),
                             tenant=_label(tenant), lane=lane).inc()
        for stage, key in (("first_sentence", "ttft_ms"), ("ttft", "provider_first_token_ms"),
                           ("total", "total_ms")):
            value = final.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                self.latency.labels(stage=stage, lane=lane).observe(value / 1000.0)
        ledger = final.get("ledger")
        if isinstance(ledger, list) and ledger:
            for use in ledger:
                self.tokens.labels(direction="in", lane=_label(use.get("lane"))).inc(
                    max(0, int(use.get("tokens_in") or 0)))
                self.tokens.labels(direction="out", lane=_label(use.get("lane"))).inc(
                    max(0, int(use.get("tokens_out") or 0)))
        elif final.get("provider_called"):  # v1 /ask: no ledger, the request's own totals
            self.tokens.labels(direction="in", lane=lane).inc(int(final.get("tokens_in") or 0))
            self.tokens.labels(direction="out", lane=lane).inc(
                int(final.get("tokens_out") or 0))
        event = CACHE_EVENTS.get(str(final.get("cache_status")))
        if event is not None:
            self.cache.labels(event=event).inc()

    def observe_spend(self, lane: str | None, tokens_in: int, tokens_out: int) -> None:
        """Tokens of an answer whose client left before its end (no ``final`` to count)."""
        self.tokens.labels(direction="in", lane=_label(lane)).inc(max(0, int(tokens_in)))
        self.tokens.labels(direction="out", lane=_label(lane)).inc(max(0, int(tokens_out)))

    def observe_block(self, layer: str | None) -> None:
        if layer in LAYERS:
            self.blocks.labels(layer=layer).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)


__all__ = ["CACHE_EVENTS", "LATENCY_BUCKETS_S", "LAYERS", "RelayMetrics", "layer_of"]
