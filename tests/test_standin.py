"""P09 tests: the stand-in model service, its profile fit and the calibration script.

The stand-in runs under a real uvicorn server on 127.0.0.1 and is driven through
``HttpOpenAIProvider``, the same client the Relay uses for Groq. Rate-limit windows use a
fake clock the test moves; latency uses real (short) sleeps. Every limit and latency here
is an explicit test input, not a Groq measurement.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from relay.orchestrator.pipeline import (
    Pipeline,
    PipelineConfig,
    PipelineRequest,
    SingleCallGenerator,
)
from relay.orchestrator.prompt import build_request
from relay.orchestrator.tools import ToolGateway
from relay.orchestrator.verifier import TAG_PATTERN, split_sentences, verify
from relay.providers.base import ChatMessage, ChatRequest, CompletionResult, collect_stream
from relay.providers.errors import (
    ProviderBadRequestError,
    ProviderPayloadTooLargeError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderStreamIncompleteError,
    ProviderTimeoutError,
)
from relay.providers.http_provider import HttpOpenAIProvider, ProviderTimeouts
from relay.standin.profile import (
    FaultModel,
    LatencyModel,
    Limits,
    LoadModel,
    ModelProfile,
    StandinProfile,
    TokenModel,
    calls_from_traces,
    fit_profile,
    header_checks,
    prompt_tokens,
    quantiles,
    request_rng,
    sample_header_delay_ms,
    uncalibrated_defaults,
)
from relay.standin.server import (
    StandinState,
    create_standin_app,
    extractive_answer,
    format_duration,
    parse_rag_prompt,
    serve_on_loopback,
)
from relay.telemetry.spans import ServerSpanTracker
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever, passage


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "calibrate_standin.py"
    spec = importlib.util.spec_from_file_location("calibrate_standin_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calibrate_standin = _load_script()
MODEL = "test-model"
QUESTION = "What is d_model in the encoder?"


class FakeClock:
    """Seconds, monotonic, moved only by the test (drives the rate-limit buckets)."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def profile(*, header_ms: float = 5.0, inter_token_ms: float = 0.2, reasoning: int = 3,
            limits: Limits | None = None, load: LoadModel | None = None,
            faults: FaultModel | None = None, sigma: float = 0.0) -> StandinProfile:
    return StandinProfile(
        label="uncalibrated_defaults", seed=7,
        models={MODEL: ModelProfile(
            latency=LatencyModel(header_median_ms=header_ms, header_sigma=sigma,
                                 prompt_ms_per_1k_tokens=0.0, inter_token_ms=inter_token_ms,
                                 reasoning_tokens=reasoning),
            tokens=TokenModel(base_prompt_tokens=10, prompt_chars_per_token=4.0,
                              output_chars_per_token=4.0))},
        limits=limits or Limits(), load=load or LoadModel(knee_concurrency=64),
        faults=faults or FaultModel(),
    )


@asynccontextmanager
async def standin(prof: StandinProfile, clock: FakeClock | None = None,
                  timeouts: ProviderTimeouts | None = None,
                  ) -> AsyncGenerator[tuple[HttpOpenAIProvider, StandinState, str]]:
    app = create_standin_app(prof, clock=clock or FakeClock())
    async with serve_on_loopback(app) as base_url:
        provider = HttpOpenAIProvider(base_url=base_url, source="stand-in", timeouts=timeouts)
        try:
            yield provider, app.state.standin, base_url
        finally:
            await provider.aclose()


def rag_request(max_tokens: int = 200, question: str = QUESTION) -> ChatRequest:
    return build_request(question, PASSAGES, model=MODEL, max_completion_tokens=max_tokens)


def short_request(max_tokens: int = 20, chars: int = 40) -> ChatRequest:
    return ChatRequest(model=MODEL, max_completion_tokens=max_tokens,
                       messages=(ChatMessage(role="user", content="x" * chars),))


async def complete(provider: HttpOpenAIProvider, request: ChatRequest) -> CompletionResult:
    return await collect_stream(provider.stream_chat(request))


