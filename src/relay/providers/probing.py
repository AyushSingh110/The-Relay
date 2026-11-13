"""Groq probe planning, safeguards, execution and evidence recording."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.config import Settings
from relay.providers.base import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    ContentDelta,
    RealCallBudget,
    ReasoningDelta,
    ResponseOpened,
    StreamCompleted,
    TokenUsage,
    UsageReported,
)
from relay.providers.errors import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from relay.providers.headers import HEADER_SEMANTICS, ResponseMeta
from relay.redaction import redact_secrets
from relay.schemas import ERROR_CODES_BY_STATUS, RETRIEVAL_SETTING_NONE, ResultRow, Status
from relay.telemetry.run_store import RunHandle, create_run
from relay.telemetry.spans import MonotonicClock, ns_to_ms

ProbeMode = Literal["discover", "baseline", "minute-limit"]
SCENARIO_BY_MODE: Final[dict[str, str]] = {
    "discover": "probe_discover",
    "baseline": "probe_baseline",
    "minute-limit": "probe_minute_limit",
}
MAX_BASELINE_MODELS: Final = 3
# [PROPOSED] Token estimate used only when a cut-short stream reported no usage.
CONSERVATIVE_CHARS_PER_TOKEN: Final = 3.0
SUMMARY_FILE: Final = "summary.json"
CONTENT_EXCERPT_CHARS: Final = 200

_SYSTEM_PROMPT: Final = (
    "You are a latency probe. Ignore any context and reply with the single word OK."
)
# Synthetic, deterministic filler. Real paper text is not available before P03.
_FILLER_WORDS: Final = (
    "transformer", "attention", "encoder", "decoder", "layer", "token", "sequence", "model",
    "chunk", "speech", "latency", "stream", "dialogue", "hierarchical", "dynamic", "routing",
    "embedding", "position", "residual",
)


class ProbeOptions(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    mode: ProbeMode
    models: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    calls_per_model: Annotated[int, Field(ge=0)] = 3
    prompt_words: tuple[Annotated[int, Field(ge=1)], ...] = (400, 1600)
    max_completion_tokens: Annotated[int, Field(ge=1)] = 256
    reasoning_effort: str | None = None
    minute_limit_calls: Annotated[int, Field(ge=1)] | None = None
    minute_limit_max_completion_tokens: Annotated[int, Field(ge=1)] = 8

    @model_validator(mode="after")
    def _check_mode(self) -> ProbeOptions:
        if self.mode == "discover":
            return self
        if not self.models:
            raise ValueError(f"{self.mode} needs at least one --model")
        if len(set(self.models)) != len(self.models):
            raise ValueError("--model values must be distinct")
        if self.mode == "baseline" and len(self.models) > MAX_BASELINE_MODELS:
            raise ValueError(f"baseline accepts at most {MAX_BASELINE_MODELS} models")
        if self.mode == "minute-limit":
            if len(self.models) != 1:
                raise ValueError("minute-limit probes exactly one model")
            if self.minute_limit_calls is None:
                raise ValueError("minute-limit needs an explicit --minute-limit-calls")
        return self

    @property
    def scenario(self) -> str:
        return SCENARIO_BY_MODE[self.mode]


@dataclass(frozen=True)
class PlannedCall:
    index: int
    label: str
    model: str | None
    request: ChatRequest | None  # None => GET /models
    prompt_words: int = 0


def filler_text(words: int) -> str:
    return " ".join(_FILLER_WORDS[i % len(_FILLER_WORDS)] for i in range(words))


def _probe_request(model: str, words: int, max_tokens: int, effort: str | None) -> ChatRequest:
    user = "Reply with OK." if words == 0 else f"Context:\n{filler_text(words)}\n\nReply with OK."
    return ChatRequest(
        model=model,
        messages=(
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user),
        ),
        max_completion_tokens=max_tokens,
        temperature=0.0,
        reasoning_effort=effort,
    )


def build_plan(options: ProbeOptions) -> tuple[PlannedCall, ...]:
    """Every call the run may make, in order. Its length is checked against the cap."""
    if options.mode == "discover":
        return (PlannedCall(index=1, label="list_models", model=None, request=None),)
    calls: list[PlannedCall] = []
    if options.mode == "minute-limit":
        model = options.models[0]
        for _ in range(options.minute_limit_calls or 0):
            request = _probe_request(
                model, 0, options.minute_limit_max_completion_tokens, options.reasoning_effort
            )
            calls.append(PlannedCall(len(calls) + 1, "tiny", model, request))
        return tuple(calls)
    for model in options.models:
        for _ in range(options.calls_per_model):
            request = _probe_request(
                model, 0, options.max_completion_tokens, options.reasoning_effort
            )
            calls.append(PlannedCall(len(calls) + 1, "short", model, request))
        for words in options.prompt_words:
            request = _probe_request(
                model, words, options.max_completion_tokens, options.reasoning_effort
            )
            calls.append(PlannedCall(len(calls) + 1, f"prompt_{words}w", model, request, words))
    return tuple(calls)


def describe_plan(plan: Sequence[PlannedCall]) -> list[dict[str, Any]]:
    return [
        {
            "index": call.index,
            "label": call.label,
            "model": call.model,
            "endpoint": "GET /models" if call.request is None else "POST /chat/completions",
            "prompt_words": call.prompt_words,
            "prompt_chars": None if call.request is None else call.request.prompt_chars,
            "max_completion_tokens": (
                None if call.request is None else call.request.max_completion_tokens
            ),
        }
        for call in plan
    ]


def live_preconditions(
    options: ProbeOptions,
    plan: Sequence[PlannedCall],
    *,
    max_real_calls: int,
    settings: Settings,
    owner_approved_rate_limit_probe: bool,
) -> list[str]:
    """Reasons a live run must be refused. Empty list => every safeguard is satisfied."""
    problems: list[str] = []
    if options.mode == "minute-limit" and not owner_approved_rate_limit_probe:
        problems.append(
            "minute-limit deliberately approaches a rate limit and needs "
            "--owner-approved-rate-limit-probe (separate owner approval)"
        )
    if max_real_calls < 1:
        problems.append("--max-real-calls must be at least 1 for a live run")
    if len(plan) > max_real_calls:
        problems.append(f"plan needs {len(plan)} calls but --max-real-calls is {max_real_calls}")
    if settings.max_real_calls < max_real_calls:
        problems.append(
            f"MAX_REAL_CALLS in the environment/.env is {settings.max_real_calls}; it must be "
            f">= --max-real-calls ({max_real_calls}) for a live run"
        )
    if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value().strip():
        problems.append("GROQ_API_KEY is not configured")
    return problems


# --- Execution ----------------------------------------------------------------------


class ProbeOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    run_dir: str
    planned_calls: int
    attempted_calls: int
    real_calls_sent: int
    rows_written: int
    stopped_reason: str | None


@dataclass
class _Observation:
    """What the probe (as the client) saw during one streamed call."""

    t0_ns: int
    opened: ResponseOpened | None = None
    first_reasoning_ns: int | None = None
    first_content_ns: int | None = None
    completed: StreamCompleted | None = None
    usage: TokenUsage | None = None
    content: list[str] = field(default_factory=list)
    reasoning_chars: int = 0
    end_ns: int = 0
    error: ProviderError | None = None

    def ms(self, at_ns: int | None) -> float | None:
        return None if at_ns is None else ns_to_ms(at_ns - self.t0_ns)


def status_for_error_code(error_code: str) -> Status:
    for status, codes in ERROR_CODES_BY_STATUS.items():
        if error_code in codes:
            return status  # type: ignore[return-value]
    raise ValueError(f"error code {error_code!r} has no status")


def _meta_record(meta: ResponseMeta | None) -> dict[str, Any] | None:
    if meta is None:
        return None
    return {
        "status_code": meta.status_code,
        "provider_request_id": meta.provider_request_id,
        "headers": meta.headers,
        "rate_limits": meta.rate_limits.model_dump(mode="json"),
    }


def _error_record(error: ProviderError | None) -> dict[str, Any] | None:
    if error is None:
        return None
    record: dict[str, Any] = {
        "class": type(error).__name__,
        "status_code": error.status_code,
        "error_code": error.error_code,
        "retryable_hint": error.retryable,
        "provider_error_type": error.provider_error_type,
        "provider_error_code": error.provider_error_code,
        "body_excerpt": error.body_excerpt,
    }
    if isinstance(error, ProviderRateLimitError):
        record.update(
            scope=error.scope,
            basis=error.basis,
            metric=error.metric,
            retry_after_s=error.retry_after_s,
            detail=error.detail,
        )
    if isinstance(error, ProviderTimeoutError):
        record["phase"] = error.phase
    if isinstance(error, ProviderConnectionError):
        record["reason"] = error.reason
    return record


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CONSERVATIVE_CHARS_PER_TOKEN)


@dataclass
class _ProbeContext:
    provider: ChatProvider
    run: RunHandle
    budget: RealCallBudget
    safety_fraction: float
    clock: MonotonicClock
    wall_clock: Callable[[], datetime]
    records: list[dict[str, Any]] = field(default_factory=list)
    rows_written: int = 0


async def _observe_stream(ctx: _ProbeContext, request: ChatRequest) -> _Observation:
    seen = _Observation(t0_ns=ctx.clock())
    try:
        async for event in ctx.provider.stream_chat(request):
            if isinstance(event, ResponseOpened):
                seen.opened = event
            elif isinstance(event, ReasoningDelta):
                if seen.first_reasoning_ns is None:
                    seen.first_reasoning_ns = event.at_ns
                seen.reasoning_chars += len(event.text)
            elif isinstance(event, ContentDelta):
                if seen.first_content_ns is None:
                    seen.first_content_ns = event.at_ns
                seen.content.append(event.text)
            elif isinstance(event, UsageReported):
                seen.usage = event.usage
            elif isinstance(event, StreamCompleted):
                seen.completed = event
    except ProviderError as exc:
        seen.error = exc
    seen.end_ns = ctx.clock()
    return seen


def _first_token(seen: _Observation) -> tuple[int | None, str | None]:
    """Probe rows: first content delta, else first reasoning delta, else completion."""
    if seen.first_content_ns is not None:
        return seen.first_content_ns, "content"
    if seen.first_reasoning_ns is not None:
        return seen.first_reasoning_ns, "reasoning"
    if seen.completed is not None:
        return seen.completed.at_ns, "completion"
    return None, None


def _token_accounting(
    seen: _Observation, request: ChatRequest
) -> tuple[int, int, Literal["provider", "estimated", "none"]]:
    if seen.usage is not None:
        return seen.usage.prompt_tokens, seen.usage.completion_tokens, "provider"
    if seen.opened is None:
        # Rejected before a stream opened (4xx/429/5xx/connection failure): nothing to count.
        return 0, 0, "none"
    out_chars = sum(len(part) for part in seen.content) + seen.reasoning_chars
    return _estimate_tokens(request.prompt_chars), _estimate_tokens(out_chars), "estimated"


async def _run_inference(ctx: _ProbeContext, call: PlannedCall, scenario: str) -> dict[str, Any]:
    if call.request is None or call.model is None:
        raise ValueError("inference probe call needs a model and a request")
    request_id = ctx.run.new_request_id()
    start_time = ctx.wall_clock()
    seen = await _observe_stream(ctx, call.request)
    error = seen.error
    first_ns, first_basis = _first_token(seen)
    if error is None:
        status: Status = "ok"
        error_code = None
    else:
        error_code = error.error_code
        status = status_for_error_code(error_code)
    tokens_in, tokens_out, usage_source = _token_accounting(seen, call.request)
    total_ms = ns_to_ms(seen.end_ns - seen.t0_ns)
    first_token_ms = seen.ms(first_ns) if status == "ok" else None
    row = ResultRow(
        request_id=request_id,
        scenario=scenario,
        source=ctx.provider.source,
        start_time=start_time,
        first_token_ms=None if first_token_ms is None else min(first_token_ms, total_ms),
        total_ms=total_ms,
        status=status,
        retries=0,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        retrieval_setting=RETRIEVAL_SETTING_NONE,
        answer_grade=None,
        run_id=ctx.run.run_id,
        question_id=None,
        provider_first_token_ms=seen.ms(seen.first_content_ns),
        server_total_ms=None,
        queue_depth_at_entry=None,
        waiting_ms=None,
        working_ms=None,
        retrying_ms=None,
        retrieval_ms=None,
        cache_status="off",
        provider_called=True,
        degraded=False,
        model_id=call.model,
        usage_source=usage_source,
        cost_usd=None,
        verification_verdict="NOT_RUN",
        error_code=error_code,  # type: ignore[arg-type]
    )
    ctx.run.append_row(row)
    ctx.rows_written += 1
    meta = seen.opened.meta if seen.opened is not None else (error.meta if error else None)
    content = "".join(seen.content)
    return {
        "record": "inference",
        "request_id": request_id,
        "call_index": call.index,
        "label": call.label,
        "model": call.model,
        "prompt_words": call.prompt_words,
        "prompt_chars": call.request.prompt_chars,
        "max_completion_tokens": call.request.max_completion_tokens,
        "reasoning_effort": call.request.reasoning_effort,
        "start_time": start_time.isoformat(),
        "status": status,
        "error_code": error_code,
        "timings_ms": {
            "headers": None if seen.opened is None else seen.ms(seen.opened.at_ns),
            "first_reasoning": seen.ms(seen.first_reasoning_ns),
            "first_content": seen.ms(seen.first_content_ns),
            "total": total_ms,
        },
        "first_token_basis": first_basis,
        "usage": None if seen.usage is None else seen.usage.model_dump(mode="json"),
        "usage_source": usage_source,
        "prompt_tokens_per_char": (
            None
            if seen.usage is None
            else round(seen.usage.prompt_tokens / call.request.prompt_chars, 6)
        ),
        "content_chars": len(content),
        "content_excerpt": content[:CONTENT_EXCERPT_CHARS],
        "reasoning_chars": seen.reasoning_chars,
        "finish_reason": None if seen.completed is None else seen.completed.finish_reason,
        "response_model": None if seen.completed is None else seen.completed.model,
        "response_id": None if seen.completed is None else seen.completed.response_id,
        "response": _meta_record(meta),
        "error": _error_record(error),
    }


async def _run_discover(ctx: _ProbeContext, call: PlannedCall) -> dict[str, Any]:
    t0 = ctx.clock()
    started = ctx.wall_clock()
    try:
        catalog = await ctx.provider.list_models()
    except ProviderError as exc:
        return {
            "record": "models",
            "call_index": call.index,
            "start_time": started.isoformat(),
            "status": status_for_error_code(exc.error_code),
            "timings_ms": {"total": ns_to_ms(ctx.clock() - t0)},
            "response": _meta_record(exc.meta),
            "error": _error_record(exc),
        }
    return {
        "record": "models",
        "call_index": call.index,
        "start_time": started.isoformat(),
        "status": "ok",
        "timings_ms": {"total": ns_to_ms(ctx.clock() - t0)},
        "model_ids": list(catalog.ids),
        "models": [model.model_dump(mode="json") for model in catalog.models],
        "metadata_issues": list(catalog.metadata_issues),
        "response": _meta_record(catalog.meta),
        "error": None,
    }


def _stop_reason(mode: ProbeMode, record: dict[str, Any], fraction: float) -> str | None:
    error = record.get("error")
    if error is not None:
        if error["class"] == ProviderAuthError.__name__:
            return "credentials rejected; stopping"
        if error["class"] == ProviderRateLimitError.__name__:
            if error.get("scope") == "daily":
                return "daily-scope rate limit observed; stopping (never drain the daily quota)"
            if mode == "minute-limit":
                return "rate limit observed; minute-limit probe stops at the first 429"
            return "unexpected rate limit during a non-limit probe; stopping"
    response = record.get("response")
    if response is None:
        return None
    limits = response["rate_limits"]
    limit, remaining = limits.get("limit_requests"), limits.get("remaining_requests")
    if isinstance(limit, int) and isinstance(remaining, int) and limit > 0:
        used_fraction = (limit - remaining) / limit
        if used_fraction >= fraction:
            return (
                f"daily safety fraction reached: x-ratelimit-*-requests (documented RPD, "
                f"{HEADER_SEMANTICS}) shows {limit - remaining}/{limit} used >= {fraction}"
            )
    return None


def _stats(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "min": min(values),
        "median": round(statistics.median(values), 3),
        "max": max(values),
    }


def build_summary(
    records: Sequence[dict[str, Any]],
    *,
    run_id: str,
    mode: ProbeMode,
    planned_calls: int,
    budget: RealCallBudget,
    stopped_reason: str | None,
) -> dict[str, Any]:
    """Per-model descriptive statistics computed only from this run's records."""
    per_model: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["record"] != "inference":
            continue
        entry = per_model.setdefault(
            record["model"],
            {"calls": 0, "ok": 0, "status_counts": {}, "_first": [], "_total": [], "_ratio": []},
        )
        entry["calls"] += 1
        counts = entry["status_counts"]
        counts[record["status"]] = counts.get(record["status"], 0) + 1
        if record["status"] == "ok":
            entry["ok"] += 1
            entry["_total"].append(record["timings_ms"]["total"])
            if record["timings_ms"]["first_content"] is not None:
                entry["_first"].append(record["timings_ms"]["first_content"])
        if record["prompt_tokens_per_char"] is not None:
            entry["_ratio"].append(record["prompt_tokens_per_char"])
    for entry in per_model.values():
        entry["provider_first_content_ms"] = _stats(entry.pop("_first"))
        entry["total_ms_ok"] = _stats(entry.pop("_total"))
        entry["prompt_tokens_per_char"] = _stats(entry.pop("_ratio"))
    last_limits = next(
        (r["response"]["rate_limits"] for r in reversed(records) if r.get("response")), None
    )
    return {
        "run_id": run_id,
        "mode": mode,
        "planned_calls": planned_calls,
        "attempted_calls": len(records),
        "real_calls_sent": budget.used,
        "max_real_calls": budget.max_calls,
        "stopped_reason": stopped_reason,
        "header_semantics": HEADER_SEMANTICS,
        "per_model": dict(sorted(per_model.items())),
        "last_rate_limit_snapshot": last_limits,
        "note": "Computed only from calls recorded in this run's traces.jsonl.",
    }


