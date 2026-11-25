"""F-03: does the provider refund the unused token reservation of an aborted stream?

One chain of exactly 10 calls (each small probe is the "after" reading of one step and the
"before" reading of the next):

  1  probe    max_completion_tokens=1          baseline remaining tokens
  2  abort    stream, max_completion_tokens=400, read 3 content deltas, close the connection
  3  probe    fired at once                    remaining after abort 1
  4  probe    at once                          one probe's own cost (3 -> 4; the bucket is
                                               below its cap here, so the cap hides nothing)
  5  abort / 6 probe / 7 abort / 8 probe       trials 2 and 3
  9  control  the same long stream, read to the end (usage reported)
 10  probe                                     remaining after a completed stream

A delayed refund (outcome C) would show as a refund in a later step of the chain (each step
compares the reading with refill-only accounting); the chain covers a few seconds only.

Every reading is x-ratelimit-remaining-tokens from that call's own response headers (the
state after the call's own reservation). Refill between two readings is estimated from the
headers themselves: rate = (limit - remaining) / reset_tokens_s.

Modes:
  default (dry run)  an in-process calibrated stand-in on 127.0.0.1; 0 real calls
  --live             Groq; needs GROQ_API_KEY in .env and --max-real-calls >= 10

Safety: hard call cap; stop on any 429, on remaining tokens < 2,000 or remaining requests
< 100; each abort uses its own client so its connection really closes; only rate-limit and a
few non-identifying headers are kept; the key is never printed or written.

Run: python scripts/experiments/groq_refund_probe.py                       # dry run (stand-in)
     python scripts/experiments/groq_refund_probe.py --live --max-real-calls 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relay.providers.headers import parse_duration_s

MODEL = "qwen/qwen3.8-27b"
ABORT_MAX_TOKENS = 400
ABORT_AFTER_DELTAS = 3
CHAIN_CALLS = 10
MIN_REMAINING_TOKENS = 2_000
MIN_REMAINING_REQUESTS = 100
KEEP_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens", "retry-after",
                "x-groq-region", "date", "content-type")
PROBE_MESSAGES = [{"role": "user", "content": "Reply with the single word OK."}]
LONG_MESSAGES = [{"role": "user", "content":
                  "Write a long, detailed essay (at least 600 words) about the history of "
                  "bridge engineering, from stone arches to modern suspension bridges."}]
# Dry run only: the stand-in answers from passages in a RAG-style prompt (a plain prompt
# gets "OK"), so this one makes it stream a multi-sentence answer it can be cut off in.
_BRIDGE = ("Stone arch bridges carried roads across rivers for centuries. Roman engineers "
           "built arch bridges with cut stone and concrete. Iron bridges appeared during the "
           "industrial revolution. Steel made long suspension bridges possible. Suspension "
           "bridges hang the deck from cables between towers. Modern bridge engineering uses "
           "computer models of wind and load.")
STANDIN_LONG_MESSAGES = [{"role": "user", "content":
                          f"Passages:\n\n[P1] Bridges, p. 1\n{_BRIDGE}\n\nQuestion: How did "
                          "bridge engineering develop from stone arch bridges to suspension "
                          "bridges?\n\nAnswer using only the passages above."}]


class StopProbe(RuntimeError):
    """A safety rule stopped the chain (reported, never retried)."""


@dataclass
class CallRecord:
    index: int
    kind: str  # probe | abort | control
    sent_at: str
    t_send_s: float
    t_headers_s: float | None = None
    t_end_s: float | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    remaining_tokens: int | None = None
    limit_tokens: int | None = None
    reset_tokens_s: float | None = None
    remaining_requests: int | None = None
    content_deltas: int = 0
    content_chars: int = 0
    aborted: bool = False
    usage: dict[str, Any] | None = None
    error: str | None = None


class Probe:
    def __init__(self, base_url: str, auth: str | None, model: str, cap: int,
                 long_messages: list[dict[str, str]] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.long_messages = long_messages or LONG_MESSAGES
        self._auth = auth  # never logged
        self.model = model
        self.cap = cap
        self.calls = 0
        self.records: list[CallRecord] = []
        self.started = time.perf_counter()
        self._shared: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._auth}"} if self._auth else {}

    def _now(self) -> float:
        return round(time.perf_counter() - self.started, 4)

    def _take(self, kind: str) -> CallRecord:
        if self.calls >= self.cap:
            raise StopProbe(f"call cap reached ({self.cap})")
        self.calls += 1
        record = CallRecord(index=self.calls, kind=kind,
                            sent_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                            t_send_s=self._now())
        self.records.append(record)
        return record

    def _read_headers(self, record: CallRecord, response: httpx.Response) -> None:
        record.t_headers_s = self._now()
        record.status = response.status_code
        record.headers = {k: response.headers[k] for k in KEEP_HEADERS if k in response.headers}
        h = record.headers
        record.remaining_tokens = _int(h.get("x-ratelimit-remaining-tokens"))
        record.limit_tokens = _int(h.get("x-ratelimit-limit-tokens"))
        record.remaining_requests = _int(h.get("x-ratelimit-remaining-requests"))
        reset = h.get("x-ratelimit-reset-tokens")
        record.reset_tokens_s = parse_duration_s(reset) if reset else None
        if response.status_code == 429:
            raise StopProbe("the provider returned 429: stopping (never retried)")
        if response.status_code >= 400:
            raise StopProbe(f"HTTP {response.status_code}: stopping")
        if record.remaining_tokens is not None and record.remaining_tokens < MIN_REMAINING_TOKENS:
            raise StopProbe(f"remaining tokens {record.remaining_tokens} < {MIN_REMAINING_TOKENS}")
        if record.remaining_requests is not None \
                and record.remaining_requests < MIN_REMAINING_REQUESTS:
            raise StopProbe(f"remaining requests {record.remaining_requests} < "
                            f"{MIN_REMAINING_REQUESTS}")

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            self._shared = client
            yield
        self._shared = None

    async def probe(self) -> CallRecord:
        record = self._take("probe")
        assert self._shared is not None
        response = await self._shared.post(f"{self.base_url}/chat/completions", json={
            "model": self.model, "messages": PROBE_MESSAGES, "max_completion_tokens": 1,
            "temperature": 0.0, "stream": False}, headers=self._headers())
        self._read_headers(record, response)
        record.t_end_s = self._now()
        body = response.json()
        record.usage = body.get("usage")
        return record

    async def long_stream(self, *, abort: bool) -> CallRecord:
        record = self._take("abort" if abort else "control")
        payload = {"model": self.model, "messages": self.long_messages,
                   "max_completion_tokens": ABORT_MAX_TOKENS, "temperature": 0.0,
                   "stream": True, "stream_options": {"include_usage": True}}
        # its own client: leaving it closes the connection, so the provider sees the abort
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:  # noqa: SIM117
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload,
                                     headers=self._headers()) as response:
                self._read_headers(record, response)
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[6:])
                    if chunk.get("usage"):
                        record.usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content") or ""
                        if text:
                            record.content_deltas += 1
                            record.content_chars += len(text)
                    if abort and record.content_deltas >= ABORT_AFTER_DELTAS:
                        record.aborted = True
                        break
        record.t_end_s = self._now()  # for an abort: the moment the connection was closed
        return record


def _int(value: str | None) -> int | None:
    try:
        return None if value is None else int(float(value))
    except ValueError:
        return None


async def run_chain(probe: Probe) -> None:
    async with probe.shared():
        await probe.probe()                       # 1 baseline
        for trial in range(3):                    # 2..8 three abort trials
            await probe.long_stream(abort=True)
            await probe.probe()
            if trial == 0:
                await probe.probe()               # 4 probe-cost calibration (3 -> 4)
        await probe.long_stream(abort=False)      # 9 control: completed stream
        await probe.probe()                       # 10


# --- analysis ------------------------------------------------------------------------------


def refill_rate(records: list[CallRecord]) -> float | None:
    """Tokens per second, from headers: (limit - remaining) / reset_tokens_s."""
    rates = [(r.limit_tokens - r.remaining_tokens) / r.reset_tokens_s for r in records
             if r.limit_tokens is not None and r.remaining_tokens is not None
             and r.reset_tokens_s and r.limit_tokens > r.remaining_tokens]
    return round(statistics.median(rates), 3) if rates else None


def _drop(rate: float | None, a: CallRecord, b: CallRecord) -> float:
    """How far b's reading is below a's, refill-only accounting: a's remaining refilled at
    ``rate`` for the time between the two readings (capped at the limit), minus b's."""
    assert a.remaining_tokens is not None and b.remaining_tokens is not None
    refilled = 0.0
    if rate is not None and a.t_headers_s is not None and b.t_headers_s is not None:
        refilled = rate * (b.t_headers_s - a.t_headers_s)
    predicted = a.remaining_tokens + refilled
    if b.limit_tokens is not None:
        predicted = min(float(b.limit_tokens), predicted)
    return predicted - b.remaining_tokens


