"""Replay of the v1 Table 3 run through the generalised (V2-04) pipeline.

v1 Table 3 is the canonical run ``exp-20261113T064542Z-c482696``: the 21 evaluation
questions in each of the three retrieval settings, answered by the real model. Its traces
record each question, the passages retrieved, the scope and gate decisions, the model's
**raw answer** and the outcome.

The replay runs each question through today's pipeline with the same retrieval indexes,
and replaces the model with a ``ScriptedProvider`` that returns the recorded raw answer
(or the recorded provider failure). No real call is made: what is tested is everything
around the model (scope, retrieval, gate, verifier, messages), and the verdict must match
v1. Two paths:

- ``v1``: the v1 ``/ask`` path (no collection), all three settings;
- ``v1-papers``: the ``v1-papers`` collection (preset ``small_fast``), through the store,
  the document registry's cue set and the index registry.

The evidence files are only read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from relay.orchestrator.pipeline import (
    CollectionResolver,
    Pipeline,
    PipelineConfig,
    PipelineOutcome,
    PipelineRequest,
    SingleCallGenerator,
)
from relay.orchestrator.tools import PassageRetriever, ToolGateway
from relay.providers.base import ScriptedProvider, ScriptedReply
from relay.providers.errors import ProviderConnectionError
from relay.telemetry.spans import ServerSpanTracker

TABLE3_RUN: Final = "exp-20261113T064542Z-c482696"
ReplayPath = Literal["v1", "v1-papers"]


@dataclass(frozen=True)
class RecordedRow:
    question_id: str
    setting: str
    question: str
    kind: str
    verdict: str
    error_code: str | None
    delivered_text: str | None
    raw_answer: str | None
    finish_reason: str | None
    provider_error: str | None
    passages: tuple[str, ...]
    gate_actions: tuple[str, ...]
    scope: dict[str, Any]


def load_rows(run_dir: Path) -> list[RecordedRow]:
    rows = []
    for line in (run_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        outcome = record["outcome"]
        trace = outcome["trace"]
        rows.append(RecordedRow(
            question_id=record["question_id"], setting=record["retrieval_setting"],
            question=trace["scope"]["question"], kind=outcome["kind"],
            verdict=outcome["verification_verdict"], error_code=outcome["error_code"],
            delivered_text=outcome["delivered_text"], raw_answer=trace.get("raw_answer"),
            finish_reason=trace.get("finish_reason"),
            provider_error=trace.get("provider_error"),
            passages=tuple(p["chunk_id"] for p in outcome["passages"]),
            gate_actions=tuple(d["action"] for d in trace["gate_decisions"]),
            scope=trace["scope"],
        ))
    return rows


def recorded_reply(row: RecordedRow) -> list[ScriptedReply]:
    """What the model returned in v1: its raw answer, or its failure."""
    if row.raw_answer is not None:
        return [ScriptedReply(content_chunks=(row.raw_answer,),
                              finish_reason=row.finish_reason or "stop")]
    if row.provider_error is not None and "ProviderConnectionError" in row.provider_error:
        return [ScriptedReply(error=ProviderConnectionError("recorded v1 failure",
                                                            reason="replay"))]
    return []  # v1 never called the model; a call now would fail the replay


@dataclass(frozen=True)
class ReplayResult:
    row: RecordedRow
    path: ReplayPath
    kind: str
    verdict: str
    error_code: str | None
    delivered_text: str | None
    passages: tuple[str, ...]
    gate_actions: tuple[str, ...]
    scope: dict[str, Any]
    error: str | None = None  # the replay itself failed (for example an unexpected call)

    @property
    def differences(self) -> list[str]:
        if self.error is not None:
            return [f"replay failed: {self.error}"]
        checks = {
            "verdict": (self.row.verdict, self.verdict),
            "kind": (self.row.kind, self.kind),
            "error_code": (self.row.error_code, self.error_code),
            "delivered_text": (self.row.delivered_text, self.delivered_text),
            "passages": (self.row.passages, self.passages),
            "gate": (self.row.gate_actions, self.gate_actions),
            "scope": (self.row.scope, self.scope),
        }
        return [name for name, (old, new) in checks.items() if old != new]

    @property
    def matches(self) -> bool:
        return not self.differences


def _result(row: RecordedRow, path: ReplayPath, outcome: PipelineOutcome) -> ReplayResult:
    trace = outcome.trace
    return ReplayResult(
        row=row, path=path, kind=outcome.kind, verdict=outcome.verification_verdict,
        error_code=outcome.error_code, delivered_text=outcome.delivered_text,
        passages=tuple(p.chunk_id for p in outcome.passages),
        gate_actions=tuple(d.action for d in trace.gate_decisions),
        scope=trace.scope.model_dump(mode="json"))


PipelineFactory = Callable[[ScriptedProvider], Pipeline]


def pipeline_factory(retrievers: Mapping[str, PassageRetriever], top_k: Mapping[str, int],
                     vocabulary: frozenset[str] | None, *, model_id: str,
                     collections: CollectionResolver | None = None) -> PipelineFactory:
    """A fresh pipeline per question, around that question's scripted model."""
    gateway = ToolGateway(retrievers)

    def build(provider: ScriptedProvider) -> Pipeline:
        return Pipeline(gateway=gateway, generator=SingleCallGenerator(provider),
                        config=PipelineConfig(model_id=model_id), top_k=top_k,
                        vocabulary=vocabulary, budget=None, collections=collections)
    return build


