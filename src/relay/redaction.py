"""Recursive redaction of secrets and account identifiers before anything is persisted."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr

REDACTED = "[REDACTED]"
REDACTED_ORG = "[REDACTED_ORG]"

_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "cookie",
)

_GROQ_KEY = re.compile(r"gsk_[A-Za-z0-9]{8,}")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_ORG_ID = re.compile(r"\borg_[A-Za-z0-9]{6,}\b")


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact_text(text: str) -> str:
    text = _GROQ_KEY.sub(REDACTED, text)
    text = _BEARER.sub(REDACTED, text)
    return _ORG_ID.sub(REDACTED_ORG, text)


def redact_secrets(value: Any) -> Any:
    """Return a copy of ``value`` with secrets removed."""
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_secrets(item) for item in value]
    return value