def analyse(records: list[CallRecord]) -> dict[str, Any]:
    rate = refill_rate(records)
    probe_cost = None
    if len(records) >= 4 and records[2].kind == records[3].kind == "probe" \
            and None not in (records[2].remaining_tokens, records[3].remaining_tokens):
        probe_cost = round(_drop(rate, records[2], records[3]), 1)  # 3 -> 4, nothing between
    steps = []
    for i, rec in enumerate(records):
        if rec.kind not in ("abort", "control") or i == 0 or i + 1 >= len(records):
            continue
        before, after = records[i - 1], records[i + 1]
        if None in (before.remaining_tokens, rec.remaining_tokens, after.remaining_tokens):
            continue
        reserved = _drop(rate, before, rec)
        # what the next reading moved by, beyond refill: the probe's own reservation, plus
        # any further charge (positive) or refund (negative) for the long call
        post = _drop(rate, rec, after)
        change = None if probe_cost is None else post - probe_cost
        steps.append({
            "index": rec.index, "kind": rec.kind, "aborted": rec.aborted,
            "content_deltas": rec.content_deltas, "usage": rec.usage,
            "reserved_at_start": round(reserved, 1),
            "post_delta": round(post, 1),
            "settlement_change": None if change is None else round(change, 1),
            "abort_to_next_headers_ms": None if rec.t_end_s is None or after.t_headers_s is None
            else round((after.t_headers_s - rec.t_end_s) * 1000, 1),
        })
    window = None
    if records and records[0].t_headers_s is not None and records[-1].t_headers_s is not None:
        window = round(records[-1].t_headers_s - records[0].t_headers_s, 2)
    return {"refill_rate_tokens_per_s": rate, "probe_cost_estimate": probe_cost,
            "chain_window_s": window, "steps": steps, "outcome": classify(steps)}


