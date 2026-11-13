"""P08 tests: exact cache, SingleFlight and the FastAPI service.

Fully offline. The app is driven through the ASGI protocol directly, so a test can make a
client disconnect at a chosen moment; one test goes through ``httpx.ASGITransport`` as
well. Retrieval is a scripted stub and the model is a ``ScriptedProvider`` behind a gate
that holds the provider call open until the test releases it. Every limit is an explicit
test input, not a measured Groq value.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from relay.api.app import RelayService, build_app, create_app
from relay.control.admission import AdmissionQueue
from relay.control.cache import (
    CachedAnswer,
    CacheScope,
    ExactCache,
    Flight,
    SingleFlight,
    corpus_digest,
    normalise_question,
)
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.orchestrator.pipeline import (
    OUT_OF_CORPUS_MESSAGE,
    SAFE_REFUSAL_MESSAGE,
    Pipeline,
    PipelineConfig,
    SingleCallGenerator,
)
from relay.orchestrator.tools import ToolGateway
from relay.providers.base import ChatRequest, ProviderEvent, ScriptedProvider, ScriptedReply
from relay.providers.errors import ProviderServerError
from relay.schemas import ResultRow, Source, VerificationVerdict
from relay.telemetry.run_store import RunHandle, create_run
from relay.telemetry.spans import ServerSpanTracker
from tests.test_pipeline_and_verifier import (
    GOOD_ANSWER,
    PASSAGES,
    USAGE,
    StubRetriever,
    passage,
    reply,
)

RUN_ID = "exp-20261113T080000Z-abc1234"
CORPUS = corpus_digest(["a" * 64, "b" * 64, "c" * 64])
Q1 = "What is d_model in the encoder?"
Q2 = "How many attention heads does the Transformer use?"
Q3 = "How many identical layers does the Transformer encoder have?"
UNGROUNDED = "The encoder uses d_model = 1024 [P1]."


# --- Harness ------------------------------------------------------------------------


class GatedProvider:
    """A ``ScriptedProvider`` whose calls wait at a gate until the test opens it."""

    def __init__(self, replies: Sequence[ScriptedReply], *, hold: bool = True) -> None:
        self.inner = ScriptedProvider(replies)
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.started = 0
        self.cancelled = 0
        if not hold:
            self.gate.set()

    @property
    def source(self) -> Source:
        return self.inner.source

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        self.started += 1
        self.entered.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        async for event in self.inner.stream_chat(request):
            yield event

    async def complete_chat(self, request: ChatRequest) -> Any:
        return await self.inner.complete_chat(request)

    async def list_models(self) -> Any:
        return await self.inner.list_models()

    async def aclose(self) -> None:
        return None


class Reply:
    """A decoded HTTP response from the app."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.events: dict[str, dict[str, Any]] = {}
        if headers.get("content-type", "").startswith("text/event-stream"):
            for block in body.decode("utf-8").strip().split("\n\n"):
                name_line, data_line = block.split("\n")
                assert name_line.startswith("event: ") and data_line.startswith("data: ")
                self.events[name_line[7:]] = json.loads(data_line[6:])

    @property
    def final(self) -> dict[str, Any]:
        return self.events["final"]


