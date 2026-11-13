"""Stand-in behaviour profile, its defaults, and the fit from Phase 02-LIVE probe evidence."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Basis = Literal["measured", "derived", "header", "documented_unverified", "assumed",
                "scenario"]
ProfileLabel = Literal["calibrated", "uncalibrated_defaults"]

SHORTLIST: Final[tuple[str, ...]] = ("openai/gpt-oss-20b", "openai/gpt-oss-120b",
                                     "qwen/qwen3.8-27b")
SECONDS_PER_DAY: Final = 86_400.0
MIN_SIGMA: Final = 0.05

_MODEL = ConfigDict(frozen=True, extra="forbid")
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
NonNeg = Annotated[float, Field(ge=0.0)]
Positive = Annotated[float, Field(gt=0.0)]
PositiveInt = Annotated[int, Field(ge=1)]


# --- Profile contract ------------------------------------------------------------------


class LatencyModel(BaseModel):
    model_config = _MODEL

    header_median_ms: Positive
    header_sigma: NonNeg  # standard deviation of log(header delay)
    prompt_ms_per_1k_tokens: NonNeg
    inter_token_ms: NonNeg
    reasoning_tokens: Annotated[int, Field(ge=0)]


class TokenModel(BaseModel):
    model_config = _MODEL

    base_prompt_tokens: Annotated[int, Field(ge=0)]  # chat-template overhead per request
    prompt_chars_per_token: Positive
    output_chars_per_token: Positive


class ModelProfile(BaseModel):
    model_config = _MODEL

    latency: LatencyModel
    tokens: TokenModel
    context_window: PositiveInt = 131_072
    max_completion_tokens: PositiveInt = 32_768


class Limits(BaseModel):
    """``None`` = not enforced. Token buckets refill continuously (limit per window)."""

    model_config = _MODEL

    rpm: PositiveInt | None = None
    tpm: PositiveInt | None = None
    rpd: PositiveInt | None = None
    tpd: PositiveInt | None = None
    # What a request is charged against TPM/TPD when it is admitted.
    tpm_charge: Literal["prompt_plus_max_completion", "prompt_only"] = (
        "prompt_plus_max_completion")
    settle_to_usage: bool = True  # refund the unused reservation when the stream completes


class LoadModel(BaseModel):
    model_config = _MODEL

    knee_concurrency: PositiveInt = 4
    slowdown_per_extra: NonNeg = 0.25


class FaultModel(BaseModel):
    model_config = _MODEL

    error_rate: Fraction = 0.0
    error_status: Literal[500, 502, 503] = 503
    straggler_rate: Fraction = 0.0
    straggler_multiplier: Annotated[float, Field(ge=1.0)] = 5.0
    stall_rate: Fraction = 0.0
    stall_after_pieces: Annotated[int, Field(ge=0)] = 3
    stall_s: Positive = 60.0


class StandinProfile(BaseModel):
    model_config = _MODEL

    schema_version: Literal[1] = 1
    label: ProfileLabel
    calibration_run_id: str | None = None
    source_probe_runs: tuple[str, ...] = ()
    seed: int = 0
    time_scale: Positive = 1.0
    default_max_completion_tokens: PositiveInt = 1024
    models: dict[str, ModelProfile]
    limits: Limits = Limits()
    load: LoadModel = LoadModel()
    faults: FaultModel = FaultModel()
    provenance: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> StandinProfile:
        if not self.models:
            raise ValueError("a profile needs at least one model")
        if self.label == "calibrated" and not self.source_probe_runs:
            raise ValueError("a calibrated profile must name its source probe runs")
        return self

    def with_overrides(self, patch: Mapping[str, Any]) -> StandinProfile:
        """A new, validated profile with ``patch`` merged in (nested objects merge)."""
        forbidden = {"label", "calibration_run_id", "source_probe_runs", "schema_version",
                     "provenance"} & set(patch)
        if forbidden:
            raise ValueError(f"cannot override {sorted(forbidden)}")
        merged = _deep_merge(self.model_dump(mode="json"), patch)
        notes = dict(self.provenance)
        for path in _leaf_paths(patch):
            notes[path] = "scenario: set through /admin/config"
        merged["provenance"] = notes
        return StandinProfile.model_validate(merged)


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _leaf_paths(patch: Mapping[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            paths.extend(_leaf_paths(value, f"{path}."))
        else:
            paths.append(path)
    return paths


# --- Defaults --------------------------------------------------------------------------

_DEFAULT_LATENCY = LatencyModel(header_median_ms=500.0, header_sigma=0.3,
                                prompt_ms_per_1k_tokens=50.0, inter_token_ms=2.0,
                                reasoning_tokens=0)
_DEFAULT_TOKENS = TokenModel(base_prompt_tokens=40, prompt_chars_per_token=4.0,
                             output_chars_per_token=4.0)

COMMON_PROVENANCE: Final[dict[str, str]] = {
    "load.knee_concurrency": "assumed: no concurrent real calls were made (P02-LIVE was "
                             "sequential), so no knee was measured",
    "load.slowdown_per_extra": "assumed: not measured (see load.knee_concurrency)",
    "faults.error_rate": "assumed: 0 errors in 16 real calls; no failure rate measured",
    "faults.straggler_rate": "assumed: not measured; set per experiment",
    "faults.stall_rate": "assumed: no stalled stream observed in 16 real calls",
    "limits.rpm": "not measured: Groq sends no RPM header and no minute-limit probe was "
                  "approved; set per experiment",
    "limits.tpd": "not measured: Groq sends no TPD header; set per experiment",
    "limits.tpm_charge": "assumed: Groq's remaining-tokens drop did not equal usage in "
                         "P02-LIVE (unreconciled); reserving prompt + max_completion is the "
                         "conservative reading",
    "limits.settle_to_usage": "assumed: how Groq settles a reservation is not observed",
    "models.*.tokens.output_chars_per_token": "assumed: probe answers were 'OK', so output "
                                              "tokenisation was not measurable",
    "models.*.context_window": "assumed: not taken from /models",
    "models.*.max_completion_tokens": "assumed: not taken from /models",
    "429 body and retry-after": "documented_unverified: no 429 was observed on Groq",
}


def uncalibrated_defaults(*, seed: int = 0,
                          models: Sequence[str] = SHORTLIST) -> StandinProfile:
    """Plausible but unmeasured behaviour. Labelled so it cannot pass as Groq-measured."""
    notes = dict(COMMON_PROVENANCE)
    notes.update({
        "models.*.latency": "assumed: no probe evidence was available",
        "models.*.tokens": "assumed: no probe evidence was available",
        "limits.tpm": "assumed: no probe evidence was available (not enforced)",
        "limits.rpd": "assumed: no probe evidence was available (not enforced)",
    })
    return StandinProfile(
        label="uncalibrated_defaults", seed=seed,
        models={m: ModelProfile(latency=_DEFAULT_LATENCY, tokens=_DEFAULT_TOKENS)
                for m in models},
        provenance=notes,
    )


# --- Sampling (used by the server and by the calibration replay) -----------------------


def prompt_tokens(model: ModelProfile, prompt_chars: int) -> int:
    return model.tokens.base_prompt_tokens + math.ceil(
        prompt_chars / model.tokens.prompt_chars_per_token)


def output_tokens(model: ModelProfile, text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / model.tokens.output_chars_per_token))


def load_factor(profile: StandinProfile, active: int) -> float:
    extra = max(0, active - profile.load.knee_concurrency)
    return 1.0 + profile.load.slowdown_per_extra * extra


def sample_header_delay_ms(model: ModelProfile, prompt_token_count: int,
                           rng: random.Random) -> float:
    latency = model.latency
    base = latency.header_median_ms * math.exp(latency.header_sigma * rng.gauss(0.0, 1.0))
    return base + latency.prompt_ms_per_1k_tokens * prompt_token_count / 1000.0


def request_rng(seed: int, index: int) -> random.Random:
    """Independent, reproducible randomness for the ``index``-th request."""
    return random.Random(f"{seed}:{index}")


# --- Fitting from probe evidence -------------------------------------------------------


class ProbeCall(BaseModel):
    """The measured facts about one successful real inference call."""

    model_config = _MODEL

    run_id: str
    request_id: str
    model: str
    label: str
    prompt_chars: Annotated[int, Field(ge=0)]
    max_completion_tokens: PositiveInt
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    reasoning_tokens: Annotated[int, Field(ge=0)]
    headers_ms: NonNeg
    first_reasoning_ms: NonNeg | None
    first_content_ms: NonNeg | None
    total_ms: NonNeg
    limit_requests: int | None
    remaining_requests: int | None
    reset_requests_s: float | None
    limit_tokens: int | None
    remaining_tokens: int | None
    reset_tokens_s: float | None


def calls_from_traces(records: Iterable[Mapping[str, Any]], run_id: str) -> list[ProbeCall]:
    """Successful inference calls with provider usage. Everything else is skipped."""
    calls: list[ProbeCall] = []
    for record in records:
        usage = record.get("usage")
        timings = record.get("timings_ms") or {}
        if (record.get("record") != "inference" or record.get("status") != "ok"
                or not isinstance(usage, Mapping) or timings.get("headers") is None
                or timings.get("total") is None):
            continue
        limits = ((record.get("response") or {}).get("rate_limits") or {})
        calls.append(ProbeCall(
            run_id=run_id, request_id=str(record.get("request_id")),
            model=str(record["model"]), label=str(record.get("label", "")),
            prompt_chars=int(record["prompt_chars"]),
            max_completion_tokens=int(record["max_completion_tokens"]),
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            headers_ms=float(timings["headers"]),
            first_reasoning_ms=_float(timings.get("first_reasoning")),
            first_content_ms=_float(timings.get("first_content")),
            total_ms=float(timings["total"]),
            limit_requests=limits.get("limit_requests"),
            remaining_requests=limits.get("remaining_requests"),
            reset_requests_s=limits.get("reset_requests_s"),
            limit_tokens=limits.get("limit_tokens"),
            remaining_tokens=limits.get("remaining_tokens"),
            reset_tokens_s=limits.get("reset_tokens_s"),
        ))
    return calls


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _least_squares(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    """(slope, intercept), or None when x has no spread."""
    if len(xs) < 2 or len(set(xs)) < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope, my - slope * mx


def _round(value: float, digits: int = 4) -> float:
    return round(value, digits)


def fit_model(calls: Sequence[ProbeCall]) -> tuple[ModelProfile, dict[str, Any]]:
    """Fit one model's latency and token parameters. Returns the profile and fit details."""
    if not calls:
        raise ValueError("fit_model needs at least one call")
    details: dict[str, Any] = {"n": len(calls)}

    token_fit = _least_squares([c.prompt_chars for c in calls],
                               [c.prompt_tokens for c in calls])
    if token_fit is not None and token_fit[0] > 0:
        slope, intercept = token_fit
        tokens = TokenModel(base_prompt_tokens=max(0, round(intercept)),
                            prompt_chars_per_token=_round(1.0 / slope),
                            output_chars_per_token=_DEFAULT_TOKENS.output_chars_per_token)
        details["prompt_tokens_fit"] = {"basis": "measured", "slope_tokens_per_char":
                                        _round(slope, 6), "intercept_tokens": _round(intercept)}
    else:
        tokens = _DEFAULT_TOKENS
        details["prompt_tokens_fit"] = {"basis": "assumed", "reason": "no spread in prompt size"}

    xs = [c.prompt_tokens / 1000.0 for c in calls]
    ys = [c.headers_ms for c in calls]
    header_fit = _least_squares(xs, ys)
    slope_ms = max(0.0, header_fit[0]) if header_fit is not None else 0.0
    bases = [y - slope_ms * x for x, y in zip(xs, ys, strict=True)]
    if min(bases) <= 0:  # a steep slope would leave non-positive bases: drop it
        slope_ms, bases = 0.0, list(ys)
    logs = [math.log(b) for b in bases]
    sigma = statistics.stdev(logs) if len(logs) >= 2 else 0.0
    gaps = [(c.total_ms - _first_delta(c)) / max(1, c.completion_tokens - 1) for c in calls]
    latency = LatencyModel(
        header_median_ms=_round(statistics.median(bases), 3),
        header_sigma=_round(max(MIN_SIGMA, sigma)),
        prompt_ms_per_1k_tokens=_round(slope_ms, 3),
        inter_token_ms=_round(max(0.0, statistics.median(gaps)), 4),
        reasoning_tokens=round(statistics.median(c.reasoning_tokens for c in calls)),
    )
    details.update({
        "header_delay": {"raw_slope_ms_per_1k_tokens": None if header_fit is None
                         else _round(header_fit[0], 3), "used_slope": latency.
                         prompt_ms_per_1k_tokens, "log_sd": _round(sigma),
                         "sigma_floor": MIN_SIGMA},
        "inter_token_gaps_ms": [_round(g, 4) for g in gaps],
        "reasoning_tokens": [c.reasoning_tokens for c in calls],
    })
    return ModelProfile(latency=latency, tokens=tokens), details


