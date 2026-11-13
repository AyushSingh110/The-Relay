"""Seeded load shapes and the canonical P10 scenarios."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.experiments.patience import DEFAULT_PATIENCE, PatienceModel

Shape = Literal["poisson", "burst", "fixed_rate"]
CacheChoice = Literal["off", "exact"]
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Arrival(BaseModel):
    model_config = _FROZEN

    index: int
    at_s: Annotated[float, Field(ge=0.0)]
    question_id: str
    patience_s: Annotated[float, Field(gt=0.0)]
    phase: str = "main"


class Phase(BaseModel):
    """One stretch of arrivals with its own shape (a scenario has one or more)."""

    model_config = _FROZEN

    name: str = "main"
    shape: Shape
    pool: str
    count: Annotated[int, Field(ge=1)] | None = None  # burst / fixed count
    rate_per_min: Annotated[float, Field(gt=0.0)] | None = None
    duration_s: Annotated[float, Field(gt=0.0)] | None = None
    window_s: Annotated[float, Field(ge=0.0)] = 2.0  # burst spread
    start_s: Annotated[float, Field(ge=0.0)] = 0.0
    sequential: bool = False  # wait for each request before sending the next
    # "random": draw with replacement; "in_order": walk the pool in order.
    draw: Literal["random", "in_order"] = "random"

    @model_validator(mode="after")
    def _check(self) -> Phase:
        if self.shape == "burst" and self.count is None:
            raise ValueError("a burst needs count")
        if self.shape == "poisson" and (self.rate_per_min is None or self.duration_s is None):
            raise ValueError("poisson needs rate_per_min and duration_s")
        if self.shape == "fixed_rate" and (self.rate_per_min is None
                                           or (self.count is None and self.duration_s is None)):
            raise ValueError("fixed_rate needs rate_per_min and count or duration_s")
        return self


class ScenarioSpec(BaseModel):
    """Everything that defines a load run apart from the system under test's code."""

    model_config = _FROZEN

    name: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    description: str
    phases: tuple[Phase, ...]
    cache_mode: CacheChoice = "off"
    retrieval_setting: str = "small_fast"  # [PROPOSED] cheap retrieval for load runs
    deadline_ms: Annotated[int, Field(gt=0)] = 30_000  # [PROPOSED] service-level deadline
    patience: PatienceModel = DEFAULT_PATIENCE
    # Overrides applied to the calibrated stand-in profile (StandinProfile.with_overrides).
    standin_overrides: dict[str, Any] = {}
    # What the Relay's ProviderGuard is configured to believe (may differ from the stand-in).
    guard_overrides: dict[str, Any] = {}
    retry_budget_enabled: bool = True
    seed: int = 0


def build_arrivals(spec: ScenarioSpec, pools: dict[str, tuple[str, ...]]) -> list[Arrival]:
    """Deterministic arrivals for ``spec``: time-ordered, indexed from 0."""
    rng = random.Random(f"workload:{spec.name}:{spec.seed}")
    patience_rng = random.Random(f"patience:{spec.name}:{spec.seed}")
    raw: list[tuple[float, str, str]] = []
    for phase in spec.phases:
        pool = pools.get(phase.pool)
        if not pool:
            raise ValueError(f"unknown or empty question pool {phase.pool!r}")
        for position, at in enumerate(_times(phase, rng)):
            qid = pool[position % len(pool)] if phase.draw == "in_order" else rng.choice(pool)
            raw.append((phase.start_s + at, qid, phase.name))
    raw.sort(key=lambda item: item[0])
    return [Arrival(index=i, at_s=round(at, 3), question_id=qid,
                    patience_s=round(spec.patience.sample(patience_rng), 3), phase=name)
            for i, (at, qid, name) in enumerate(raw)]


