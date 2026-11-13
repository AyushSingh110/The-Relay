"""Retry policy and retry budget (ARCHITECTURE.md section 9.5)."""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from relay.providers.errors import ProviderError, ProviderRateLimitError
from relay.telemetry.spans import MonotonicClock

_NS: Final = 1_000_000_000
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")

RetryReason = Literal[
    "retry",
    "not_retryable",
    "daily_scope",
    "max_attempts",
    "deadline",
    "budget_exhausted",
]


class RetryConfig(BaseModel):
    model_config = _STRICT

    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3  # first attempt + 2 retries
    base_delay_s: Annotated[float, Field(gt=0.0)] = 0.5
    max_delay_s: Annotated[float, Field(gt=0.0)] = 8.0
    deadline_share: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5


class RetryBudgetConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    ratio: Annotated[float, Field(ge=0.0)] = 0.2
    min_retries: Annotated[int, Field(ge=0)] = 3
    window_s: Annotated[float, Field(gt=0.0)] = 60.0


class RetryDecision(BaseModel):
    model_config = _STRICT

    retry: bool
    reason: RetryReason
    delay_s: float = 0.0
    budget_enforced: bool = True


class RetryBudget:
    """Rolling-window cap on retries relative to first attempts (shared across requests)."""

    def __init__(self, config: RetryBudgetConfig | None = None,
                 clock: MonotonicClock = time.perf_counter_ns) -> None:
        self.config = config or RetryBudgetConfig()
        self._clock = clock
        self._firsts: deque[int] = deque()
        self._retries: deque[int] = deque()

    def _trim(self) -> None:
        horizon = self._clock() - int(self.config.window_s * _NS)
        for events in (self._firsts, self._retries):
            while events and events[0] <= horizon:
                events.popleft()

    def record_first_attempt(self) -> None:
        self._firsts.append(self._clock())

    def allowance(self) -> float:
        self._trim()
        return self.config.ratio * len(self._firsts) + self.config.min_retries

    def try_spend(self) -> bool:
        """Record a retry if the budget allows it (always allowed when disabled)."""
        self._trim()
        if self.config.enabled and len(self._retries) + 1 > self.allowance():
            return False
        self._retries.append(self._clock())
        return True

    @property
    def retries_in_window(self) -> int:
        self._trim()
        return len(self._retries)


class RetryPolicy:
    def __init__(self, config: RetryConfig | None = None, budget: RetryBudget | None = None,
                 rng: random.Random | None = None) -> None:
        self.config = config or RetryConfig()
        self.budget = budget or RetryBudget()
        self._rng = rng or random.Random()

    def backoff_s(self, retry_number: int, retry_after_s: float | None = None) -> float:
        """Full-jitter delay for the ``retry_number``-th retry (1-based)."""
        cap = min(self.config.max_delay_s, self.config.base_delay_s * 2 ** (retry_number - 1))
        delay = self._rng.uniform(0.0, cap)
        if retry_after_s is not None:
            delay = max(delay, retry_after_s)
        return round(delay, 6)

    def decide(self, error: ProviderError, *, attempt: int,
               remaining_s: float | None) -> RetryDecision:
        """Should attempt number ``attempt`` (1-based, just failed) be retried?"""
        enforced = self.budget.config.enabled
        if isinstance(error, ProviderRateLimitError) and error.scope == "daily":
            return RetryDecision(retry=False, reason="daily_scope", budget_enforced=enforced)
        if not error.retryable:
            return RetryDecision(retry=False, reason="not_retryable", budget_enforced=enforced)
        if attempt >= self.config.max_attempts:
            return RetryDecision(retry=False, reason="max_attempts", budget_enforced=enforced)
        retry_after = error.retry_after_s if isinstance(error, ProviderRateLimitError) else None
        delay = self.backoff_s(attempt, retry_after)
        if remaining_s is not None and delay > self.config.deadline_share * remaining_s:
            return RetryDecision(retry=False, reason="deadline", delay_s=delay,
                                 budget_enforced=enforced)
        if not self.budget.try_spend():
            return RetryDecision(retry=False, reason="budget_exhausted", delay_s=delay,
                                 budget_enforced=enforced)
        return RetryDecision(retry=True, reason="retry", delay_s=delay, budget_enforced=enforced)


__all__ = [
    "RetryBudget",
    "RetryBudgetConfig",
    "RetryConfig",
    "RetryDecision",
    "RetryPolicy",
]