def _first_delta(call: ProbeCall) -> float:
    candidates = [t for t in (call.first_reasoning_ms, call.first_content_ms) if t is not None]
    return min(candidates) if candidates else call.headers_ms


def _single_value(values: Iterable[int | None]) -> int | None:
    seen = {v for v in values if v is not None}
    return min(seen) if seen else None


def fit_profile(calls: Sequence[ProbeCall], *, seed: int = 0,
                calibration_run_id: str | None = None,
                ) -> tuple[StandinProfile, dict[str, Any]]:
    """A calibrated profile from real probe calls, or ``uncalibrated_defaults`` if none."""
    if not calls:
        return uncalibrated_defaults(seed=seed), {"label": "uncalibrated_defaults",
                                                  "reason": "no usable probe calls"}
    by_model: dict[str, list[ProbeCall]] = {}
    for call in calls:
        by_model.setdefault(call.model, []).append(call)
    models: dict[str, ModelProfile] = {}
    details: dict[str, Any] = {}
    notes = dict(COMMON_PROVENANCE)
    for model_id in sorted(by_model):
        group = by_model[model_id]
        models[model_id], details[model_id] = fit_model(group)
        n = len(group)
        prefix = f"models.{model_id}"
        notes[f"{prefix}.latency.header_median_ms"] = (
            f"measured: median client-observed time to response headers, n={n}, minus the "
            f"prompt-size slope")
        notes[f"{prefix}.latency.header_sigma"] = (
            f"measured: SD of log(header delay), n={n}, floored at {MIN_SIGMA}")
        notes[f"{prefix}.latency.prompt_ms_per_1k_tokens"] = (
            f"measured: least-squares slope of header delay on prompt tokens, n={n}, "
            "clipped at 0")
        notes[f"{prefix}.latency.inter_token_ms"] = (
            f"derived: median of (total - first delta) / (completion_tokens - 1), n={n}")
        notes[f"{prefix}.latency.reasoning_tokens"] = (
            f"measured: median usage.reasoning_tokens, n={n}")
        notes[f"{prefix}.tokens.base_prompt_tokens"] = (
            f"measured: intercept of prompt_tokens on prompt_chars, n={n}")
        notes[f"{prefix}.tokens.prompt_chars_per_token"] = (
            f"measured: 1 / slope of prompt_tokens on prompt_chars, n={n} "
            "(synthetic probe prompts, not paper text)")
    tpm = _single_value(c.limit_tokens for c in calls)
    rpd = _single_value(c.limit_requests for c in calls)
    notes["limits.tpm"] = ("header: x-ratelimit-limit-tokens (per model; meaning consistent "
                           "with TPM, see fit_report header_checks)")
    notes["limits.rpd"] = ("header: x-ratelimit-limit-requests (per model; meaning "
                           "consistent with RPD, see fit_report header_checks)")
    runs = tuple(sorted({c.run_id for c in calls}))
    profile = StandinProfile(
        label="calibrated", calibration_run_id=calibration_run_id, source_probe_runs=runs,
        seed=seed, models=models, limits=Limits(tpm=tpm, rpd=rpd), provenance=notes,
    )
    return profile, {"label": "calibrated", "per_model": details}


