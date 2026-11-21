"""V2-09 units: pricing, the guard's admission forecast, the routing rules, escalation, the
settings, migration 6 and the truncated stream. Scripted providers; no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from relay.app.compose import guard_config, lane_pricing
from relay.app.settings import RelaySettings, load_relay_settings
from relay.control.pricing import LanePricing, LaneUse, RouteRecord, cost_usd, total_cost
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.control.retry import RetryBudget, RetryBudgetConfig, RetryConfig, RetryPolicy
from relay.control.router import NoLaneAvailableError, ProviderLane, ProviderRouter
from relay.errors import ConfigurationError
from relay.orchestrator.streaming import SentenceStream, StoppedEvent, VerifiedSentence
from relay.providers.base import ChatMessage, ChatRequest, ScriptedProvider, ScriptedReply
from relay.store import open_store
from relay.store.migrations import LATEST_VERSION

_S = 1_000_000_000


class Clock:
    def __init__(self) -> None:
        self.now = 10 * _S

    def __call__(self) -> int:
        return self.now


def request(max_tokens: int = 100) -> ChatRequest:
    return ChatRequest(model="m", max_completion_tokens=max_tokens,
                       messages=(ChatMessage(role="user", content="x" * 300),))


def lane(name: str, clock: Clock, *, escalate_to: str | None = None,
         **limits: Any) -> ProviderLane:
    guard = ProviderGuard(GuardConfig(**limits), clock=clock)
    return ProviderLane(name=name, provider=ScriptedProvider([ScriptedReply()]),
                        model=f"model-{name}", guard=guard, escalate_to=escalate_to)


async def spend(lane_: ProviderLane, clock: Clock) -> None:
    """One reservation on the lane, as a request would make."""
    permit = await lane_.guard.acquire(lane_.adapt(request()))
    lane_.guard.settle(permit, usage=None, stream_opened=False)


# --- pricing -----------------------------------------------------------------------------
def test_cost_is_tokens_times_the_lane_rates() -> None:
    price = LanePricing(input_per_m=0.5, output_per_m=1.5)
    assert cost_usd(1_200, 300, price) == pytest.approx((1_200 * 0.5 + 300 * 1.5) / 1e6)
    assert cost_usd(1_000_000, 0, price) == 0.5
    assert cost_usd(10, 10, None) is None  # no price: unknown, never 0


def test_a_total_is_unknown_as_soon_as_one_part_is() -> None:
    assert total_cost([0.25, 0.5]) == 0.75
    assert total_cost([0.25, None]) is None
    assert total_cost([]) == 0.0  # no call, no cost


def test_a_route_sums_its_calls() -> None:
    uses = tuple(LaneUse(lane=n, provider="stand-in", model="m", purpose=p, tokens_in=i,
                         tokens_out=o, usage_source="provider", cost_usd=c)
                 for n, p, i, o, c in [("a", "answer", 100, 20, 0.001),
                                       ("b", "escalation", 120, 80, 0.002)])
    route = RouteRecord(lane="b", provider="stand-in", model="m", uses=uses)
    assert (route.tokens_in, route.tokens_out, route.cost_usd) == (220, 100, 0.003)


# --- the guard's forecast -----------------------------------------------------------------
async def test_predict_forecasts_acquire_without_reserving() -> None:
    clock = Clock()
    a = lane("a", clock, rpm_limit=1)
    assert a.guard.predict(request()).wait_ns == 0
    before = a.guard.snapshot()
    a.guard.predict(request())
    assert a.guard.snapshot() == before  # nothing reserved
    await spend(a, clock)
    wait = a.guard.predict(request()).wait_ns
    assert wait is not None and 59 * _S < wait <= 60 * _S  # the RPM window frees in 60 s


async def test_predict_names_what_would_refuse() -> None:
    clock = Clock()
    daily = lane("d", clock, rpd_limit=1, daily_safety_fraction=1.0)
    await spend(daily, clock)
    assert daily.guard.predict(request()).refusal == "daily_budget_exhausted"
    small = lane("s", clock, tpm_limit=50)
    assert small.guard.predict(request()).refusal == "payload_too_large"
    broken = lane("b", clock, circuit_failure_threshold=1)
    from relay.providers.errors import ProviderServerError

    broken.guard.record_result(ProviderServerError("500"))
    assert broken.guard.predict(request()).refusal == "circuit_open"


# --- routing rules ------------------------------------------------------------------------
async def test_rule_1_and_2_primary_first_then_the_next_lane_that_admits() -> None:
    clock = Clock()
    a, b = lane("a", clock, rpm_limit=1), lane("b", clock)
    router = ProviderRouter([a, b], max_wait_ms=2000, clock=clock)
    assert router.select_lane(request()) is a
    await spend(a, clock)
    assert router.select_lane(request()) is b
    lanes, skipped, _ = router.candidates(request())
    assert lanes[0] is b and skipped[0].startswith("a:wait_")


async def test_rule_3_waits_on_the_lane_that_frees_first_up_to_the_deadline() -> None:
    clock = Clock()
    a, b = lane("a", clock, rpm_limit=1), lane("b", clock, rpm_limit=1)
    router = ProviderRouter([a, b], max_wait_ms=2000, clock=clock)
    await spend(a, clock)
    clock.now += 5 * _S
    await spend(b, clock)  # b frees 5 s after a
    assert router.select_lane(request()) is a  # no deadline: wait for a (v1 behaviour)
    with pytest.raises(NoLaneAvailableError) as caught:
        router.select_lane(request(), deadline_ns=clock.now + 10 * _S)
    assert caught.value.error_code == "predicted_wait_exceeds_deadline"
    assert caught.value.retry_after_s == pytest.approx(55.0, abs=0.1)


async def test_no_lane_that_can_admit_at_all_is_unavailable_with_the_primary_reason() -> None:
    clock = Clock()
    a = lane("a", clock, rpd_limit=1, daily_safety_fraction=1.0)
    b = lane("b", clock, tpm_limit=50)
    router = ProviderRouter([a, b], clock=clock)
    await spend(a, clock)
    with pytest.raises(NoLaneAvailableError) as caught:
        router.select_lane(request())
    assert caught.value.error_code == "daily_budget_exhausted"


def test_with_one_lane_routing_changes_nothing() -> None:
    clock = Clock()
    only = lane("only", clock)
    router = ProviderRouter([only], clock=clock)
    assert router.select_lane(request()) is only
    adapted = only.adapt(request(max_tokens=100))
    assert (adapted.model, adapted.max_completion_tokens) == ("model-only", 512)


def test_the_rewriter_keeps_its_own_small_budget_on_any_lane() -> None:
    assert lane("a", Clock()).adapt(request(max_tokens=64), keep_budget=True
                                    ).max_completion_tokens == 64


# --- escalation -----------------------------------------------------------------------------
def test_escalation_needs_a_target_that_admits_and_a_retry_from_the_budget() -> None:
    clock = Clock()
    a, b = lane("a", clock, escalate_to="b"), lane("b", clock, circuit_failure_threshold=1)
    budget = RetryBudget(RetryBudgetConfig(ratio=0.0, min_retries=1), clock=clock)
    router = ProviderRouter([a, b], retry=RetryPolicy(RetryConfig(), budget), clock=clock)
    assert router.escalate(b, "truncated", request()) is None  # b has no escalate_to
    assert router.escalate(a, "truncated", request()) is b
    assert budget.retries_in_window == 1  # it spent a retry
    assert router.escalate(a, "truncated", request()) is None  # budget spent
    from relay.providers.errors import ProviderServerError

    b.guard.record_result(ProviderServerError("500"))
    budget2 = RetryBudget(RetryBudgetConfig(min_retries=5), clock=clock)
    open_target = ProviderRouter([a, b], retry=RetryPolicy(RetryConfig(), budget2),
                                 clock=clock)
    assert open_target.escalate(a, "truncated", request()) is None  # circuit open


def test_a_router_rejects_an_escalation_lane_that_does_not_exist() -> None:
    with pytest.raises(ValueError, match="escalate_to"):
        ProviderRouter([lane("a", Clock(), escalate_to="zzz")])


# --- settings -------------------------------------------------------------------------------
LANES = """
[providers]
max_wait_ms = 750

