"""Spans, CSV codec, configuration, error sanitisation, redaction and .gitignore tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from relay.config import Settings, load_settings
from relay.errors import ConfigurationError, ContractViolationError, sanitize_error
from relay.redaction import REDACTED, REDACTED_ORG, redact_secrets
from relay.schemas import ResultRow
from relay.telemetry.csv_codec import COLUMNS, read_rows, render_csv, row_from_record, row_to_record
from relay.telemetry.spans import ClientTimer, ServerSpanTracker
from tests.factories import FAKE_KEY, RUN_ID, failed_values, row_values

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.now_ns = 1_000_000_000

    def __call__(self) -> int:
        return self.now_ns

    def advance_ms(self, ms: float) -> None:
        self.now_ns += int(ms * 1_000_000)


# --- Spans --------------------------------------------------------------------------


def test_server_spans_sum_to_total_by_construction() -> None:
    clock = FakeClock()
    tracker = ServerSpanTracker(clock=clock)
    clock.advance_ms(12.5)  # queued
    tracker.switch("working")
    clock.advance_ms(40.25)
    tracker.mark_provider_first_token()
    clock.advance_ms(100.0)
    tracker.switch("retrying")  # backoff after a failure
    clock.advance_ms(250.0)
    tracker.switch("working")
    clock.advance_ms(7.0)
    tracker.mark_provider_first_token()  # second call must not move the first mark
    summary = tracker.finish()

    assert summary.waiting_ms == 12.5
    assert summary.working_ms == 147.25
    assert summary.retrying_ms == 250.0
    assert summary.server_total_ms == 409.75
    assert summary.provider_first_token_ms == 52.75
    parts = summary.waiting_ms + summary.working_ms + summary.retrying_ms
    assert abs(parts - summary.server_total_ms) < 0.01


def test_server_tracker_cannot_be_reused_after_finish() -> None:
    tracker = ServerSpanTracker(clock=FakeClock())
    tracker.finish()
    with pytest.raises(RuntimeError):
        tracker.switch("working")
    with pytest.raises(RuntimeError):
        tracker.finish()


def test_client_timer_records_first_byte_and_total() -> None:
    clock = FakeClock()
    started = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
    timer = ClientTimer(clock=clock, wall_clock=lambda: started)
    clock.advance_ms(800.0)
    timer.mark_first_byte()
    clock.advance_ms(5.0)
    timing = timer.finish()
    assert (timing.start_time, timing.first_token_ms, timing.total_ms) == (started, 800.0, 805.0)


def test_client_timer_without_delivery_has_blank_first_token() -> None:
    timing = ClientTimer(clock=FakeClock()).finish()
    assert timing.first_token_ms is None


# --- CSV codec ----------------------------------------------------------------------


def test_round_trip_preserves_rows(tmp_path: Path) -> None:
    rows = [
        ResultRow(**row_values(cost_usd=0.00012345, answer_grade="correct")),
        ResultRow(**failed_values(request_id=f"{RUN_ID}-000002")),
    ]
    path = tmp_path / "rows.csv"
    path.write_text(render_csv(rows), encoding="utf-8", newline="")
    assert read_rows(path) == rows


def test_blank_cells_for_missing_values_never_zero() -> None:
    record = row_to_record(ResultRow(**failed_values()))
    assert record["first_token_ms"] == ""
    assert record["provider_first_token_ms"] == ""
    assert record["answer_grade"] == ""
    assert record["error_code"] == "queue_full"
    assert record["provider_called"] == "false"


def test_timestamp_format_is_utc_milliseconds() -> None:
    assert row_to_record(ResultRow(**row_values()))["start_time"] == "2026-11-11T08:00:00.123Z"


def test_header_is_columns_in_order() -> None:
    assert render_csv([]).strip() == ",".join(COLUMNS)


@pytest.mark.parametrize(
    ("column", "raw"),
    [("retries", "1.0"), ("provider_called", "True"), ("answer_grade", "na"), ("total_ms", "nan")],
)
def test_parsing_is_strict(column: str, raw: str) -> None:
    record = row_to_record(ResultRow(**row_values()))
    record[column] = raw
    with pytest.raises(ContractViolationError):
        row_from_record(record)


def test_read_rows_rejects_wrong_header(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    path.write_text("request_id,scenario\n", encoding="utf-8")
    with pytest.raises(ContractViolationError, match="header"):
        read_rows(path)


# --- Configuration ------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("GROQ_API_KEY", "PROVIDER_MODE", "GROQ_MODEL", "CACHE_MODE", "MAX_REAL_CALLS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_are_offline_and_spend_nothing(clean_env) -> None:
    settings = load_settings(env_file=None)
    assert settings.provider_mode == "scripted"
    assert settings.max_real_calls == 0
    assert settings.groq_api_key is None


def test_groq_mode_requires_key(clean_env) -> None:
    clean_env.setenv("PROVIDER_MODE", "groq")
    with pytest.raises(ConfigurationError):
        load_settings(env_file=None)


def test_key_never_appears_in_repr_snapshot_or_errors(clean_env) -> None:
    clean_env.setenv("PROVIDER_MODE", "groq")
    clean_env.setenv("GROQ_API_KEY", FAKE_KEY)
    settings = load_settings(env_file=None)
    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == FAKE_KEY
    snapshot = settings.manifest_snapshot()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(snapshot)
    assert snapshot["groq_credential_configured"] is True

    clean_env.setenv("MAX_REAL_CALLS", "-1")
    with pytest.raises(ConfigurationError) as info:
        load_settings(env_file=None)
    assert FAKE_KEY not in str(info.value)


def test_env_file_is_read_when_given(clean_env, tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("PROVIDER_MODE=standin\nCACHE_MODE=off\n", encoding="utf-8")
    settings = load_settings(env_file=env_file)
    assert (settings.provider_mode, settings.cache_mode) == ("standin", "off")


def test_settings_are_frozen(clean_env) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        settings.max_real_calls = 5  # type: ignore[misc]


# --- Error sanitisation and redaction -------------------------------------------------


class _HttpFailure(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_sanitize_error_drops_message() -> None:
    exc = _HttpFailure(f"bad key {FAKE_KEY}", 401)
    assert sanitize_error(exc) == "_HttpFailure(status_code=401)"
    assert sanitize_error(ValueError(FAKE_KEY)) == "ValueError"


def test_redact_secrets_recursively() -> None:
    payload = {
        "headers": {"Authorization": f"Bearer {FAKE_KEY}", "x-ratelimit-limit-tokens": "6000"},
        "body": f"Rate limit reached in organization org_01abcdefgh key {FAKE_KEY}",
        "items": [f"Bearer {FAKE_KEY}", 3],
        "groq_api_key": "anything",
    }
    cleaned = redact_secrets(payload)
    assert cleaned["headers"]["Authorization"] == REDACTED
    assert cleaned["headers"]["x-ratelimit-limit-tokens"] == "6000"
    assert REDACTED_ORG in cleaned["body"]
    assert FAKE_KEY not in str(cleaned)
    assert cleaned["groq_api_key"] == REDACTED
    assert cleaned["items"][1] == 3


# --- Repository hygiene ---------------------------------------------------------------


def test_gitignore_protects_secrets_but_keeps_evidence() -> None:
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in lines
    assert "!.env.example" in lines
    for forbidden in ("results.csv", "evidence", "evidence/", "evidence/*", "*.csv", "runs/"):
        assert forbidden not in lines
