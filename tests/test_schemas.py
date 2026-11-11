"""ResultRow / RunManifest contract tests (ARCHITECTURE.md section 13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from relay.schemas import MANDATORY_COLUMNS, ResultRow, RunManifest
from tests.factories import RUN_ID, failed_values, probe_values, row_values

ASSIGNMENT_COLUMNS = (
    "request_id, scenario, source, start_time, first_token_ms, total_ms, status, retries, "
    "tokens_in, tokens_out, retrieval_setting, answer_grade"
)


def test_first_twelve_columns_match_assignment_exactly() -> None:
    expected = tuple(name.strip() for name in ASSIGNMENT_COLUMNS.split(","))
    assert expected == MANDATORY_COLUMNS
    assert tuple(ResultRow.model_fields)[:12] == expected


def test_valid_row_is_frozen(make_row) -> None:
    row = make_row()
    with pytest.raises(ValidationError):
        row.total_ms = 5.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "grade", ["correct", "wrong", "refused_correctly", "refused_wrongly", None]
)
def test_allowed_answer_grades(grade) -> None:
    assert ResultRow(**row_values(answer_grade=grade)).answer_grade == grade


@pytest.mark.parametrize("grade", ["na", "", "partial", "CORRECT"])
def test_unsupported_answer_grades_rejected(grade) -> None:
    with pytest.raises(ValidationError):
        ResultRow(**row_values(answer_grade=grade))


def test_strict_types_do_not_coerce() -> None:
    with pytest.raises(ValidationError):
        ResultRow(**row_values(tokens_in="5"))
    with pytest.raises(ValidationError):
        ResultRow(**row_values(provider_called="true"))


def test_unknown_column_rejected() -> None:
    with pytest.raises(ValidationError):
        ResultRow(**row_values(extra_column="x"))


def test_start_time_must_be_utc() -> None:
    with pytest.raises(ValidationError):
        ResultRow(**row_values(start_time=datetime(2026, 11, 11, 8, 0, 0)))
    ist = timezone(timedelta(hours=5, minutes=30))
    with pytest.raises(ValidationError):
        ResultRow(**row_values(start_time=datetime(2026, 11, 11, 13, 30, tzinfo=ist)))


def test_request_id_must_be_prefixed_by_run_id() -> None:
    with pytest.raises(ValidationError, match="must start with its run_id"):
        ResultRow(**row_values(request_id="other-000001"))


def test_non_ok_rows_need_error_code_and_blank_delivery_fields() -> None:
    assert ResultRow(**failed_values()).first_token_ms is None
    with pytest.raises(ValidationError, match="requires an error_code"):
        ResultRow(**failed_values(error_code=None))
    with pytest.raises(ValidationError, match="first_token_ms must be blank"):
        ResultRow(**failed_values(first_token_ms=1.0))
    with pytest.raises(ValidationError, match="answer_grade must be blank"):
        ResultRow(**failed_values(answer_grade="wrong"))


def test_ok_row_requires_first_token() -> None:
    with pytest.raises(ValidationError, match="requires first_token_ms"):
        ResultRow(**row_values(first_token_ms=None))


def test_error_code_must_match_status() -> None:
    with pytest.raises(ValidationError, match="not valid for status"):
        ResultRow(**failed_values(status="error", error_code="queue_full"))
    with pytest.raises(ValidationError, match="not valid for status"):
        ResultRow(**row_values(error_code="upstream_error"))


def test_client_abandonment_is_a_timeout() -> None:
    row = ResultRow(**failed_values(status="timeout", error_code="client_gave_up"))
    assert row.status == "timeout"
    with pytest.raises(ValidationError):
        ResultRow(**failed_values(status="error", error_code="client_gave_up"))


def test_verification_failure_refusal_is_ok_and_gradable() -> None:
    row = ResultRow(
        **row_values(
            verification_verdict="BLOCKED",
            error_code="citation_verification_failed",
            answer_grade="refused_wrongly",
        )
    )
    assert row.status == "ok"
    with pytest.raises(ValidationError, match="must occur together"):
        ResultRow(**row_values(verification_verdict="BLOCKED"))
    with pytest.raises(ValidationError, match="must occur together"):
        ResultRow(**row_values(error_code="citation_verification_failed"))


def test_span_invariant_enforced() -> None:
    ResultRow(**row_values(waiting_ms=10.5, working_ms=980.0, retrying_ms=0.0))  # +0.5 ms ok
    with pytest.raises(ValidationError, match="must equal server_total_ms"):
        ResultRow(**row_values(waiting_ms=20.0))
    with pytest.raises(ValidationError, match="all set or all blank"):
        ResultRow(**row_values(retrying_ms=None))


def test_spans_may_all_be_blank() -> None:
    row = ResultRow(
        **row_values(server_total_ms=None, waiting_ms=None, working_ms=None, retrying_ms=None)
    )
    assert row.server_total_ms is None


def test_first_token_cannot_exceed_total() -> None:
    with pytest.raises(ValidationError, match="cannot exceed total_ms"):
        ResultRow(**row_values(first_token_ms=1001.0))


def test_no_provider_call_means_no_tokens() -> None:
    with pytest.raises(ValidationError, match="tokens must be 0"):
        ResultRow(**failed_values(tokens_in=10, usage_source="estimated"))
    with pytest.raises(ValidationError, match="usage_source must be 'none'"):
        ResultRow(**failed_values(usage_source="provider"))


def test_cache_hits_and_coalesced_rows_do_not_call_provider() -> None:
    hit = row_values(
        cache_status="exact_hit",
        provider_called=False,
        tokens_in=0,
        tokens_out=0,
        usage_source="none",
        provider_first_token_ms=None,
    )
    assert ResultRow(**hit).cache_status == "exact_hit"
    with pytest.raises(ValidationError, match="implies provider_called=false"):
        ResultRow(**row_values(cache_status="coalesced"))


def test_early_refusal_has_no_provider_call() -> None:
    with pytest.raises(ValidationError, match="EARLY_REFUSAL"):
        ResultRow(**row_values(verification_verdict="EARLY_REFUSAL"))


_probe_values = probe_values


def test_probe_rows_follow_owner_rules() -> None:
    assert ResultRow(**_probe_values()).retrieval_setting == "none"
    with pytest.raises(ValidationError, match="retrieval_setting 'none'"):
        ResultRow(**_probe_values(retrieval_setting="small_fast"))
    with pytest.raises(ValidationError, match="blank answer_grade"):
        ResultRow(**_probe_values(answer_grade="correct"))
    with pytest.raises(ValidationError, match="source 'real'"):
        ResultRow(**_probe_values(source="stand-in"))


def test_probe_rate_limit_row_is_valid() -> None:
    row = ResultRow(
        **_probe_values(
            status="rate_limited",
            error_code="upstream_rate_limited",
            first_token_ms=None,
            provider_first_token_ms=None,
            tokens_in=0,
            tokens_out=0,
            usage_source="none",
        )
    )
    assert row.status == "rate_limited"


def _manifest_values(**overrides):
    values = {
        "run_id": RUN_ID,
        "kind": "experiment",
        "scenarios": ("steady",),
        "source": "stand-in",
        "model_id": None,
        "reasoning_settings": None,
        "retrieval_settings": ("balanced_rerank",),
        "cache_mode": "off",
        "seed": 42,
        "config_snapshot": {},
        "git_commit": "abc1234",
        "git_dirty": False,
        "started_at": datetime(2026, 11, 11, 8, 0, tzinfo=UTC),
        "ended_at": None,
        "real_call_count": 0,
        "status": "in_progress",
        "notes": None,
    }
    values.update(overrides)
    return values


def test_manifest_consistency_rules() -> None:
    RunManifest(**_manifest_values())
    with pytest.raises(ValidationError, match="prefix does not match"):
        RunManifest(**_manifest_values(kind="groq_probe"))
    with pytest.raises(ValidationError, match="ended_at must be set"):
        RunManifest(**_manifest_values(status="complete"))
    with pytest.raises(ValidationError, match="must not start with 'probe_'"):
        RunManifest(**_manifest_values(scenarios=("probe_baseline",)))