def _times(phase: Phase, rng: random.Random) -> list[float]:
    if phase.shape == "burst":
        assert phase.count is not None
        return sorted(rng.uniform(0.0, phase.window_s) for _ in range(phase.count))
    assert phase.rate_per_min is not None
    gap = 60.0 / phase.rate_per_min
    if phase.shape == "fixed_rate":
        count = phase.count or int((phase.duration_s or 0) / gap)
        return [i * gap for i in range(count)]
    assert phase.duration_s is not None
    times, t = [], rng.expovariate(1.0 / gap)
    while t < phase.duration_s:
        times.append(t)
        t += rng.expovariate(1.0 / gap)
    return times


def offered_rate_per_min(arrivals: list[Arrival]) -> float | None:
    """Requests per minute over the span of the schedule (None for < 2 arrivals)."""
    if len(arrivals) < 2:
        return None
    span = arrivals[-1].at_s - arrivals[0].at_s
    return None if span <= 0 else round(len(arrivals) / span * 60.0, 2)


def load_pools(path: Path) -> dict[str, tuple[str, ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: tuple(ids) for name, ids in data["pools"].items()}


# --- Canonical scenarios ----------------------------------------------------------------

SCENARIOS: Final[dict[str, ScenarioSpec]] = {s.name: s for s in (
    ScenarioSpec(
        name="steady", cache_mode="off",
        description="Poisson arrivals at 4/min for 5 min, below the TPM-bound service rate.",
        phases=(Phase(shape="poisson", pool="all", rate_per_min=4.0, duration_s=300.0),),
    ),
    ScenarioSpec(
        name="burst", cache_mode="exact",
        description="30 requests within 2 s over 6 hot questions (duplicates); run with "
                    "cache on and with cache off.",
        phases=(Phase(shape="burst", pool="burst_hot", count=30, window_s=2.0),),
    ),
    ScenarioSpec(
        name="overload", cache_mode="off",
        description="Poisson arrivals at 20/min for 4 min, about 3x the service rate.",
        phases=(Phase(shape="poisson", pool="all", rate_per_min=20.0, duration_s=240.0),),
    ),
    ScenarioSpec(
        name="tpm_halved", cache_mode="off",
        # 8/min sits between halved real capacity (~4.9/min) and the guard's belief (~9.9/min).
        description="Poisson 8/min for 5 min while the provider's TPM is halved to 4000 "
                    "and the Relay's guard still believes 8000.",
        phases=(Phase(shape="poisson", pool="all", rate_per_min=8.0, duration_s=300.0),),
        standin_overrides={"limits": {"tpm": 4000}},
    ),
    ScenarioSpec(
        name="daily_exhausted", cache_mode="exact",
        description="3 sequential warm-up requests use the daily budget the guard is given "
                    "(RPD 4 x 0.75); then 20 requests at 10/min mix cached and uncached "
                    "questions.",
        phases=(Phase(name="warmup", shape="fixed_rate", pool="daily_warm", count=3,
                      rate_per_min=60.0, sequential=True, draw="in_order"),
                Phase(name="exhausted", shape="poisson", pool="daily_mix", rate_per_min=10.0,
                      duration_s=120.0, start_s=10.0)),
        guard_overrides={"rpd_limit": 4},
    ),
    ScenarioSpec(
        name="retry_amplification", cache_mode="off",
        description="Poisson 6/min for 3 min with 30 % injected 503s; run with the retry "
                    "budget on and off.",
        phases=(Phase(shape="poisson", pool="all", rate_per_min=6.0, duration_s=180.0),),
        standin_overrides={"faults": {"error_rate": 0.3, "error_status": 503}},
    ),
    ScenarioSpec(
        name="real_steady", cache_mode="off",
        description="Owner-approved real run: 10 distinct answerable questions at 2/min.",
        phases=(Phase(shape="fixed_rate", pool="real_steady", count=10, rate_per_min=2.0,
                      draw="in_order"),),
    ),
    ScenarioSpec(
        name="smoke", cache_mode="exact",
        description="10-request stand-in smoke run (into scratch/).",
        phases=(Phase(shape="fixed_rate", pool="all", count=10, rate_per_min=60.0),),
    ),
)}

__all__ = ["SCENARIOS", "Arrival", "Phase", "ScenarioSpec", "build_arrivals", "load_pools",
           "offered_rate_per_min"]