[[providers.lanes]]
name = "fast"
kind = "standin"
model = "qwen/qwen3.8-27b"
embedded = true
max_completion_tokens = 200
escalate_to = "strong"
pricing = { input_per_m = 0.5, output_per_m = 1 }
guard = { rpm = 30 }

[[providers.lanes]]
name = "strong"
kind = "standin"
model = "qwen/qwen3.8-27b"
embedded = true
"""


def test_providers_lanes_are_read_with_their_prices_guards_and_escalation(
        tmp_path: Path) -> None:
    path = tmp_path / "relay.toml"
    path.write_text(LANES, encoding="utf-8")
    settings = load_relay_settings(path, environ={})
    fast, strong = settings.lanes
    assert settings.providers.max_wait_ms == 750
    assert (fast.escalate_to, strong.escalate_to) == ("strong", None)
    assert lane_pricing(fast) == LanePricing(input_per_m=0.5, output_per_m=1.0)
    assert lane_pricing(strong) is None  # no price: cost unknown
    assert guard_config(settings, fast).rpm_limit == 30  # its own
    assert guard_config(settings, strong).rpm_limit is None  # [guard]'s
    assert guard_config(settings, fast).tpm_limit == settings.guard.tpm  # inherited


def test_the_lanes_are_set_once(tmp_path: Path) -> None:
    path = tmp_path / "relay.toml"
    path.write_text(LANES + '\n[[lanes]]\nname = "x"\nkind = "standin"\nmodel = "m"\n'
                    'embedded = true\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not both"):
        load_relay_settings(path, environ={})


def test_an_unknown_escalation_lane_is_a_settings_error() -> None:
    with pytest.raises(ValueError, match="escalate_to"):
        RelaySettings.model_validate({"lanes": [
            {"name": "a", "kind": "standin", "model": "m", "embedded": True,
             "escalate_to": "nope"}]})


# --- store ----------------------------------------------------------------------------------
async def test_migration_6_adds_lane_cost_and_escalation_to_the_request_log() -> None:
    assert LATEST_VERSION == 6
    async with open_store(":memory:") as store:
        entry = await store.request_log.insert(status="ok", lane_name="b", cost_usd=0.00042,
                                               escalated=True)
        got = await store.request_log.get_by_id(entry.id)
        assert got is not None
        assert (got.lane_name, got.cost_usd, got.escalated) == ("b", 0.00042, True)
        plain = await store.request_log.insert(status="ok")
        again = await store.request_log.get_by_id(plain.id)
        assert again is not None
        assert (again.lane_name, again.cost_usd, again.escalated) == (None, None, False)


# --- a truncated stream ---------------------------------------------------------------------
class Truncated:
    """Tokens that end at the completion limit."""

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.truncated = True

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        for piece in self.pieces:
            yield piece


async def test_a_truncated_stream_never_releases_its_cut_off_last_sentence() -> None:
    from tests.test_ask_v1 import PASSAGES

    tokens = Truncated(["The model uses attention [P1]. It has six", " layers [P1"])
    events = [e async for e in SentenceStream(tokens, PASSAGES)]  # type: ignore[arg-type]
    released = [e for e in events if isinstance(e, VerifiedSentence)]
    stops = [e for e in events if isinstance(e, StoppedEvent)]
    assert all("six" not in e.text for e in released)  # the cut-off unit is not judged
    assert [s.reason_class for s in stops] == ["truncated"]


# review fixes -----------------------------------------------------------------------------
async def test_the_retry_budget_counts_one_first_attempt_per_request() -> None:
    from relay.control.router import RoutedGenerator
    from relay.providers.base import TokenUsage
    from relay.telemetry.spans import ServerSpanTracker

    clock = Clock()
    budget = RetryBudget(RetryBudgetConfig(ratio=1.0, min_retries=0), clock=clock)
    cut = ScriptedReply(content_chunks=("Attention",), finish_reason="length",
                        usage=TokenUsage(prompt_tokens=10, completion_tokens=5))
    whole = ScriptedReply(content_chunks=("Attention is all [P1].",),
                          usage=TokenUsage(prompt_tokens=10, completion_tokens=7))
    a = ProviderLane(name="a", provider=ScriptedProvider([cut]), model="m-a",
                     guard=ProviderGuard(GuardConfig(), clock=clock), escalate_to="b")
    b = ProviderLane(name="b", provider=ScriptedProvider([whole]), model="m-b",
                     guard=ProviderGuard(GuardConfig(), clock=clock))
    router = ProviderRouter([a, b], retry=RetryPolicy(RetryConfig(), budget), clock=clock)
    result = await RoutedGenerator(router).generate(request(), ServerSpanTracker())
    assert result.route is not None and result.route.escalated
    assert budget.allowance() == 1.0  # one request, one first attempt (not two)
    assert budget.retries_in_window == 1  # the escalation is the retry


async def test_a_request_refused_by_the_router_still_counts_as_a_first_attempt() -> None:
    from relay.control.router import RoutedGenerator
    from relay.telemetry.spans import ServerSpanTracker

    clock = Clock()
    budget = RetryBudget(RetryBudgetConfig(ratio=1.0, min_retries=0), clock=clock)
    only = lane("only", clock, rpd_limit=1, daily_safety_fraction=1.0)
    router = ProviderRouter([only], retry=RetryPolicy(RetryConfig(), budget), clock=clock)
    await spend(only, clock)
    result = await RoutedGenerator(router).generate(request(), ServerSpanTracker())
    assert result.error is not None and result.error.error_code == "daily_budget_exhausted"
    assert budget.allowance() == 1.0  # as the single guarded generator did before V2-09


async def test_an_expired_deadline_is_reported_as_expired() -> None:
    from relay.control.router import RoutedGenerator
    from relay.telemetry.spans import ServerSpanTracker

    clock = Clock()
    a = lane("a", clock, rpm_limit=1)
    router = ProviderRouter([a], clock=clock)
    await spend(a, clock)
    result = await RoutedGenerator(router).generate(request(), ServerSpanTracker(),
                                                    deadline_ns=clock.now - 1)
    assert result.error is not None and result.error.error_code == "deadline_expired"