async def post(app: FastAPI, payload: dict[str, Any] | bytes, *,
               gone: asyncio.Event | None = None, path: str = "/ask") -> Reply:
    """One ASGI request. Setting ``gone`` delivers ``http.disconnect`` to the app."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    left = gone or asyncio.Event()
    body_sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
    }
    await app(scope, receive, send)
    start = messages[0]
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, b"".join(m.get("body", b"") for m in messages[1:]))


def ask_body(n: int, question: str = Q1, **extra: Any) -> dict[str, Any]:
    return {"request_id": f"{RUN_ID}-{n:06d}", "question": question,
            "retrieval_setting": "unit", **extra}


async def until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition was never reached"
        await asyncio.sleep(0.001)


def make_run(tmp_path: Path) -> RunHandle:
    return create_run(tmp_path / "evidence", "experiment", scenarios=["burst"],
                      config_snapshot={}, source="stand-in", cache_mode="exact",
                      git_state=("abc1234", False))


def traces(run: RunHandle) -> list[dict[str, Any]]:
    path = run.run_dir / "traces.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_app(replies: Sequence[ScriptedReply], *, hold: bool = False, cache_mode: str = "exact",
             capacity: int = 16, max_active: int = 1, run: RunHandle | None = None,
             retriever: StubRetriever | None = None, **guard: Any,
             ) -> tuple[FastAPI, GatedProvider, RelayService]:
    provider = GatedProvider(replies, hold=hold)
    guard.setdefault("max_concurrency", 2)
    app = create_app(
        provider=provider, retrievers={"unit": retriever or StubRetriever(PASSAGES)},
        top_k={"unit": 4},
        pipeline_config=PipelineConfig(model_id="test-model", max_completion_tokens=300),
        guard=ProviderGuard(GuardConfig(**guard)),
        queue=AdmissionQueue(capacity=capacity, service_rate_per_s=100.0,
                             max_active=max_active),
        corpus_sha256=CORPUS, cache_mode=cache_mode, run=run,  # type: ignore[arg-type]
    )
    return app, provider, app.state.relay


def to_row(final: dict[str, Any]) -> ResultRow:
    """The row a harness would write: the final event plus client-side timing."""
    delivered = final["status"] == "ok"
    client_total = final["server_total_ms"] + 1.0
    return ResultRow(
        scenario="burst", source="stand-in",
        start_time=datetime(2026, 11, 13, 8, 0, 0, tzinfo=UTC),
        first_token_ms=client_total if delivered else None, total_ms=client_total,
        answer_grade=None, run_id=RUN_ID, question_id=None, cost_usd=None, **final,
    )


def assert_span_sum(final: dict[str, Any]) -> None:
    parts = final["waiting_ms"] + final["working_ms"] + final["retrying_ms"]
    assert abs(parts - final["server_total_ms"]) <= 1.0


# --- Exact cache --------------------------------------------------------------------


def cached(verdict: VerificationVerdict = "VERIFIED", text: str = "answer [P1]") -> CachedAnswer:
    return CachedAnswer(delivered_text=text, refused=verdict != "VERIFIED", citations=(),
                        verification_verdict=verdict, model_id="m",
                        source_request_id="r1")


def make_cache(max_entries: int = 8, **scope: str) -> ExactCache:
    values = {"corpus_sha256": CORPUS, "prompt_version": "p05-1", "model_id": "m", **scope}
    return ExactCache(CacheScope(**values), max_entries=max_entries)


def test_question_normalisation_is_whitespace_and_unicode_form_only() -> None:
    assert normalise_question("  What   is\td_model?\n") == "What is d_model?"
    assert normalise_question("ﬁne") == "fine"  # NFKC folds the ligature
    assert normalise_question("What is N?") != normalise_question("what is n?")


def test_cache_hits_only_the_same_question_and_setting() -> None:
    cache = make_cache()
    assert cache.put("What is  d_model?", "balanced", cached(), degraded=False)
    assert cache.get(" What is d_model? ", "balanced") == cached()
    assert cache.get("What is d_ff?", "balanced") is None
    assert cache.get("what is d_model?", "balanced") is None
    assert cache.get("What is d_model?", "small_fast") is None
    assert (cache.hits, cache.misses, len(cache)) == (1, 3, 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [("corpus_sha256", "d" * 64), ("prompt_version", "p05-2"), ("model_id", "other")],
)
def test_cache_key_is_scoped_by_corpus_prompt_and_model(field: str, value: str) -> None:
    assert make_cache().key("q", "s") != make_cache(**{field: value}).key("q", "s")


def test_cache_admits_only_verified_and_non_degraded_answers() -> None:
    cache = make_cache()
    assert cache.put("q1", "s", cached("VERIFIED"), degraded=False)
    assert cache.put("q2", "s", cached("REFUSAL_VERIFIED"), degraded=False)
    for verdict in ("BLOCKED", "EARLY_REFUSAL", "NOT_RUN"):
        assert not cache.put("q3", "s", cached(verdict), degraded=False)  # type: ignore[arg-type]
    assert not cache.put("q4", "s", cached("VERIFIED"), degraded=True)
    assert (len(cache), cache.rejected) == (2, 4)
    assert cache.get("q3", "s") is None and cache.get("q4", "s") is None


def test_cache_evicts_least_recently_used_and_clears() -> None:
    cache = make_cache(max_entries=2)
    cache.put("a", "s", cached(text="A"), degraded=False)
    cache.put("b", "s", cached(text="B"), degraded=False)
    assert cache.get("a", "s") is not None  # "a" is now the most recently used
    cache.put("c", "s", cached(text="C"), degraded=False)
    assert cache.get("b", "s") is None and cache.get("a", "s") is not None
    cache.clear()
    assert (len(cache), cache.hits, cache.misses, cache.rejected) == (0, 0, 0, 0)
    with pytest.raises(ValueError):
        make_cache(max_entries=0)


def test_corpus_digest_ignores_order_and_rejects_empty_input() -> None:
    assert corpus_digest(["b" * 64, "a" * 64]) == corpus_digest(["a" * 64, "b" * 64])
    assert corpus_digest(["a" * 64]) != corpus_digest(["b" * 64])
    with pytest.raises(ValueError):
        corpus_digest([])


# --- SingleFlight -------------------------------------------------------------------


async def test_singleflight_runs_the_work_once_for_concurrent_callers() -> None:
    flights: SingleFlight[int] = SingleFlight()
    gate = asyncio.Event()
    runs = 0

    async def work(_: Flight[int]) -> int:
        nonlocal runs
        runs += 1
        await gate.wait()
        return 42

    tasks = [asyncio.create_task(flights.do("k", work)) for _ in range(5)]
    await until(lambda: flights.followed == 4)
    assert flights.in_flight("k") and len(flights) == 1
    gate.set()
    results = await asyncio.gather(*tasks)
    assert runs == 1 and {r.value for r in results} == {42}
    assert sorted(r.role for r in results) == ["follower"] * 4 + ["leader"]
    assert not flights.in_flight("k")
    assert (await flights.do("k", work)).role == "leader" and runs == 2  # not a cache


async def test_singleflight_cancels_work_only_when_the_last_waiter_leaves() -> None:
    flights: SingleFlight[str] = SingleFlight()
    gate = asyncio.Event()
    cancelled = False

    async def work(flight: Flight[str]) -> str:
        nonlocal cancelled
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return "done"

    leader = asyncio.create_task(flights.do("k", work))
    follower = asyncio.create_task(flights.do("k", work))
    await until(lambda: flights.followed == 1)
    leader.cancel()
    await asyncio.gather(leader, return_exceptions=True)
    assert not cancelled and flights.in_flight("k")  # the follower still needs the work
    follower.cancel()
    await asyncio.gather(follower, return_exceptions=True)
    await until(lambda: cancelled)
    assert not flights.in_flight("k")  # a new caller starts fresh
    gate.set()
    assert (await flights.do("k", work)).value == "done"


async def test_singleflight_shares_errors_and_bounds_a_followers_wait() -> None:
    flights: SingleFlight[int] = SingleFlight()
    gate = asyncio.Event()

    async def failing(_: Flight[int]) -> int:
        await gate.wait()
        raise RuntimeError("boom")

    leader = asyncio.create_task(flights.do("k", failing))
    await until(lambda: flights.in_flight("k"))
    with pytest.raises(TimeoutError):
        await flights.do("k", failing, follower_timeout_s=0.01)
    assert flights.in_flight("k")  # an impatient follower does not stop the leader
    follower = asyncio.create_task(flights.do("k", failing))
    await until(lambda: flights.followed == 2)
    gate.set()
    for task in (leader, follower):
        with pytest.raises(RuntimeError, match="boom"):
            await task


def test_span_snapshot_reads_without_closing_the_tracker() -> None:
    now = 0

    def clock() -> int:
        return now

    spans = ServerSpanTracker(clock)
    now = 5_000_000
    spans.switch("working")
    now = 12_000_000
    snap = spans.snapshot()
    assert (snap.waiting_ms, snap.working_ms, snap.server_total_ms) == (5.0, 7.0, 12.0)
    now = 20_000_000
    final = spans.finish()  # still open after the snapshot
    assert (final.working_ms, final.server_total_ms) == (15.0, 20.0)
    assert spans.snapshot() == final


# --- Service: answers, cache and coalescing -----------------------------------------


async def test_verified_answer_is_delivered_then_served_from_the_cache(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, provider, service = make_app([reply(GOOD_ANSWER)], run=run)
    first = await post(app, ask_body(1))
    assert first.status == 200 and first.headers["content-type"].startswith("text/event-stream")
    assert list(first.events) == ["answer", "final"]
    assert first.events["answer"]["text"] == GOOD_ANSWER and not first.events["answer"]["refused"]
    assert [c["tag"] for c in first.events["answer"]["citations"]] == [1, 2]
    final = first.final
    assert (final["cache_status"], final["verification_verdict"]) == ("miss", "VERIFIED")
    assert (final["tokens_in"], final["tokens_out"], final["usage_source"]) == (900, 40, "provider")
    assert final["provider_called"] and final["queue_depth_at_entry"] == 0
    assert final["provider_first_token_ms"] is not None and final["retrieval_ms"] > 0
    assert_span_sum(final)
    to_row(final)

    second = await post(app, ask_body(2, "  What is   d_model in the encoder? "))
    assert second.status == 200 and second.events["answer"] == first.events["answer"]
    hit = second.final
    assert (hit["cache_status"], hit["provider_called"], hit["model_id"]) == (
        "exact_hit", False, "test-model")
    assert (hit["tokens_in"], hit["tokens_out"], hit["usage_source"], hit["retries"]) == (
        0, 0, "none", 0)
    assert hit["provider_first_token_ms"] is None and hit["queue_depth_at_entry"] is None
    assert_span_sum(hit)
    to_row(hit)
    assert provider.started == 1 and len(service.cache or ()) == 1

    records = traces(run)
    assert [(r["record"], r.get("role")) for r in records] == [
        ("flight", None), ("request", "leader"), ("request", "cache_hit")]
    flight = records[0]
    assert flight["end"] == "completed" and flight["cached"]
    assert flight["delivered_text"] == GOOD_ANSWER and flight["final"]["tokens_in"] == 900
    assert flight["pipeline"]["verification"]["verdict"] == "VERIFIED"
    assert records[2]["source_request_id"] == f"{RUN_ID}-000001"


async def test_ten_identical_concurrent_misses_make_one_provider_call(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, provider, service = make_app([reply(GOOD_ANSWER)], hold=True, run=run)
    tasks = [asyncio.create_task(post(app, ask_body(n))) for n in range(1, 11)]
    await until(lambda: service.flights.followed == 9 and provider.entered.is_set())
    provider.gate.set()
    replies = await asyncio.gather(*tasks)

    assert provider.started == 1 and len(provider.inner.requests) == 1
    assert {r.status for r in replies} == {200}
    assert {r.events["answer"]["text"] for r in replies} == {GOOD_ANSWER}
    finals = [r.final for r in replies]
    leaders = [f for f in finals if f["cache_status"] == "miss"]
    followers = [f for f in finals if f["cache_status"] == "coalesced"]
    assert (len(leaders), len(followers)) == (1, 9)
    for follower in followers:
        assert not follower["provider_called"] and follower["usage_source"] == "none"
        assert (follower["tokens_in"], follower["tokens_out"], follower["retries"]) == (0, 0, 0)
        assert follower["provider_first_token_ms"] is None and follower["working_ms"] == 0.0
        assert follower["verification_verdict"] == "VERIFIED"
    rows = [to_row(final) for final in finals]
    assert len({row.request_id for row in rows}) == 10
    assert sum(row.tokens_in for row in rows) == USAGE.prompt_tokens
    assert sum(row.tokens_out for row in rows) == USAGE.completion_tokens
    for final in finals:
        assert_span_sum(final)

    records = traces(run)
    assert sum(r["record"] == "flight" for r in records) == 1
    assert sorted(r["role"] for r in records if r["record"] == "request") == (
        ["follower"] * 9 + ["leader"])

    eleventh = await post(app, ask_body(11))
    assert eleventh.final["cache_status"] == "exact_hit" and provider.started == 1


async def test_follower_disconnect_does_not_cancel_the_leader(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, provider, service = make_app([reply(GOOD_ANSWER)], hold=True, run=run)
    leader = asyncio.create_task(post(app, ask_body(1)))
    await until(provider.entered.is_set)
    gone = asyncio.Event()
    quitter = asyncio.create_task(post(app, ask_body(2), gone=gone))
    stayer = asyncio.create_task(post(app, ask_body(3)))
    await until(lambda: service.flights.followed == 2)

    gone.set()
    left = await quitter
    assert left.status == 499 and left.body == b""
    assert provider.cancelled == 0 and len(service.flights) == 1

    provider.gate.set()
    led, followed = await asyncio.gather(leader, stayer)
    assert (led.status, led.final["cache_status"]) == (200, "miss")
    assert (followed.status, followed.final["cache_status"]) == (200, "coalesced")
    assert provider.started == 1

    abandoned = [r for r in traces(run) if r.get("abandoned")]
    assert len(abandoned) == 1 and abandoned[0]["role"] == "follower"
    final = abandoned[0]["final"]
    assert (final["status"], final["error_code"], final["cache_status"]) == (
        "timeout", "client_gave_up", "coalesced")
    assert (final["tokens_in"], final["tokens_out"], final["provider_called"]) == (0, 0, False)
    assert_span_sum(final)


async def test_leader_disconnect_keeps_working_for_a_follower(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, provider, service = make_app([reply(GOOD_ANSWER)], hold=True, run=run)
    gone = asyncio.Event()
    leader = asyncio.create_task(post(app, ask_body(1), gone=gone))
    await until(provider.entered.is_set)
    follower = asyncio.create_task(post(app, ask_body(2)))
    await until(lambda: service.flights.followed == 1)

    gone.set()
    assert (await leader).status == 499
    assert provider.cancelled == 0  # the follower still needs the answer

    provider.gate.set()
    followed = await follower
    assert followed.status == 200 and followed.events["answer"]["text"] == GOOD_ANSWER
    assert (followed.final["cache_status"], followed.final["tokens_in"]) == ("coalesced", 0)
    assert len(service.cache or ()) == 1  # the verified answer is still cached

    records = traces(run)
    flight = next(r for r in records if r["record"] == "flight")
    assert flight["leader_request_id"] == f"{RUN_ID}-000001" and flight["end"] == "completed"
    assert (flight["final"]["tokens_in"], flight["final"]["tokens_out"]) == (900, 40)
    gave_up = next(r for r in records if r.get("abandoned"))
    assert gave_up["role"] == "leader" and gave_up["final"]["error_code"] == "client_gave_up"
    assert gave_up["final"]["cache_status"] == "miss" and "tokens_in" not in gave_up["final"]
    assert_span_sum(gave_up["final"])


async def test_sole_client_disconnect_cancels_the_provider_call(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, provider, service = make_app([reply(GOOD_ANSWER)], hold=True, run=run)
    gone = asyncio.Event()
    only = asyncio.create_task(post(app, ask_body(1), gone=gone))
    await until(provider.entered.is_set)
    gone.set()
    assert (await only).status == 499
    await until(lambda: provider.cancelled == 1)
    await until(lambda: service.queue.active == 0)
    assert len(service.flights) == 0 and len(service.cache or ()) == 0

    flight = next(r for r in traces(run) if r["record"] == "flight")
    final = flight["final"]
    assert flight["end"] == "cancelled" and final["error_code"] == "client_gave_up"
    # A call had been sent, so its cost is estimated rather than reported as zero.
    assert final["provider_called"] and final["usage_source"] == "estimated"
    assert final["tokens_in"] > 0 and final["tokens_out"] == 0 and final["retries"] == 0
    assert final["model_id"] == "test-model"
    assert_span_sum(final)

    provider.gate.set()  # the same question now starts a fresh flight
    again = await post(app, ask_body(2))
    assert (again.status, again.final["cache_status"]) == (200, "miss")
    assert provider.started == 2


async def test_follower_deadline_expires_without_stopping_the_leader() -> None:
    app, provider, service = make_app([reply(GOOD_ANSWER)], hold=True)
    leader = asyncio.create_task(post(app, ask_body(1)))
    await until(provider.entered.is_set)
    late = await post(app, ask_body(2, deadline_ms=20))
    assert late.status == 504 and list(late.events) == ["unavailable", "final"]
    assert (late.final["status"], late.final["error_code"], late.final["cache_status"]) == (
        "timeout", "deadline_expired", "coalesced")
    assert late.final["tokens_in"] == 0 and not late.final["provider_called"]
    to_row(late.final)
    provider.gate.set()
    assert (await leader).status == 200 and provider.started == 1


async def test_cache_off_never_coalesces_or_caches() -> None:
    app, provider, service = make_app([reply(GOOD_ANSWER), reply(GOOD_ANSWER)], hold=True,
                                      cache_mode="off", max_active=2)
    tasks = [asyncio.create_task(post(app, ask_body(n))) for n in (1, 2)]
    await until(lambda: provider.started == 2)
    provider.gate.set()
    replies = await asyncio.gather(*tasks)
    assert [r.final["cache_status"] for r in replies] == ["off", "off"]
    assert all(r.final["provider_called"] and r.final["tokens_in"] == 900 for r in replies)
    assert service.cache is None and service.flights.followed == 0
    for r in replies:
        to_row(r.final)


# --- Service: what is never cached --------------------------------------------------


async def test_blocked_answers_are_never_cached() -> None:
    app, provider, service = make_app([reply(UNGROUNDED), reply(UNGROUNDED)])
    for n in (1, 2):
        blocked = await post(app, ask_body(n))
        assert blocked.status == 200
        assert blocked.events["answer"] == {"text": SAFE_REFUSAL_MESSAGE, "refused": True,
                                            "citations": []}
        final = blocked.final
        assert (final["cache_status"], final["verification_verdict"], final["error_code"]) == (
            "miss", "BLOCKED", "citation_verification_failed")
        to_row(final)
    assert provider.started == 2 and len(service.cache or ()) == 0
    assert service.cache is not None and service.cache.rejected == 2


class OneTightBudget:
    """Refuses the first prompt, so the pipeline drops a passage and marks it degraded."""

    def __init__(self) -> None:
        self.asked = 0

    def fits(self, request: ChatRequest) -> bool:
        self.asked += 1
        return self.asked % 2 == 0


async def test_degraded_answers_are_never_cached() -> None:
    provider = ScriptedProvider([reply(GOOD_ANSWER), reply(GOOD_ANSWER)])
    pipeline = Pipeline(
        gateway=ToolGateway({"unit": StubRetriever(PASSAGES)}),
        generator=SingleCallGenerator(provider),
        config=PipelineConfig(model_id="test-model", max_completion_tokens=300),
        top_k={"unit": 4}, budget=OneTightBudget(),
    )
    cache = ExactCache(CacheScope(corpus_sha256=CORPUS, prompt_version="p05-1",
                                  model_id="test-model"))
    service = RelayService(pipeline=pipeline, cache=cache, model_id="test-model",
                           queue=AdmissionQueue(capacity=4, service_rate_per_s=100.0),
                           retrieval_settings=frozenset({"unit"}))
    app = build_app(service)
    for n in (1, 2):
        degraded = await post(app, ask_body(n))
        assert degraded.status == 200 and degraded.events["answer"]["text"] == GOOD_ANSWER
        final = degraded.final
        assert final["degraded"] and final["verification_verdict"] == "VERIFIED"
        assert final["cache_status"] == "miss"
        to_row(final)
    assert len(provider.requests) == 2 and len(cache) == 0 and cache.rejected == 2


async def test_early_refusal_is_delivered_without_provider_or_cache() -> None:
    off_topic = tuple(passage(f"t{i}", rerank=-9.0 - i, chunk_id=f"X{i}") for i in range(4))
    app, provider, service = make_app([], retriever=StubRetriever(off_topic))
    refused = await post(app, ask_body(1, "Who won the football league last year?"))
    assert refused.status == 200
    assert refused.events["answer"]["text"] == OUT_OF_CORPUS_MESSAGE
    final = refused.final
    assert (final["verification_verdict"], final["provider_called"], final["tokens_in"]) == (
        "EARLY_REFUSAL", False, 0)
    to_row(final)
    assert provider.started == 0 and len(service.cache or ()) == 0


# --- Service: unavailability --------------------------------------------------------


async def test_daily_budget_exhausted_serves_hits_and_points_to_passages(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, provider, service = make_app(
        [reply(GOOD_ANSWER), reply(GOOD_ANSWER)], run=run, rpd_limit=4,
        daily_safety_fraction=0.5,
    )
    for n, question in ((1, Q1), (2, Q2)):  # two calls reach 0.5 x 4: the daily stop
        assert (await post(app, ask_body(n, question))).final["cache_status"] == "miss"
    assert provider.started == 2
    health = await post_get(app, "/healthz")
    assert health["daily_stop"] is not None and health["cache_entries"] == 2

    hit = await post(app, ask_body(3, Q1))  # cached answers are still served
    assert hit.status == 200 and hit.final["cache_status"] == "exact_hit"
    assert hit.events["answer"]["text"] == GOOD_ANSWER

    miss = await post(app, ask_body(4, Q3))  # a miss gets an honest "unavailable"
    assert miss.status == 429 and list(miss.events) == ["unavailable", "final"]
    unavailable = miss.events["unavailable"]
    assert unavailable["error_code"] == "daily_budget_exhausted"
    assert "daily model budget" in unavailable["message"]
    assert "no answer was generated" in unavailable["message"]
    assert [p["chunk_id"] for p in unavailable["passages"]] == [p.chunk_id for p in PASSAGES]
    assert all(set(p) == {"chunk_id", "paper_id", "page_start", "page_end", "citation"}
               for p in unavailable["passages"])  # pointers only, no passage text
    final = miss.final
    assert (final["status"], final["error_code"], final["cache_status"]) == (
        "rate_limited", "daily_budget_exhausted", "miss")
    assert (final["provider_called"], final["tokens_in"], final["tokens_out"],
            final["usage_source"], final["verification_verdict"]) == (
        False, 0, 0, "none", "NOT_RUN")
    assert final["retrieval_ms"] > 0
    assert_span_sum(final)
    to_row(final)

    assert provider.started == 2  # the hit and the unavailable miss cost no provider call
    assert len(service.cache or ()) == 2  # the unavailable result was not cached
    flight = [r for r in traces(run) if r["record"] == "flight"][-1]
    assert flight["guard"]["daily_stop"] is not None and not flight["cached"]


async def post_get(app: FastAPI, path: str) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(path)
    assert response.status_code == 200
    return response.json()


async def test_full_queue_sheds_with_rate_limited_status() -> None:
    app, provider, service = make_app(
        [reply(GOOD_ANSWER), reply(GOOD_ANSWER)], hold=True, capacity=1)
    active = asyncio.create_task(post(app, ask_body(1, Q1)))
    await until(provider.entered.is_set)
    queued = asyncio.create_task(post(app, ask_body(2, Q2)))
    await until(lambda: service.queue.depth == 1)

    shed = await post(app, ask_body(3, Q3))
    assert shed.status == 429 and shed.events["unavailable"]["passages"] == []
    final = shed.final
    assert (final["status"], final["error_code"], final["queue_depth_at_entry"]) == (
        "rate_limited", "queue_full", 1)
    assert not final["provider_called"] and final["retrieval_ms"] is None
    assert_span_sum(final)
    to_row(final)

    provider.gate.set()
    first, second = await asyncio.gather(active, queued)
    assert first.status == second.status == 200
    assert second.final["queue_depth_at_entry"] == 0 and second.final["waiting_ms"] > 0
    assert provider.started == 2


async def test_upstream_failure_is_reported_with_pointers_and_not_cached() -> None:
    failing = ScriptedReply(error=ProviderServerError("boom"))
    app, provider, service = make_app([failing] * 3, circuit_failure_threshold=50)
    service.pipeline._generator._sleep = _no_sleep  # type: ignore[attr-defined]
    failed = await post(app, ask_body(1))
    assert failed.status == 502 and failed.final["error_code"] == "upstream_error"
    assert failed.final["provider_called"] and failed.final["retries"] == 2
    assert len(failed.events["unavailable"]["passages"]) == 4
    assert failed.final["retrying_ms"] >= 0
    assert_span_sum(failed.final)
    to_row(failed.final)
    assert len(service.cache or ()) == 0


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


async def test_retrieval_failure_is_an_internal_error() -> None:
    app, provider, _ = make_app([reply(GOOD_ANSWER)],
                                retriever=StubRetriever(error=RuntimeError("index gone")))
    broken = await post(app, ask_body(1))
    assert broken.status == 500 and broken.final["error_code"] == "internal_error"
    assert provider.started == 0
    to_row(broken.final)


# --- Service: validation, runs and transport ----------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        {"question": Q1, "retrieval_setting": "unit"},
        ask_body(1, extra_field=1),
        ask_body(1, "   "),
        ask_body(1, deadline_ms=0),
        ask_body(1, deadline_ms="soon"),
        ask_body(1, "x" * 2001),
        {**ask_body(1), "retrieval_setting": "no_such_setting"},
    ],
    ids=["bad-json", "missing-id", "extra-field", "blank-question", "zero-deadline",
         "string-deadline", "too-long", "unknown-setting"],
)
async def test_invalid_requests_are_rejected_before_any_work(
    payload: dict[str, Any] | bytes
) -> None:
    app, provider, service = make_app([reply(GOOD_ANSWER)])
    rejected = await post(app, payload)
    assert rejected.status == 422
    body = json.loads(rejected.body)
    assert body["error"] == "invalid_request" and body["details"]
    assert "x" * 50 not in rejected.body.decode()  # the input is not echoed back
    assert provider.started == 0 and len(service.flights) == 0


async def test_start_run_clears_the_cache_and_redirects_traces(tmp_path: Path) -> None:
    first_run = make_run(tmp_path / "a")
    app, provider, service = make_app([reply(GOOD_ANSWER), reply(GOOD_ANSWER)], run=first_run)
    assert (await post(app, ask_body(1))).final["cache_status"] == "miss"
    assert len(service.cache or ()) == 1 and service.run_id == first_run.run_id

    second_run = make_run(tmp_path / "b")
    service.start_run(second_run)
    assert len(service.cache or ()) == 0
    assert (await post(app, ask_body(2))).final["cache_status"] == "miss"  # no carry-over
    assert provider.started == 2
    assert len(traces(first_run)) == 2 and len(traces(second_run)) == 2


async def test_trace_failure_is_counted_and_does_not_break_the_answer(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    app, _, service = make_app([reply(GOOD_ANSWER)], run=run)
    run.finalize()  # writing to a closed run now fails
    answered = await post(app, ask_body(1))
    assert answered.status == 200 and service.trace_failures == 2
    assert (await post_get(app, "/healthz"))["trace_failures"] == 2


async def test_service_over_httpx_asgi_transport() -> None:
    app, provider, _ = make_app([reply(GOOD_ANSWER)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/ask", json=ask_body(1))
        health = (await client.get("/healthz")).json()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.startswith("event: answer\ndata: ")
    assert "\n\nevent: final\ndata: " in response.text and response.text.endswith("\n\n")
    assert health["status"] == "ok" and health["cache_entries"] == 1
    assert (health["queue_depth"], health["queue_active"], health["in_flight"]) == (0, 0, 0)
    assert provider.started == 1
