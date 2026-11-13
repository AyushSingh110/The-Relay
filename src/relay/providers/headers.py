"""Response-header capture and rate-limit header parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from relay.redaction import redact_secrets

HEADER_SEMANTICS: Final = "documented_unverified"

LIMIT_REQUESTS: Final = "x-ratelimit-limit-requests"  # documented: RPD (unverified)
REMAINING_REQUESTS: Final = "x-ratelimit-remaining-requests"
RESET_REQUESTS: Final = "x-ratelimit-reset-requests"
LIMIT_TOKENS: Final = "x-ratelimit-limit-tokens"  # documented: TPM (unverified)
REMAINING_TOKENS: Final = "x-ratelimit-remaining-tokens"
RESET_TOKENS: Final = "x-ratelimit-reset-tokens"
RETRY_AFTER: Final = "retry-after"

_RATE_LIMIT_PREFIX: Final = "x-ratelimit-"
_BARE_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_SECONDS: Final[dict[str, float]] = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}

_STRICT_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")


def parse_duration_s(value: str) -> float | None:
    """Parse a reset duration such as ``"2m59.56s"``, ``"7.66s"``, ``"120ms"`` or ``"30"``."""
    text = value.strip()
    if not text:
        return None
    if _BARE_NUMBER.fullmatch(text):
        return float(text)
    position = 0
    total = 0.0
    for match in _DURATION_PART.finditer(text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        position = match.end()
    if position == 0 or position != len(text):
        return None
    return round(total, 6)


def parse_retry_after_s(value: str, received_at: datetime | None = None) -> float | None:
    """Parse ``retry-after``: delay-seconds, a duration string, or an HTTP-date."""
    seconds = parse_duration_s(value)
    if seconds is not None:
        return seconds
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = received_at or datetime.now(UTC)
    return max(0.0, round((when - now).total_seconds(), 6))


def capture_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Lower-case header names, join repeated headers with ``", "``, and sanitise."""
    merged: dict[str, str] = {}
    for name, value in items:
        key = name.lower()
        merged[key] = f"{merged[key]}, {value}" if key in merged else value
    return redact_secrets(dict(sorted(merged.items())))


class RateLimitSnapshot(BaseModel):
    """Parsed rate-limit headers from one response. Meanings are unverified hypotheses."""

    model_config = _STRICT_FROZEN

    semantics: Literal["documented_unverified"] = HEADER_SEMANTICS
    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests_s: float | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_tokens_s: float | None = None
    retry_after_s: float | None = None
    # Every x-ratelimit-* and retry-after header exactly as received.
    raw: dict[str, str] = {}
    parse_issues: tuple[str, ...] = ()

    @property
    def any_present(self) -> bool:
        return bool(self.raw)


def _parse_count(headers: dict[str, str], name: str, issues: list[str]) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    issues.append(f"{name}: not a non-negative integer: {value!r}")
    return None


def _parse_seconds(headers: dict[str, str], name: str, issues: list[str]) -> float | None:
    value = headers.get(name)
    if value is None:
        return None
    seconds = parse_duration_s(value)
    if seconds is None:
        issues.append(f"{name}: unrecognised duration: {value!r}")
    return seconds


def parse_rate_limits(
    headers: dict[str, str], received_at: datetime | None = None
) -> RateLimitSnapshot:
    """Build a snapshot from captured (lower-cased) headers."""
    issues: list[str] = []
    retry_after: float | None = None
    if RETRY_AFTER in headers:
        retry_after = parse_retry_after_s(headers[RETRY_AFTER], received_at)
        if retry_after is None:
            issues.append(f"{RETRY_AFTER}: unrecognised value: {headers[RETRY_AFTER]!r}")
    raw = {
        name: value
        for name, value in headers.items()
        if name.startswith(_RATE_LIMIT_PREFIX) or name == RETRY_AFTER
    }
    return RateLimitSnapshot(
        limit_requests=_parse_count(headers, LIMIT_REQUESTS, issues),
        remaining_requests=_parse_count(headers, REMAINING_REQUESTS, issues),
        reset_requests_s=_parse_seconds(headers, RESET_REQUESTS, issues),
        limit_tokens=_parse_count(headers, LIMIT_TOKENS, issues),
        remaining_tokens=_parse_count(headers, REMAINING_TOKENS, issues),
        reset_tokens_s=_parse_seconds(headers, RESET_TOKENS, issues),
        retry_after_s=retry_after,
        raw=raw,
        parse_issues=tuple(issues),
    )


class ResponseMeta(BaseModel):
    """Status line and headers of one provider HTTP response."""

    model_config = _STRICT_FROZEN

    status_code: int
    headers: dict[str, str]
    rate_limits: RateLimitSnapshot
    provider_request_id: str | None = None


def build_response_meta(
    status_code: int,
    header_items: Iterable[tuple[str, str]],
    received_at: datetime | None = None,
) -> ResponseMeta:
    headers = capture_headers(header_items)
    return ResponseMeta(
        status_code=status_code,
        headers=headers,
        rate_limits=parse_rate_limits(headers, received_at),
        provider_request_id=headers.get("x-request-id"),
    )


__all__ = [
    "HEADER_SEMANTICS",
    "RateLimitSnapshot",
    "ResponseMeta",
    "build_response_meta",
    "capture_headers",
    "parse_duration_s",
    "parse_rate_limits",
    "parse_retry_after_s",
]