async def replay(rows: Sequence[RecordedRow], build: PipelineFactory, *, path: ReplayPath,
                 collection_id: str | None = None) -> list[ReplayResult]:
    results = []
    for n, row in enumerate(rows, start=1):
        provider = ScriptedProvider(recorded_reply(row))
        request = PipelineRequest(request_id=f"replay-{n:03d}", question=row.question,
                                  retrieval_setting=row.setting, collection_id=collection_id)
        try:
            outcome = await build(provider).run(request, ServerSpanTracker())
        except Exception as exc:  # report it in the table; never hide a failed replay
            results.append(ReplayResult(row=row, path=path, kind="-", verdict="-",
                                        error_code=None, delivered_text=None, passages=(),
                                        gate_actions=(), scope={},
                                        error=type(exc).__name__))
            continue
        results.append(_result(row, path, outcome))
    return results


# --- Streaming replay (V2-05) ---------------------------------------------------------

STREAM_PIECE_CHARS: Final = 4  # about one token per piece


def streamed_reply(row: RecordedRow) -> list[ScriptedReply]:
    """The recorded answer, cut into token-sized pieces (failures as in ``recorded_reply``)."""
    if row.raw_answer is None:
        return recorded_reply(row)
    text = row.raw_answer
    pieces = tuple(text[i:i + STREAM_PIECE_CHARS] for i in range(0, len(text),
                                                                 STREAM_PIECE_CHARS))
    return [ScriptedReply(content_chunks=pieces, finish_reason=row.finish_reason or "stop")]


@dataclass(frozen=True)
class StreamingReplayResult:
    row: RecordedRow
    kind: str
    verdict: str
    sentences: int  # verified sentences released to the client
    stopped: str | None  # reason class when the stream stopped
    partial: bool  # sentences were released, then the answer stopped
    error: str | None = None

    @property
    def agrees(self) -> bool:
        """Streaming reaches v1's verdict: verified answers stream fully, refusals stay
        refusals, and an answer v1 blocked is never verified."""
        return self.error is None and self.verdict == self.row.verdict


async def replay_streaming(rows: Sequence[RecordedRow], build: PipelineFactory, *,
                           collection_id: str | None = None) -> list[StreamingReplayResult]:
    from relay.orchestrator.streaming import SentenceStream, StoppedEvent, StreamRefusal

    results = []
    for n, row in enumerate(rows, start=1):
        pipeline = build(ScriptedProvider(streamed_reply(row)))
        spans = ServerSpanTracker()
        request = PipelineRequest(request_id=f"stream-replay-{n:03d}", question=row.question,
                                  retrieval_setting=row.setting, collection_id=collection_id)
        try:
            plan = await pipeline.prepare_streaming(request, spans)
            if plan.early is not None:
                results.append(StreamingReplayResult(row, plan.early.kind,
                                                     plan.early.verification_verdict, 0,
                                                     None, False))
                continue
            tokens = pipeline.run_streaming(plan, spans)
            stream = SentenceStream(tokens, plan.passages, plan.streaming_verifier)
            events = [event async for event in stream]
            stop = next((e for e in events if isinstance(e, StoppedEvent)), None)
            refused = any(isinstance(e, StreamRefusal) for e in events)
            outcome = await pipeline.complete_streaming(plan, tokens, stream, stop=stop,
                                                        refused=refused)
        except Exception as exc:  # reported in the table; never hidden
            results.append(StreamingReplayResult(row, "-", "-", 0, None, False,
                                                 error=type(exc).__name__))
            continue
        released = len(stream.sentences)
        reason = (stop.reason_class if stop is not None else
                  "final_verification_failed" if outcome.verification_verdict == "BLOCKED"
                  else None)
        results.append(StreamingReplayResult(
            row, outcome.kind, outcome.verification_verdict, released, reason,
            partial=released > 0 and outcome.kind != "answer"))
    return results


def streaming_table(results: Sequence[StreamingReplayResult]) -> str:
    lines = ["| Question | Setting | v1 verdict | Streamed verdict | Sentences released | "
             "Stopped | Partial | Agrees |", "| :-- | :-- | :-- | :-- | --: | :-- | :-- | :-- |"]
    for r in results:
        lines.append(f"| {r.row.question_id} | {r.row.setting} | {r.row.verdict} | "
                     f"{r.verdict} | {r.sentences} | {r.stopped or '—'} | "
                     f"{'yes' if r.partial else 'no'} | {'yes' if r.agrees else '**no**'} |")
    return "\n".join(lines)


def markdown_table(results: Sequence[ReplayResult]) -> str:
    lines = ["| Question | Setting | Path | v1 verdict | Replay verdict | Match | Differences |",
             "| :-- | :-- | :-- | :-- | :-- | :-- | :-- |"]
    for r in results:
        lines.append(f"| {r.row.question_id} | {r.row.setting} | {r.path} | {r.row.verdict} | "
                     f"{r.verdict} | {'yes' if r.matches else '**no**'} | "
                     f"{', '.join(r.differences) or '—'} |")
    return "\n".join(lines)


__all__ = ["TABLE3_RUN", "RecordedRow", "ReplayResult", "StreamingReplayResult", "load_rows",
           "markdown_table", "pipeline_factory", "recorded_reply", "replay",
           "replay_streaming", "streamed_reply", "streaming_table"]
