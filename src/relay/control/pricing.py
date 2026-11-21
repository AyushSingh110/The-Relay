"""Per-lane pricing and the per-request cost ledger (plan V2-09; ARCHITECTURE_V2 §11).

``cost_usd = (tokens_in * input_per_m + tokens_out * output_per_m) / 1_000_000``.

Every model call a request makes is one ``LaneUse``: the answer, an escalation retry and a
follow-up rewrite each get their own entry, so the request's total tokens and cost are the
sum of its entries and reconcile with what each provider reported. A lane without a
configured price has ``cost_usd = None`` ("unknown"), and so does any total that includes
it: no price is ever guessed.

``RouteNote`` is a per-task record of the lane a call is using. ``TokenStream`` sets one
for its generation task (``ROUTE_NOTE``), so a stream cancelled mid-way (a failed sentence,
a client that left) still knows which lane and model it ran on.
"""

from __future__ import annotations

import math
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")
_PER: Final = 1_000_000
_DIGITS: Final = 10  # cost rounding: below a millionth of a cent

Purpose = Literal["answer", "escalation", "rewrite"]
Usage = Literal["provider", "estimated", "none"]


class LanePricing(BaseModel):
    """US dollars per million tokens, from the provider's own price list (never guessed)."""

    model_config = _STRICT

    input_per_m: Annotated[float, Field(ge=0.0)]
    output_per_m: Annotated[float, Field(ge=0.0)]


def cost_usd(tokens_in: int, tokens_out: int, pricing: LanePricing | None) -> float | None:
    """The cost of one call; None when the lane has no price."""
    if pricing is None:
        return None
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError("token counts cannot be negative")
    raw = (tokens_in * pricing.input_per_m + tokens_out * pricing.output_per_m) / _PER
    return round(raw, _DIGITS)


def total_cost(costs: list[float | None] | tuple[float | None, ...]) -> float | None:
    """The sum of the costs; 0.0 for no call at all; None as soon as one part is unknown."""
    if any(c is None for c in costs):
        return None
    return round(math.fsum(c for c in costs if c is not None), _DIGITS)


class LaneUse(BaseModel):
    """One model call of a request: where it ran and what it cost."""

    model_config = _STRICT

    lane: str
    provider: str  # the provider's source: "standin", "real" (Groq), "scripted", ...
    model: str
    purpose: Purpose
    tokens_in: Annotated[int, Field(ge=0)]
    tokens_out: Annotated[int, Field(ge=0)]
    usage_source: Usage
    cost_usd: Annotated[float, Field(ge=0.0)] | None


class RouteRecord(BaseModel):
    """How the router served one generation: the lane that produced the result, the lanes
    it skipped and why, and every call it paid for."""

    model_config = _STRICT

    lane: str | None  # None: no lane could serve
    provider: str | None
    model: str | None
    fallback: bool = False  # served by a lane other than the primary
    escalated: bool = False  # one escalation retry was made
    escalation_reason: str | None = None  # "truncated", "reasoning_exhausted", "empty"
    uses: tuple[LaneUse, ...] = ()
    skipped: tuple[str, ...] = ()  # "lane:reason", in the order the router decided
    retry_after_ms: float | None = None  # no lane could serve: when one may free up

    @property
    def tokens_in(self) -> int:
        return sum(u.tokens_in for u in self.uses)

    @property
    def tokens_out(self) -> int:
        return sum(u.tokens_out for u in self.uses)

    @property
    def cost_usd(self) -> float | None:
        """Every call's cost: 0.0 when no call was made, None when a price is unknown."""
        return total_cost([u.cost_usd for u in self.uses])


@dataclass
class RouteNote:
    """Mutable, per task: the lane the current call runs on (see the module docstring)."""

    lane: str | None = None
    provider: str | None = None
    model: str | None = None
    pricing: LanePricing | None = None
    purpose: Purpose = "answer"
    fallback: bool = False
    skipped: list[str] = field(default_factory=list)

    def use(self, tokens_in: int, tokens_out: int, usage_source: Usage) -> LaneUse | None:
        """A ledger entry for the noted lane (None when no lane was chosen yet)."""
        if self.lane is None or self.provider is None or self.model is None:
            return None
        return LaneUse(lane=self.lane, provider=self.provider, model=self.model,
                       purpose=self.purpose, tokens_in=tokens_in, tokens_out=tokens_out,
                       usage_source=usage_source,
                       cost_usd=cost_usd(tokens_in, tokens_out, self.pricing))


ROUTE_NOTE: ContextVar[RouteNote | None] = ContextVar("relay_route_note", default=None)


__all__ = ["ROUTE_NOTE", "LanePricing", "LaneUse", "Purpose", "RouteNote", "RouteRecord",
           "cost_usd", "total_cost"]