async def until(condition: Any, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition was never reached"
        await asyncio.sleep(0.005)


# --- Streaming, usage, answers ------------------------------------------------------


async def test_stream_delivers_reasoning_then_cited_content_and_usage() -> None:
    request = rag_request()
    async with standin(profile(limits=Limits(tpm=100_000, rpd=1_000))) as (provider, state, _):
        result = await complete(provider, request)
    assert result.streamed and result.finish_reason == "stop" and result.model == MODEL
    assert result.reasoning == "thinking " * 3
    assert "[P1]" in result.content and result.content.endswith(".")
    assert verify(result.content, PASSAGES).verdict == "VERIFIED"
    assert result.first_reasoning_ns is not None and result.first_content_ns is not None
    assert result.first_reasoning_ns < result.first_content_ns < result.finished_ns

    usage = result.usage
    assert usage is not None and usage.location == "usage"
    model = state.profile.models[MODEL]
    assert usage.prompt_tokens == prompt_tokens(model, request.prompt_chars)
    assert usage.reasoning_tokens == 3
    assert usage.completion_tokens > 3
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens

    limits = result.rate_limits
    assert (limits.limit_requests, limits.remaining_requests) == (1_000, 999)
    assert limits.reset_requests_s == pytest.approx(86.4)
    assert limits.limit_tokens == 100_000
    charged = usage.prompt_tokens + request.max_completion_tokens
    assert limits.remaining_tokens == 100_000 - charged  # reserved when admitted
    assert limits.reset_tokens_s == pytest.approx(charged * 60 / 100_000, abs=0.002)
    assert set(limits.raw) == {f"x-ratelimit-{a}-{b}" for a in ("limit", "remaining", "reset")
                               for b in ("requests", "tokens")}
    stats = state.stats.as_dict()
    assert (stats["requests"], stats["completed_streams"], stats["by_status"]) == (
        1, 1, {"200": 1})
    # After the stream the unused reservation is given back.
    used = usage.prompt_tokens + usage.completion_tokens
    assert state.limiter.buckets["TPM"].remaining == 100_000 - used


async def test_answers_refuse_without_overlap_and_say_ok_without_passages() -> None:
    async with standin(profile()) as (provider, _, _):
        refusal = await complete(provider, rag_request(question="Who won the football cup?"))
        plain = await complete(provider, short_request())
    assert refusal.content == "INSUFFICIENT_EVIDENCE"
    assert verify(refusal.content, PASSAGES).verdict == "REFUSAL_VERIFIED"
    assert plain.content == "OK"


def test_extractive_answer_parsing_is_deterministic() -> None:
    messages = build_request(QUESTION, PASSAGES, model=MODEL, max_completion_tokens=50).messages
    question, passages = parse_rag_prompt(messages[1].content)
    assert question == QUESTION and [tag for tag, _ in passages] == [1, 2, 3, 4]
    assert passages[0][1].startswith("The encoder is composed")
    answer = extractive_answer(list(messages))  # type: ignore[arg-type]
    assert answer == extractive_answer(list(messages))  # type: ignore[arg-type]
    assert answer.count("[P") <= 2 and "d_model" in answer


# F-01: the exact passage spans that stopped v2 overload streams as `uncited` (q08 x3, q04 x2,
# q13, q17; runs/v2-load-rerun-20261120T084043Z/overload). Each holds a sentence boundary the
# Relay's splitter sees but the stand-in's old regex did not ("." before a bullet, a digit or
# another "."), so the stand-in copied two sentences under one tag at the end.
UNCITED_STOP_SPANS = (
    ("q04", "What are the three different ways multi-head attention is used in the Transformer "
            "architecture? Explain the role of each.",
     "Each position in the encoder can attend to all positions in the previous layer of the "
     "encoder. • Similarly, self-attention layers in the decoder allow each position in the "
     "decoder to attend to all positions in the decoder up to and including that position."),
    ("q08", "What is the Inner Monologue mechanism in Moshi, and how does predicting "
            "time-aligned text tokens before audio tokens improve the quality of generated "
            "speech?",
     "Section 3.4 describes this architecture. • In Section 3.4.4, we introduce Inner "
     "Monologue, a new training and inference setup for audio language models that "
     "significantly improves the factuality and linguistic quality of generated speech by "
     "predicting time-aligned text tokens before audio tokens."),
    ("q13", "In H-Net's Dynamic Chunking mechanism, what is the purpose of the routing module and "
            "the smoothing module?",
     "(d) Upsampler 1 −'_!\"$ 1 −'_!\"# 1 −'_! . '!\"% '!\"$ '!\"# '_! .̅ M (c) "
     "Smoothing Module 0\"#$ /0^\" +^\" (b) Downsampler - Chunking /0 (a) Routing Module T h e "
     "e _ E _ n d o f t o k e s _ n Dynamic Chunking Figure 1: (left) Architectural overview "
     "of."),
    ("q17", "Explain the two major stages of photosynthesis: light-dependent reactions and the "
            "Calvin cycle. Where does each stage occur?",
     "number of inputs processed at each stage and the corresponding hidden dimensions (see "
     "Appendix C). 9 With this modulation, the model achieves more stable training dynamics "
     "and improved convergence behavior across the entire hierarchy."),
)


@pytest.mark.parametrize(("qid", "question", "text"), UNCITED_STOP_SPANS,
                         ids=[case[0] for case in UNCITED_STOP_SPANS])
def test_every_sentence_the_relay_splits_from_a_standin_answer_is_cited(
        qid: str, question: str, text: str) -> None:
    passages = (passage(text),)
    messages = build_request(question, passages, model=MODEL, max_completion_tokens=200).messages
    answer = extractive_answer(list(messages))  # type: ignore[arg-type]
    assert answer != "INSUFFICIENT_EVIDENCE"
    uncited = [unit for unit in split_sentences(answer) if not TAG_PATTERN.search(unit)]
    assert uncited == [], f"{qid}: the Relay would stop on {uncited[0]!r}"
    codes = {failure.code for failure in verify(answer, passages).failures}
    assert "uncited_sentence" not in codes


# F-07: an injected uncited sentence, so overload runs can include the verifier stops a real
# model causes (F-01 made the stand-in's own answers always cite).
async def test_an_injected_uncited_sentence_is_one_sentence_without_a_tag() -> None:
    async with standin(profile(faults=FaultModel(uncited_rate=1.0))) as (provider, state, _):
        result = await complete(provider, rag_request())
    units = split_sentences(result.content)
    uncited = [unit for unit in units if not TAG_PATTERN.search(unit)]
    assert len(units) >= 1 and len(uncited) == 1
    assert "uncited_sentence" in {f.code for f in verify(result.content, PASSAGES).failures}
    assert state.stats.uncited_injected == 1
    assert state.stats.as_dict()["uncited_injected"] == 1


async def test_no_uncited_sentence_is_injected_by_default() -> None:
    async with standin(profile()) as (provider, state, _):
        result = await complete(provider, rag_request())
    assert all(TAG_PATTERN.search(unit) for unit in split_sentences(result.content))
    assert state.stats.uncited_injected == 0


async def test_a_client_that_leaves_before_its_body_arrives_is_not_an_app_error() -> None:
    """F-07 run log: a client gave up while its request body was still being sent and the
    stand-in raised ``ClientDisconnect`` out of the ASGI app (an ERROR traceback)."""
    app = create_standin_app(profile(), clock=FakeClock())
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/openai/v1/chat/completions",
             "raw_path": b"/openai/v1/chat/completions", "query_string": b"",
             "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 2), "root_path": ""}
    await app(scope, receive, send)  # must not raise
    stats = app.state.standin.stats
    assert stats.client_gone == 1 and stats.by_status[499] == 1


def test_the_uncited_rate_is_a_fraction() -> None:
    assert FaultModel(uncited_rate=1.0).uncited_rate == 1.0
    with pytest.raises(ValueError, match="uncited_rate"):
        FaultModel(uncited_rate=1.5)


async def test_truncation_at_max_completion_tokens_reports_length() -> None:
    async with standin(profile(reasoning=3)) as (provider, _, _):
        result = await complete(provider, rag_request(max_tokens=6))
    assert result.finish_reason == "length"
    assert result.usage is not None and result.usage.completion_tokens <= 6


async def test_non_streaming_call_and_model_list() -> None:
    async with standin(profile()) as (provider, _, _):
        result = await provider.complete_chat(rag_request())
        catalog = await provider.list_models()
    assert not result.streamed and "[P1]" in result.content
    assert result.usage is not None and result.usage.reasoning_tokens == 3
    assert catalog.ids == (MODEL,)
    info = catalog.models[0]
    assert (info.owned_by, info.active, info.context_window) == ("relay-standin", True, 131_072)


async def test_unknown_model_and_bad_body_are_rejected() -> None:
    async with standin(profile()) as (provider, state, base_url):
        with pytest.raises(ProviderBadRequestError):
            await complete(provider, ChatRequest(
                model="nope", max_completion_tokens=5,
                messages=(ChatMessage(role="user", content="hi"),)))
        async with httpx.AsyncClient(base_url=base_url + "/") as client:
            bad = await client.post("chat/completions", content=b"{not json")
    assert bad.status_code == 400 and bad.json()["error"]["type"] == "invalid_request_error"
    assert state.stats.by_status == {404: 1, 400: 1}


async def test_the_relay_pipeline_runs_against_the_standin() -> None:
    async with standin(profile()) as (provider, _, _):
        pipeline = Pipeline(gateway=ToolGateway({"unit": StubRetriever(PASSAGES)}),
                            generator=SingleCallGenerator(provider),
                            config=PipelineConfig(model_id=MODEL, max_completion_tokens=200),
                            top_k={"unit": 4})
        spans = ServerSpanTracker()
        outcome = await pipeline.run(PipelineRequest(
            request_id="exp-20261113T080000Z-abc1234-000001", question=QUESTION,
            retrieval_setting="unit"), spans)
    assert (outcome.kind, outcome.verification_verdict, outcome.usage_source) == (
        "answer", "VERIFIED", "provider")
    assert outcome.tokens_in > 0 and spans.finish().provider_first_token_ms is not None


# --- Rate limits ---------------------------------------------------------------------


async def test_rpm_limit_returns_429_with_retry_after_then_recovers() -> None:
    clock = FakeClock()
    async with standin(profile(limits=Limits(rpm=2)), clock) as (provider, state, _):
        for _ in range(2):
            await complete(provider, short_request())
        with pytest.raises(ProviderRateLimitError) as caught:
            await complete(provider, short_request())
        clock.advance(29.0)
        with pytest.raises(ProviderRateLimitError):
            await complete(provider, short_request())  # still one second short
        clock.advance(1.0)
        recovered = await complete(provider, short_request())
    error = caught.value
    assert (error.scope, error.basis, error.metric, error.retryable) == (
        "minute", "body", "RPM", True)
    assert error.retry_after_s == 30.0 and error.meta.rate_limits.retry_after_s == 30.0
    assert error.meta.status_code == 429
    assert recovered.content == "OK"
    assert state.stats.rate_limited == {"RPM": 2}


async def test_tpm_limit_uses_the_reservation_and_recovers() -> None:
    clock = FakeClock()
    request = short_request(max_tokens=200, chars=400)  # 10 + 100 prompt + 200 reserved
    async with standin(profile(limits=Limits(tpm=500)), clock) as (provider, state, _):
        first = await complete(provider, request)
        assert first.rate_limits.remaining_tokens == 500 - 310
        assert state.limiter.buckets["TPM"].remaining == 500 - 110 - 4  # settled to usage
        await complete(provider, request)
        with pytest.raises(ProviderRateLimitError) as caught:
            await complete(provider, request)
        with pytest.raises(ProviderPayloadTooLargeError):
            await complete(provider, short_request(max_tokens=600))
        error = caught.value
        assert (error.scope, error.metric) == ("minute", "TPM")
        wait = error.retry_after_s
        assert wait is not None and wait >= 1
        assert error.detail["limit"] == 500 and error.detail["requested"] == 310
        clock.advance(wait)
        assert (await complete(provider, request)).content == "OK"


async def test_rpd_limit_is_daily_not_retryable_and_recovers_after_its_window() -> None:
    clock = FakeClock()
    async with standin(profile(limits=Limits(rpd=3)), clock) as (provider, _, _):
        resets = []
        for _ in range(3):
            resets.append((await complete(provider, short_request())).rate_limits)
        with pytest.raises(ProviderRateLimitError) as caught:
            await complete(provider, short_request())
        clock.advance(86_400 / 3)
        assert (await complete(provider, short_request())).content == "OK"
    assert [r.remaining_requests for r in resets] == [2, 1, 0]
    assert [r.reset_requests_s for r in resets] == [28_800.0, 57_600.0, 86_400.0]
    assert resets[0].raw["x-ratelimit-reset-requests"] == "8h"
    error = caught.value
    assert (error.scope, error.metric, error.error_code, error.retryable) == (
        "daily", "RPD", "daily_budget_exhausted", False)
    assert error.retry_after_s == 28_800.0


async def test_admin_reset_can_exhaust_daily_budgets() -> None:
    async with standin(profile(limits=Limits(rpd=100, tpd=10_000))) as (provider, _, base):
        admin = base.removesuffix("/openai/v1")
        async with httpx.AsyncClient(base_url=admin) as client:
            bad = await client.post("/admin/reset", json={"exhaust": ["RPM"]})
            assert bad.status_code == 422  # RPM is not enforced in this profile
            done = await client.post("/admin/reset", json={"exhaust": ["TPD"]})
            assert done.json()["limits"]["TPD"]["level"] == 0.0
            with pytest.raises(ProviderRateLimitError) as caught:
                await complete(provider, short_request())
            assert (caught.value.scope, caught.value.metric) == ("daily", "TPD")
            await client.post("/admin/reset", json={})
            assert (await complete(provider, short_request())).content == "OK"
            stats = (await client.get("/admin/stats")).json()
    assert stats["requests"] == 1 and stats["rate_limited"] == {}  # counters were reset


def test_token_bucket_headers_follow_the_observed_groq_shape() -> None:
    assert format_duration(86.4) == "1m26.4s" and format_duration(432) == "7m12s"
    assert format_duration(2.28) == "2.28s" and format_duration(0) == "0s"
    assert format_duration(28_800) == "8h"


# --- Load and faults -----------------------------------------------------------------


async def test_latency_slows_above_the_concurrency_knee() -> None:
    prof = profile(header_ms=80.0, inter_token_ms=0.0, reasoning=0,
                   load=LoadModel(knee_concurrency=1, slowdown_per_extra=1.0))
    async with standin(prof) as (provider, state, _):
        solo = await complete(provider, short_request())
        crowd = await asyncio.gather(*(complete(provider, short_request()) for _ in range(4)))
    assert state.stats.peak_active == 4
    slowest = max(r.total_ms for r in crowd)
    assert solo.total_ms < 200 and slowest >= 2.5 * 80  # 4 active, knee 1: x4 at the top
    assert slowest - min(r.total_ms for r in crowd) >= 150  # 4 x 80 vs 1-2 x 80 ms


async def test_many_short_token_gaps_add_up_to_the_profiled_stream_time() -> None:
    # 40 reasoning tokens x 2.5 ms = 100 ms. Per-token sleeps of this size are wrong by
    # up to a timer tick each on a coarse event-loop clock; pacing must not drift.
    prof = profile(header_ms=5.0, inter_token_ms=2.5, reasoning=40)
    async with standin(prof) as (provider, _, _):
        result = await complete(provider, short_request(max_tokens=60))
    assert result.first_reasoning_ns is not None and result.first_content_ns is not None
    reasoning_ms = (result.first_content_ns - result.first_reasoning_ns) / 1e6
    assert 80.0 <= reasoning_ms <= 140.0


async def test_injected_5xx_is_a_retryable_server_error_and_costs_no_quota() -> None:
    prof = profile(limits=Limits(rpd=10), faults=FaultModel(error_rate=1.0, error_status=503))
    async with standin(prof) as (provider, state, _):
        with pytest.raises(ProviderServerError) as caught:
            await complete(provider, short_request())
    assert caught.value.retryable and caught.value.meta.status_code == 503
    assert state.stats.errors_injected == 1 and state.limiter.buckets["RPD"].remaining == 10


async def test_stragglers_are_much_slower() -> None:
    fast = profile(header_ms=40.0, reasoning=0)
    slow = profile(header_ms=40.0, reasoning=0,
                   faults=FaultModel(straggler_rate=1.0, straggler_multiplier=5.0))
    async with standin(fast) as (provider, _, _):
        normal = await complete(provider, short_request())
    async with standin(slow) as (provider, state, _):
        straggler = await complete(provider, short_request())
    assert state.stats.stragglers == 1
    # The sleeps give a floor of 5 x 40 ms; connection set-up and scheduling jitter are
    # not scaled, so compare against the floor and a gap, not a ratio.
    assert straggler.total_ms >= 190 and straggler.total_ms - normal.total_ms >= 100


async def test_stalled_stream_trips_the_client_stall_timeout_and_frees_the_slot() -> None:
    prof = profile(faults=FaultModel(stall_rate=1.0, stall_after_pieces=1, stall_s=30.0))
    timeouts = ProviderTimeouts(stall_s=0.3)
    async with standin(prof, timeouts=timeouts) as (provider, state, _):
        with pytest.raises(ProviderTimeoutError) as caught:
            await complete(provider, rag_request())
        await until(lambda: state.active == 0)
    assert caught.value.phase == "stall" and state.stats.stalls == 1


async def test_stream_that_closes_without_done_is_incomplete() -> None:
    prof = profile(faults=FaultModel(stall_rate=1.0, stall_after_pieces=2, stall_s=0.05))
    async with standin(prof) as (provider, state, _):
        with pytest.raises(ProviderStreamIncompleteError):
            await complete(provider, rag_request())
    assert state.stats.completed_streams == 0


async def test_admin_config_overrides_are_validated_and_recorded() -> None:
    async with standin(profile()) as (_, state, base), httpx.AsyncClient(
            base_url=base.removesuffix("/openai/v1")) as client:
        ok = await client.post("/admin/config", json={"limits": {"rpm": 5},
                                                      "faults": {"error_rate": 0.1}})
        invalid = await client.post("/admin/config", json={"limits": {"rpm": 0}})
        forbidden = await client.post("/admin/config", json={"label": "calibrated"})
        shown = (await client.get("/admin/config")).json()
        restored = await client.post("/admin/reset", json={"restore_profile": True})
    assert ok.status_code == 200 and invalid.status_code == 422
    assert forbidden.status_code == 422
    assert shown["profile"]["limits"]["rpm"] == 5
    assert shown["profile"]["provenance"]["limits.rpm"].startswith("scenario")
    assert shown["overrides"] == [{"limits": {"rpm": 5}, "faults": {"error_rate": 0.1}}]
    assert restored.status_code == 200 and state.profile.limits.rpm is None
    assert state.overrides == []


# --- Profile, fit and calibration ---------------------------------------------------


def probe_record(n: int, *, model: str = "m1", prompt_chars: int = 100, headers: float = 300.0,
                 total: float = 360.0, prompt_tok: int = 50, completion: int = 31,
                 reasoning: int = 20, remaining_tokens: int = 7900,
                 reset_tokens: float = 0.75) -> dict[str, Any]:
    return {
        "record": "inference", "status": "ok", "request_id": f"r{n}", "model": model,
        "label": "short", "prompt_chars": prompt_chars, "max_completion_tokens": 256,
        "timings_ms": {"headers": headers, "first_reasoning": headers + 2,
                       "first_content": headers + 30, "total": total},
        "usage": {"prompt_tokens": prompt_tok, "completion_tokens": completion,
                  "reasoning_tokens": reasoning},
        "response": {"rate_limits": {
            "limit_requests": 1000, "remaining_requests": 1000 - n,
            "reset_requests_s": 86.4 * n, "limit_tokens": 8000,
            "remaining_tokens": remaining_tokens, "reset_tokens_s": reset_tokens}},
    }


def fixture_records() -> list[dict[str, Any]]:
    return [
        probe_record(1, prompt_chars=100, prompt_tok=50, headers=300.0, total=360.0),
        probe_record(2, prompt_chars=900, prompt_tok=150, headers=320.0, total=380.0),
        probe_record(3, prompt_chars=1700, prompt_tok=250, headers=340.0, total=400.0,
                     remaining_tokens=7000, reset_tokens=9.9),
        {"record": "models", "status": "ok"},
        {**probe_record(4), "status": "error", "usage": None},
    ]


def test_fit_recovers_the_linear_token_and_latency_models() -> None:
    calls = calls_from_traces(fixture_records(), "probe-x")
    assert len(calls) == 3  # the models record and the failed call are skipped
    fitted, details = fit_profile(calls, seed=3)
    model = fitted.models["m1"]
    assert fitted.label == "calibrated" and fitted.source_probe_runs == ("probe-x",)
    tokens = model.tokens  # intercept 37.5 rounds to 38
    assert (tokens.base_prompt_tokens, tokens.prompt_chars_per_token) == (38, 8.0)
    assert model.latency.prompt_ms_per_1k_tokens == pytest.approx(200.0)
    assert model.latency.header_median_ms == pytest.approx(290.0)
    assert model.latency.header_sigma == 0.05  # identical bases: the floor applies
    assert model.latency.reasoning_tokens == 20
    assert model.latency.inter_token_ms == pytest.approx(58 / 30, abs=1e-3)
    assert (fitted.limits.tpm, fitted.limits.rpd, fitted.limits.rpm) == (8000, 1000, None)
    assert fitted.provenance["limits.rpm"].startswith("not measured")
    assert details["per_model"]["m1"]["n"] == 3


def test_header_checks_report_matches_and_mismatches() -> None:
    checks = header_checks(calls_from_traces(fixture_records(), "probe-x"))
    assert checks["requests"] == {"matched": 3, "checked": 3}
    assert checks["tokens"] == {"matched": 2, "checked": 3}  # 1000 used -> 7.5 s, not 9.9 s
    assert checks["calls"][2]["tokens_reset_predicted_s"] == 7.5


def test_no_evidence_gives_uncalibrated_defaults() -> None:
    fitted, details = fit_profile([])
    assert fitted.label == "uncalibrated_defaults" and details["reason"]
    with pytest.raises(ValueError):
        StandinProfile(label="calibrated", models=fitted.models)  # must name its source


def test_sampling_is_seeded_and_quantiles_are_nearest_rank() -> None:
    model = uncalibrated_defaults().models["openai/gpt-oss-20b"]
    first = [sample_header_delay_ms(model, 100, request_rng(5, i)) for i in range(5)]
    again = [sample_header_delay_ms(model, 100, request_rng(5, i)) for i in range(5)]
    other = [sample_header_delay_ms(model, 100, request_rng(6, i)) for i in range(5)]
    assert first == again and first != other
    q = quantiles([5.0, 1.0, 3.0, 2.0, 4.0])
    assert (q["n"], q["min"], q["p50"], q["p90"], q["max"]) == (5, 1.0, 3.0, 5.0, 5.0)
    assert quantiles([])["p50"] is None


def write_probe_run(root: Path, run_id: str, records: Sequence[dict[str, Any]],
                    status: str = "complete") -> None:
    run_dir = root / "groq_probes" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"kind": "groq_probe", "status": status,
                                                       "run_id": run_id}), encoding="utf-8")
    (run_dir / "traces.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def read_run(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    (run_dir,) = (root / "standin_calibration").iterdir()
    return tuple(json.loads((run_dir / name).read_text(encoding="utf-8"))  # type: ignore[return-value]
                 for name in ("manifest.json", "profile.json", "fit_report.json"))


async def test_calibration_script_fits_replays_and_writes_a_run(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    records = [probe_record(1, prompt_chars=100, prompt_tok=50, headers=30.0, total=40.0),
               probe_record(2, prompt_chars=900, prompt_tok=150, headers=32.0, total=42.0)]
    write_probe_run(evidence, "probe-20261112T031816Z-abc1234", records)
    write_probe_run(evidence, "probe-20261112T000000Z-abc1234", records[:1], status="aborted")
    code = await calibrate_standin.run(calibrate_standin.parse_args([
        "--evidence-dir", str(evidence), "--replay-repeats", "2", "--seed", "11"]),
        git_state=("abc1234", True))
    assert code == 0
    manifest, prof, report = read_run(evidence)
    assert manifest["kind"] == "standin_calibration" and manifest["status"] == "complete"
    assert manifest["source"] == "stand-in" and manifest["seed"] == 11
    assert prof["label"] == "calibrated" and prof["calibration_run_id"] == manifest["run_id"]
    assert prof["source_probe_runs"] == ["probe-20261112T031816Z-abc1234"]  # aborted skipped
    StandinProfile.model_validate(prof)
    assert report["label"] == "calibrated" and report["n_calls"] == 2
    comparison = report["comparison"]["m1"]
    assert comparison["total_ms"]["measured"]["n"] == 2
    assert comparison["total_ms"]["standin"]["n"] == 4  # 2 calls x 2 repeats
    assert comparison["prompt_tokens"]["standin"]["p50"] is not None
    assert report["replay"]["limits_enforced"] is False
    assert len(report["least_like_the_real_thing"]) >= 8
    assert report["header_checks"]["requests"]["checked"] == 2


async def test_calibration_without_evidence_is_labelled_uncalibrated(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    code = await calibrate_standin.run(calibrate_standin.parse_args([
        "--evidence-dir", str(evidence)]), git_state=("abc1234", False))
    assert code == 0
    manifest, prof, report = read_run(evidence)
    assert prof["label"] == report["label"] == "uncalibrated_defaults"
    assert report["comparison"] == {} and report["replay"]["skipped"]
    assert "uncalibrated_defaults" in manifest["notes"]


def test_rounding_helpers_are_stable() -> None:
    assert math.isclose(prompt_tokens(profile().models[MODEL], 41), 21)