def classify(steps: list[dict[str, Any]]) -> dict[str, Any]:
    aborts = [s for s in steps if s["kind"] == "abort" and s["settlement_change"] is not None]
    control = next((s for s in steps if s["kind"] == "control"), None)
    if not aborts:
        return {"label": "undetermined", "why": "no complete abort step"}
    unused = ABORT_MAX_TOKENS - ABORT_AFTER_DELTAS
    refunds = [-s["settlement_change"] for s in aborts]
    mean_refund = statistics.fmean(refunds)
    label = ("B: refund at abort" if mean_refund > 0.5 * unused else
             "A-or-C: no refund at abort time" if abs(mean_refund) < 0.25 * unused else
             "partial / unclear")
    return {"label": label, "refund_per_abort": [round(r, 1) for r in refunds],
            "unused_reservation_per_abort_approx": unused,
            "control_settlement_change": None if control is None
            else control["settlement_change"]}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-real-calls", type=int, default=0)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    root = Path("runs") / (f"f03-groq-refund-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
                           if args.live else
                           f"f03-standin-dryrun-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    if args.live:
        if args.max_real_calls < CHAIN_CALLS:
            print(f"refused: --live needs --max-real-calls >= {CHAIN_CALLS}")
            return 2
        from relay.config import load_settings
        settings = load_settings(".env")
        if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value().strip():
            print("refused: no GROQ_API_KEY configured")
            return 2
        probe = Probe(settings.groq_base_url, settings.groq_api_key.get_secret_value(),
                      args.model, cap=min(args.max_real_calls, CHAIN_CALLS))
        stopped = await _run(probe)
        source = "groq (real)"
    else:
        from relay.standin.server import create_standin_app, load_profile, serve_on_loopback
        profile = load_profile(Path(
            "evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json"))
        async with serve_on_loopback(create_standin_app(profile)) as url:
            probe = Probe(url, None, args.model, cap=CHAIN_CALLS,
                          long_messages=STANDIN_LONG_MESSAGES)
            stopped = await _run(probe)
        source = "stand-in (dry run, 0 real calls)"
    root.mkdir(parents=True, exist_ok=False)
    analysis = analyse(probe.records)
    manifest = {"kind": "f03-refund-probe", "source": source, "model": args.model,
                "real_calls": probe.calls if args.live else 0, "call_cap": probe.cap,
                "abort_max_tokens": ABORT_MAX_TOKENS, "abort_after_deltas": ABORT_AFTER_DELTAS,
                "stopped": stopped,
                "git": subprocess.run(["git", "rev-parse", "--short", "HEAD"], text=True,
                                      capture_output=True, check=False).stdout.strip(),
                "finished_at": datetime.now(UTC).isoformat()}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with (root / "calls.jsonl").open("w", encoding="utf-8") as handle:
        for record in probe.records:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    (root / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    for r in probe.records:
        print(f"{r.index:2d} {r.kind:7s} status {r.status} remaining {r.remaining_tokens} "
              f"limit {r.limit_tokens} reset {r.reset_tokens_s} deltas {r.content_deltas} "
              f"aborted {r.aborted} usage {r.usage} t {r.t_headers_s}->{r.t_end_s}")
    print(json.dumps(analysis, indent=1))
    print(f"calls made: {probe.calls} (real: {manifest['real_calls']}); stopped: {stopped}; "
          f"written: {root}")
    return 0


async def _run(probe: Probe) -> str | None:
    try:
        await run_chain(probe)
    except StopProbe as exc:
        return str(exc)
    except httpx.HTTPError as exc:
        return f"transport error: {type(exc).__name__}"
    return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
