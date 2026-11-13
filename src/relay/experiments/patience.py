"""Client patience: how long a simulated user waits before giving up (ARCHITECTURE.md §16.4).

The owner has not yet chosen the distribution. The default here is **[PROPOSED]**:
log-normal with median 20 s and sigma 0.6 (so about 10 % of users give up before 9 s and
about 10 % wait longer than 43 s), floored at 1 s. Every run records the model it used.

Patience is private to the client. The server is told a fixed service-level
``deadline_ms`` instead (ARCHITECTURE.md §16.10), so server-side shedding
(``deadline_expired`` / ``predicted_wait_exceeds_deadline``) and client abandonment
(``client_gave_up``) are measured separately.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class PatienceModel(BaseModel):
    model_config = _FROZEN

    kind: Literal["lognormal", "fixed"] = "lognormal"
    median_s: Annotated[float, Field(gt=0.0)] = 20.0
    sigma: Annotated[float, Field(ge=0.0)] = 0.6
    min_s: Annotated[float, Field(ge=0.0)] = 1.0
    max_s: Annotated[float, Field(gt=0.0)] | None = None

    def _clip(self, value: float) -> float:
        value = max(self.min_s, value)
        return value if self.max_s is None else min(self.max_s, value)

    def sample(self, rng: random.Random) -> float:
        if self.kind == "fixed":
            return self._clip(self.median_s)
        return self._clip(self.median_s * math.exp(self.sigma * rng.gauss(0.0, 1.0)))

    def quantile(self, q: float) -> float:
        """Analytic quantile of the (clipped) distribution."""
        if not 0.0 < q < 1.0:
            raise ValueError("q must be in (0, 1)")
        if self.kind == "fixed":
            return self._clip(self.median_s)
        return self._clip(self.median_s * math.exp(self.sigma * NormalDist().inv_cdf(q)))


DEFAULT_PATIENCE: Final = PatienceModel()
# [PROPOSED] fixed service-level deadline sent with every request (ms).
DEFAULT_DEADLINE_MS: Final = 30_000

__all__ = ["DEFAULT_DEADLINE_MS", "DEFAULT_PATIENCE", "PatienceModel"]