# --- Header consistency checks ---------------------------------------------------------


def header_checks(calls: Sequence[ProbeCall], *, tolerance_s: float = 0.05,
                  ) -> dict[str, Any]:
    """Test the token-bucket reading of Groq's headers against each probe call."""
    rows: list[dict[str, Any]] = []
    for c in calls:
        row: dict[str, Any] = {"request_id": c.request_id, "model": c.model}
        if None not in (c.limit_requests, c.remaining_requests, c.reset_requests_s):
            assert c.limit_requests is not None and c.remaining_requests is not None
            predicted = (c.limit_requests - c.remaining_requests) * SECONDS_PER_DAY \
                / c.limit_requests
            row["requests_reset_predicted_s"] = _round(predicted, 3)
            row["requests_reset_observed_s"] = c.reset_requests_s
            row["requests_match"] = abs(predicted - float(c.reset_requests_s or 0)) \
                <= tolerance_s
        if None not in (c.limit_tokens, c.remaining_tokens, c.reset_tokens_s):
            assert c.limit_tokens is not None and c.remaining_tokens is not None
            predicted = (c.limit_tokens - c.remaining_tokens) * 60.0 / c.limit_tokens
            row["tokens_reset_predicted_s"] = _round(predicted, 3)
            row["tokens_reset_observed_s"] = c.reset_tokens_s
            row["tokens_match"] = abs(predicted - float(c.reset_tokens_s or 0)) <= tolerance_s
            row["tokens_used_by_header"] = c.limit_tokens - c.remaining_tokens
            row["usage_total_tokens"] = c.prompt_tokens + c.completion_tokens
            row["prompt_plus_max_completion"] = c.prompt_tokens + c.max_completion_tokens
        rows.append(row)

    def share(key: str) -> dict[str, int]:
        values = [r[key] for r in rows if key in r]
        return {"matched": sum(values), "checked": len(values)}

    return {"tolerance_s": tolerance_s, "requests": share("requests_match"),
            "tokens": share("tokens_match"), "calls": rows}


def quantiles(values: Sequence[float]) -> dict[str, float | int | None]:
    """n, min, p50, p90, max (nearest-rank p90). Empty input gives Nones."""
    if not values:
        return {"n": 0, "min": None, "p50": None, "p90": None, "max": None}
    ordered = sorted(values)
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return {"n": len(ordered), "min": _round(ordered[0], 3),
            "p50": _round(statistics.median(ordered), 3), "p90": _round(ordered[rank - 1], 3),
            "max": _round(ordered[-1], 3)}


__all__ = [
    "COMMON_PROVENANCE",
    "SHORTLIST",
    "FaultModel",
    "LatencyModel",
    "Limits",
    "LoadModel",
    "ModelProfile",
    "ProbeCall",
    "StandinProfile",
    "TokenModel",
    "calls_from_traces",
    "fit_model",
    "fit_profile",
    "header_checks",
    "load_factor",
    "output_tokens",
    "prompt_tokens",
    "quantiles",
    "request_rng",
    "sample_header_delay_ms",
    "uncalibrated_defaults",
]