async def execute_plan(
    plan: Sequence[PlannedCall], options: ProbeOptions, ctx: _ProbeContext
) -> str | None:
    """Run the plan sequentially. Returns the reason the run stopped early, if any."""
    for call in plan:
        if ctx.budget.remaining <= 0:
            return "real-call cap reached"
        if call.request is None:
            record = await _run_discover(ctx, call)
        else:
            record = await _run_inference(ctx, call, options.scenario)
        ctx.records.append(record)
        ctx.run.append_trace(record)
        reason = _stop_reason(options.mode, record, ctx.safety_fraction)
        if reason is not None:
            return reason
    return None


ProviderFactory = Callable[[RealCallBudget], ChatProvider]


async def run_probe(
    options: ProbeOptions,
    *,
    settings: Settings,
    max_real_calls: int,
    evidence_root: Path,
    provider_factory: ProviderFactory,
    git_state: tuple[str, bool] | None = None,
    repo_root: Path | None = None,
    clock: MonotonicClock = time.perf_counter_ns,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ProbeOutcome:
    """Execute a probe plan into a new ``groq_probe`` run directory."""
    plan = build_plan(options)
    single_model = options.models[0] if len(options.models) == 1 else None
    reasoning = None if options.reasoning_effort is None else {
        "reasoning_effort": options.reasoning_effort
    }
    with create_run(
        evidence_root,
        "groq_probe",
        scenarios=(options.scenario,),
        source="real",
        model_id=single_model,
        reasoning_settings=reasoning,
        retrieval_settings=(RETRIEVAL_SETTING_NONE,),
        config_snapshot={
            "settings": settings.manifest_snapshot(),
            "probe_options": options.model_dump(mode="json"),
            "max_real_calls": max_real_calls,
            "planned_calls": len(plan),
        },
        notes=f"probe mode={options.mode}",
        git_state=git_state,
        repo_root=repo_root,
    ) as run:
        budget = RealCallBudget(max_real_calls, on_consume=run.record_real_calls)
        provider = provider_factory(budget)
        ctx = _ProbeContext(
            provider=provider,
            run=run,
            budget=budget,
            safety_fraction=settings.daily_safety_fraction,
            clock=clock,
            wall_clock=wall_clock,
        )
        try:
            stopped = await execute_plan(plan, options, ctx)
        finally:
            await provider.aclose()
        summary = build_summary(
            ctx.records,
            run_id=run.run_id,
            mode=options.mode,
            planned_calls=len(plan),
            budget=budget,
            stopped_reason=stopped,
        )
        (run.run_dir / SUMMARY_FILE).write_text(
            json.dumps(redact_secrets(summary), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        run.finalize("complete", notes=f"probe mode={options.mode}; stopped: {stopped or 'no'}")
        return ProbeOutcome(
            run_id=run.run_id,
            run_dir=str(run.run_dir),
            planned_calls=len(plan),
            attempted_calls=len(ctx.records),
            real_calls_sent=budget.used,
            rows_written=ctx.rows_written,
            stopped_reason=stopped,
        )


__all__ = [
    "MAX_BASELINE_MODELS",
    "SCENARIO_BY_MODE",
    "PlannedCall",
    "ProbeOptions",
    "ProbeOutcome",
    "build_plan",
    "build_summary",
    "describe_plan",
    "filler_text",
    "live_preconditions",
    "run_probe",
    "status_for_error_code",
]
