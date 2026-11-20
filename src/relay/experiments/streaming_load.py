"""Load harness for verified streaming: ``POST /v1/ask`` first-status and first-sentence
times (plan V2-05).

Per request the client records, from just before it sends:

- ``first_status_ms``: the ``accepted`` event (first-status time);
- ``first_sentence_ms``: the first ``sentence`` event (first verified text);
- ``total_ms``: the ``final`` event.

``steady_streaming``: ``concurrency`` requests at once, repeated for ``rounds`` rounds
``round_gap_s`` apart, each request a different question (so nothing is served from the
cache or coalesced). Results go to a new run directory, never to ``evidence/``.

Percentiles: ``relay.experiments.load_runner.pct`` (nearest rank, as the v1 load harness).
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, Final

import httpx
from fastapi import FastAPI

from relay.experiments.load_runner import pct

ASK_PATH: Final = "/v1/ask"


@dataclass(frozen=True)
class StreamingScenario:
    name: str
    concurrency: int
    rounds: int
    round_gap_s: float
    # Warm-up rounds run first and are reported apart (round -1): the cold start (lazy
    # model load, the guard's first token estimates) is not the steady state.
    warmup_rounds: int = 1


# 1 warm-up round, then 3 rounds of 5 concurrent streams, a minute apart: each round
# fits the stand-in's 8,000 TPM, so the times measure streaming, not quota waits.
STREAMING_SCENARIOS: Final[dict[str, StreamingScenario]] = {
    "steady_streaming": StreamingScenario(name="steady_streaming", concurrency=5, rounds=3,
                                          round_gap_s=60.0),
}


@dataclass
class StreamResult:
    question_id: str
    round: int
    http_status: int
    request_id: str | None = None
    first_status_ms: float | None = None
    first_sentence_ms: float | None = None
    total_ms: float | None = None
    sentences: int = 0
    stopped: str | None = None  # reason_class of a "stopped" event
    partial: bool = False
    status: str | None = None
    verdict: str | None = None
    error_code: str | None = None
    cache_status: str | None = None
    keepalives: int = 0
    max_gap_ms: float | None = None  # the longest silence between two events
    provider_first_token_ms: float | None = None  # server side, from the final event
    final: dict[str, Any] | None = None  # the whole final event (V2-14: spend, refusal)


async def stream_one(client: httpx.AsyncClient, question_id: str, question: str, *,
                     collection_id: str | None, auth: str, round_: int = 0) -> StreamResult:
    """One request; a transport or protocol failure is recorded, not raised."""
    started = time.perf_counter()
    try:
        return await _stream_one(client, question_id, question, collection_id=collection_id,
                                 auth=auth, round_=round_, started=started)
    except httpx.HTTPError as exc:  # transport: the request did not complete
        return StreamResult(question_id=question_id, round=round_, http_status=0,
                            error_code=type(exc).__name__,
                            total_ms=round((time.perf_counter() - started) * 1000, 3))
    except (ValueError, KeyError) as exc:  # protocol: the server sent something malformed
        return StreamResult(question_id=question_id, round=round_, http_status=-1,
                            error_code=f"protocol:{type(exc).__name__}",
                            total_ms=round((time.perf_counter() - started) * 1000, 3))


async def _stream_one(client: httpx.AsyncClient, question_id: str, question: str, *,
                      collection_id: str | None, auth: str, round_: int,
                      started: float) -> StreamResult:
    body: dict[str, Any] = {"question": question}
    if collection_id is not None:
        body["collection_id"] = collection_id

    def now_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    async with client.stream("POST", ASK_PATH, json=body,
                             headers={"Authorization": auth}) as response:
        result = StreamResult(question_id=question_id, round=round_,
                              http_status=response.status_code)
        if response.status_code != 200:
            await response.aread()
            result.total_ms = now_ms()
            return result
        name, last, gaps = "", 0.0, []
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
                continue
            if not line.startswith("data: "):
                continue
            at = now_ms()
            gaps.append(at - last)
            last = at
            data = json.loads(line[6:])
            if name == "accepted":
                result.first_status_ms = at
                result.request_id = data["request_id"]
            elif name == "status" and data.get("keepalive"):
                result.keepalives += 1
            elif name == "sentence":
                result.sentences += 1
                if result.first_sentence_ms is None:
                    result.first_sentence_ms = at
            elif name == "stopped":
                result.stopped = data["reason_class"]
            elif name == "final":
                result.total_ms = at
                result.partial = bool(data["partial"])
                result.status, result.verdict = data["status"], data["verdict"]
                result.error_code, result.cache_status = data["error_code"], data["cache_status"]
                result.provider_first_token_ms = data.get("provider_first_token_ms")
                result.final = data
        result.max_gap_ms = round(max(gaps), 3) if gaps else None
        return result


async def run_streaming_scenario(client: httpx.AsyncClient, scenario: StreamingScenario,
                                 questions: Sequence[tuple[str, str]], *,
                                 collection_id: str | None, auth: str,
                                 sleep: Any = asyncio.sleep) -> list[StreamResult]:
    """``questions`` are used in order, ``concurrency`` per round; there must be enough."""
    needed = scenario.concurrency * scenario.rounds
    if len(questions) < needed:
        raise ValueError(f"{scenario.name} needs {needed} distinct questions, got "
                         f"{len(questions)}")
    results: list[StreamResult] = []
    # The same words (so scope, retrieval and the gate behave alike) plus one "?": a
    # different cache key, so a warm-up answer is never a later cache hit.
    warm = [(f"{qid}-warmup", f"{text}?") for qid, text in questions[:scenario.concurrency]]
    plan = [(-1, warm)] * scenario.warmup_rounds + [
        (r, list(questions[r * scenario.concurrency:(r + 1) * scenario.concurrency]))
        for r in range(scenario.rounds)]
    for n, (round_, batch) in enumerate(plan):
        if n and scenario.round_gap_s:
            await sleep(scenario.round_gap_s)
        results += await asyncio.gather(*(
            stream_one(client, qid, text, collection_id=collection_id, auth=auth,
                       round_=round_) for qid, text in batch))
    return results


def _dist(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": pct(values, 0.5), "p95": pct(values, 0.95),
            "max": max(values) if values else None}


def summarise_streaming(scenario: StreamingScenario,
                        results: Sequence[StreamResult]) -> dict[str, Any]:
    """Steady-state numbers over the measured rounds; warm-up rounds under ``warmup``."""
    measured = [r for r in results if r.round >= 0]
    summary = _summary(measured)
    summary["scenario"] = asdict(scenario)
    summary["warmup"] = _summary([r for r in results if r.round < 0])
    return summary


def _summary(results: Sequence[StreamResult]) -> dict[str, Any]:
    ok = [r for r in results if r.http_status == 200]
    partial = sum(r.partial for r in ok)
    return {
        "requests": len(results), "http_200": len(ok),
        "final_missing": sum(r.status is None for r in ok),  # invariant: always a final
        "protocol_errors": sum(r.http_status == -1 for r in results),
        "transport_errors": sum(r.http_status == 0 for r in results),
        "first_status_ms": _dist([r.first_status_ms for r in ok
                                  if r.first_status_ms is not None]),
        "first_sentence_ms": _dist([r.first_sentence_ms for r in ok
                                    if r.first_sentence_ms is not None]),
        "total_ms": _dist([r.total_ms for r in ok if r.total_ms is not None]),
        "provider_first_token_ms": _dist([r.provider_first_token_ms for r in ok
                                          if r.provider_first_token_ms is not None]),
        "max_gap_ms": max((r.max_gap_ms for r in ok if r.max_gap_ms is not None),
                          default=None),
        "verdicts": {v: sum(r.verdict == v for r in ok) for v in sorted({
            r.verdict for r in ok if r.verdict})},
        "stopped": {s: sum(r.stopped == s for r in ok) for s in sorted({
            r.stopped for r in ok if r.stopped})},
        "partial": partial, "partial_rate": round(partial / len(ok), 4) if ok else None,
        "with_sentences": sum(r.sentences > 0 for r in ok),
    }


@asynccontextmanager
async def serve_app_on_loopback(app: FastAPI) -> AsyncGenerator[str]:
    """Run ``app`` under uvicorn on 127.0.0.1 (a free port); yields its base URL."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("server stopped during startup")
            await asyncio.sleep(0.005)
        yield f"http://127.0.0.1:{port}"
    finally:
        try:
            server.should_exit = True
            if not task.done():
                await task
        finally:
            sock.close()


__all__ = ["STREAMING_SCENARIOS", "StreamResult", "StreamingScenario", "run_streaming_scenario",
           "serve_app_on_loopback", "stream_one", "summarise_streaming"]
