"""Builders for valid test data. Override any field to construct an invalid variant."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

RUN_ID = "exp-20261111T080000Z-abc1234"
PROBE_RUN_ID = "probe-20261111T070000Z-abc1234"
FAKE_KEY = "gsk_FAKEFAKEFAKE1234567890abcdef"


def row_values(**overrides: Any) -> dict[str, Any]:
    """Field values for a valid, verified, delivered stand-in answer."""
    values: dict[str, Any] = {
        "request_id": f"{RUN_ID}-000001",
        "scenario": "steady",
        "source": "stand-in",
        "start_time": datetime(2026, 11, 11, 8, 0, 0, 123000, tzinfo=UTC),
        "first_token_ms": 900.0,
        "total_ms": 1000.0,
        "status": "ok",
        "retries": 0,
        "tokens_in": 1200,
        "tokens_out": 150,
        "retrieval_setting": "balanced_rerank",
        "answer_grade": None,
        "run_id": RUN_ID,
        "question_id": "q01",
        "provider_first_token_ms": 300.0,
        "server_total_ms": 990.0,
        "queue_depth_at_entry": 0,
        "waiting_ms": 10.0,
        "working_ms": 980.0,
        "retrying_ms": 0.0,
        "retrieval_ms": 12.0,
        "cache_status": "miss",
        "provider_called": True,
        "degraded": False,
        "model_id": "openai/gpt-oss-20b",
        "usage_source": "provider",
        "cost_usd": None,
        "verification_verdict": "VERIFIED",
        "error_code": None,
    }
    values.update(overrides)
    return values


def failed_values(**overrides: Any) -> dict[str, Any]:
    """Field values for a request shed before any provider call (queue full)."""
    values = row_values(
        status="rate_limited",
        error_code="queue_full",
        first_token_ms=None,
        total_ms=4.0,
        tokens_in=0,
        tokens_out=0,
        provider_called=False,
        usage_source="none",
        provider_first_token_ms=None,
        server_total_ms=3.0,
        waiting_ms=3.0,
        working_ms=0.0,
        retrying_ms=0.0,
        retrieval_ms=None,
        verification_verdict="NOT_RUN",
        queue_depth_at_entry=5,
    )
    values.update(overrides)
    return values


def probe_values(**overrides: Any) -> dict[str, Any]:
    """Field values for a successful real Groq inference probe."""
    values = row_values(
        request_id=f"{PROBE_RUN_ID}-000001",
        run_id=PROBE_RUN_ID,
        scenario="probe_baseline",
        source="real",
        retrieval_setting="none",
        answer_grade=None,
        question_id=None,
        cache_status="off",
        verification_verdict="NOT_RUN",
        retrieval_ms=None,
    )
    values.update(overrides)
    return values
