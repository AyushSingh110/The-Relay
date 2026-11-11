"""Frozen, strict contracts for result rows and run manifests.

`ResultRow` is the single source of truth for the `results.csv` schema. Columns 1-12
are exactly the columns required by ASSIGNMENT.md (page 4), in that order; the
appended columns follow ARCHITECTURE.md section 13.3.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Vocabulary -------------------------------------------------------------------

Source = Literal["real", "stand-in"]
Status = Literal["ok", "rate_limited", "timeout", "error"]
AnswerGrade = Literal["correct", "wrong", "refused_correctly", "refused_wrongly"]
CacheStatus = Literal["off", "miss", "exact_hit", "coalesced"]
VerificationVerdict = Literal[
    "VERIFIED", "REFUSAL_VERIFIED", "BLOCKED", "EARLY_REFUSAL", "NOT_RUN"
]
UsageSource = Literal["provider", "estimated", "none"]
ErrorCode = Literal[
    "citation_verification_failed",
    "queue_full",
    "predicted_wait_exceeds_deadline",
    "upstream_rate_limited",
    "daily_budget_exhausted",
    "deadline_expired",
    "client_gave_up",
    "provider_stalled",
    "upstream_error",
    "circuit_open",
    "provider_auth_error",
    "payload_too_large",
    "internal_error",
]
RunKind = Literal["groq_probe", "standin_calibration", "experiment"]
RunStatus = Literal["in_progress", "complete", "aborted"]

MANDATORY_COLUMNS: Final[tuple[str, ...]] = (
    "request_id",
    "scenario",
    "source",
    "start_time",
    "first_token_ms",
    "total_ms",
    "status",
    "retries",
    "tokens_in",
    "tokens_out",
    "retrieval_setting",
    "answer_grade",
)

# Which error codes are legal for each status (ARCHITECTURE.md section 13.4).
ERROR_CODES_BY_STATUS: Final[dict[str, frozenset[str]]] = {
    "ok": frozenset({"citation_verification_failed"}),
    "rate_limited": frozenset(
        {
            "queue_full",
            "predicted_wait_exceeds_deadline",
            "upstream_rate_limited",
            "daily_budget_exhausted",
        }
    ),
    "timeout": frozenset({"deadline_expired", "client_gave_up", "provider_stalled"}),
    "error": frozenset(
        {
            "upstream_error",
            "circuit_open",
            "provider_auth_error",
            "payload_too_large",
            "internal_error",
        }
    ),
}

SPAN_TOLERANCE_MS: Final[float] = 1.0
RETRIEVAL_SETTING_NONE: Final[str] = "none"
PROBE_SCENARIO_PREFIX: Final[str] = "probe_"
RUN_ID_PREFIX: Final[dict[str, str]] = {
    "groq_probe": "probe",
    "standin_calibration": "cal",
    "experiment": "exp",
}
RUN_ID_PATTERN: Final[str] = r"^(probe|cal|exp)-\d{8}T\d{6}Z-[0-9a-z]{4,40}$"

_SLUG = r"^[a-z0-9][a-z0-9_]*$"
Slug = Annotated[str, Field(pattern=_SLUG, max_length=64)]
NonNegMs = Annotated[float, Field(ge=0.0)]
NonNegInt = Annotated[int, Field(ge=0)]

_STRICT_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value


# --- Result row ---------------------------------------------------------------------


class ResultRow(BaseModel):
    """One request, as recorded in `results.csv`. Field order defines column order."""

    model_config = _STRICT_FROZEN

    # Columns 1-12: mandatory, exact names and order from ASSIGNMENT.md.
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    scenario: Slug
    source: Source
    start_time: datetime
    first_token_ms: NonNegMs | None
    total_ms: NonNegMs
    status: Status
    retries: NonNegInt
    tokens_in: NonNegInt
    tokens_out: NonNegInt
    retrieval_setting: Slug
    answer_grade: AnswerGrade | None

    # Columns 13+: appended telemetry (ARCHITECTURE.md section 13.3).
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
    question_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")] | None
    provider_first_token_ms: NonNegMs | None
    server_total_ms: NonNegMs | None
    queue_depth_at_entry: NonNegInt | None
    waiting_ms: NonNegMs | None
    working_ms: NonNegMs | None
    retrying_ms: NonNegMs | None
    retrieval_ms: NonNegMs | None
    cache_status: CacheStatus
    provider_called: bool
    degraded: bool
    model_id: Annotated[str, Field(min_length=1, max_length=128)] | None
    usage_source: UsageSource
    cost_usd: Annotated[float, Field(ge=0.0)] | None
    verification_verdict: VerificationVerdict
    error_code: ErrorCode | None

    @field_validator("start_time")
    @classmethod
    def _validate_start_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _check_invariants(self) -> ResultRow:
        problems = (
            _identity_problems(self)
            + _outcome_problems(self)
            + _span_problems(self)
            + _provider_problems(self)
            + _probe_problems(self)
        )
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _identity_problems(row: ResultRow) -> list[str]:
    if not row.request_id.startswith(f"{row.run_id}-"):
        return [f"request_id must start with its run_id followed by '-' ({row.run_id}-...)"]
    return []


def _outcome_problems(row: ResultRow) -> list[str]:
    problems: list[str] = []
    if row.error_code is not None and row.error_code not in ERROR_CODES_BY_STATUS[row.status]:
        problems.append(f"error_code {row.error_code!r} is not valid for status {row.status!r}")
    if row.status == "ok":
        if row.first_token_ms is None:
            problems.append("status 'ok' requires first_token_ms (something was delivered)")
    else:
        if row.error_code is None:
            problems.append(f"status {row.status!r} requires an error_code")
        if row.first_token_ms is not None:
            problems.append("first_token_ms must be blank when no answer was delivered")
        if row.answer_grade is not None:
            problems.append("answer_grade must be blank when no answer was delivered")
    if row.first_token_ms is not None and row.first_token_ms > row.total_ms:
        problems.append("first_token_ms cannot exceed total_ms")
    blocked = row.verification_verdict == "BLOCKED"
    flagged = row.error_code == "citation_verification_failed"
    if blocked != flagged:
        problems.append(
            "verification_verdict BLOCKED and error_code citation_verification_failed "
            "must occur together"
        )
    return problems


def _span_problems(row: ResultRow) -> list[str]:
    spans = (row.server_total_ms, row.waiting_ms, row.working_ms, row.retrying_ms)
    present = [value is not None for value in spans]
    if not any(present):
        return []
    if not all(present):
        return ["server_total_ms, waiting_ms, working_ms, retrying_ms must be all set or all blank"]
    server_total, waiting, working, retrying = (float(v) for v in spans)  # type: ignore[arg-type]
    problems: list[str] = []
    if abs((waiting + working + retrying) - server_total) > SPAN_TOLERANCE_MS:
        problems.append(
            "waiting_ms + working_ms + retrying_ms must equal server_total_ms "
            f"within {SPAN_TOLERANCE_MS} ms"
        )
    if row.provider_first_token_ms is not None and row.provider_first_token_ms > server_total:
        problems.append("provider_first_token_ms cannot exceed server_total_ms")
    return problems


def _provider_problems(row: ResultRow) -> list[str]:
    problems: list[str] = []
    if not row.provider_called:
        if row.tokens_in or row.tokens_out:
            problems.append("tokens must be 0 when the provider was not called")
        if row.usage_source != "none":
            problems.append("usage_source must be 'none' when the provider was not called")
        if row.retries:
            problems.append("retries must be 0 when the provider was not called")
        if row.provider_first_token_ms is not None:
            problems.append("provider_first_token_ms must be blank without a provider call")
    if row.usage_source == "none" and (row.tokens_in or row.tokens_out):
        problems.append("usage_source 'none' requires zero tokens")
    if row.cache_status in ("exact_hit", "coalesced") and row.provider_called:
        problems.append(f"cache_status {row.cache_status!r} implies provider_called=false")
    if row.verification_verdict == "EARLY_REFUSAL" and row.provider_called:
        problems.append("EARLY_REFUSAL implies provider_called=false")
    return problems


def _probe_problems(row: ResultRow) -> list[str]:
    if not row.scenario.startswith(PROBE_SCENARIO_PREFIX):
        return []
    problems: list[str] = []
    if row.retrieval_setting != RETRIEVAL_SETTING_NONE:
        problems.append("probe_* rows must use retrieval_setting 'none'")
    if row.answer_grade is not None:
        problems.append("probe_* rows must have a blank answer_grade")
    if row.source != "real":
        problems.append("probe_* rows record real Groq inference probes (source 'real')")
    if not row.provider_called:
        problems.append("probe_* rows record actual inference calls (provider_called=true)")
    return problems


# --- Run manifest ------------------------------------------------------------------


class RunManifest(BaseModel):
    """Metadata describing one evidence run directory (ARCHITECTURE.md section 13.1)."""

    model_config = _STRICT_FROZEN

    schema_version: Literal[1] = 1
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
    kind: RunKind
    scenarios: tuple[Slug, ...]
    source: Source | None
    model_id: str | None
    reasoning_settings: dict[str, Any] | None
    retrieval_settings: tuple[Slug, ...]
    cache_mode: Literal["off", "exact"] | None
    seed: int | None
    config_snapshot: dict[str, Any]
    git_commit: Annotated[str, Field(pattern=r"^[0-9a-z]{4,40}$")]
    git_dirty: bool
    started_at: datetime
    ended_at: datetime | None
    real_call_count: NonNegInt
    status: RunStatus
    notes: str | None

    @field_validator("started_at")
    @classmethod
    def _validate_started(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("ended_at")
    @classmethod
    def _validate_ended(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> RunManifest:
        problems: list[str] = []
        if not self.run_id.startswith(f"{RUN_ID_PREFIX[self.kind]}-"):
            problems.append(f"run_id prefix does not match kind {self.kind!r}")
        if (self.status == "in_progress") != (self.ended_at is None):
            problems.append("ended_at must be set exactly when the run is no longer in progress")
        if self.ended_at is not None and self.ended_at < self.started_at:
            problems.append("ended_at cannot be earlier than started_at")
        if self.kind == "groq_probe":
            if self.source not in (None, "real"):
                problems.append("groq_probe runs have source 'real'")
            bad = [s for s in self.scenarios if not s.startswith(PROBE_SCENARIO_PREFIX)]
            if bad:
                problems.append(f"groq_probe scenarios must start with 'probe_': {bad}")
        elif self.kind == "experiment":
            bad = [s for s in self.scenarios if s.startswith(PROBE_SCENARIO_PREFIX)]
            if bad:
                problems.append(f"experiment scenarios must not start with 'probe_': {bad}")
        if problems:
            raise ValueError("; ".join(problems))
        return self
