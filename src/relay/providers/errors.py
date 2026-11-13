"""Typed provider failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from relay.errors import RelayError

if TYPE_CHECKING:
    from relay.providers.headers import ResponseMeta
    from relay.schemas import ErrorCode

RateLimitScope = Literal["minute", "daily", "unknown"]
RateLimitMetric = Literal["RPM", "TPM", "RPD", "TPD"]
ClassificationBasis = Literal["body", "headers_documented_unverified", "none"]
TimeoutPhase = Literal["read", "stall", "total"]


class ProviderError(RelayError):
    """Base class for failures talking to an OpenAI-compatible provider."""

    error_code: ErrorCode = "upstream_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        meta: ResponseMeta | None = None,
        body_excerpt: str | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.meta = meta
        self.status_code: int | None = None if meta is None else meta.status_code
        self.body_excerpt = body_excerpt
        self.provider_error_type = provider_error_type
        self.provider_error_code = provider_error_code


class ProviderRateLimitError(ProviderError):
    """HTTP 429 (or an in-stream rate-limit error)."""

    def __init__(
        self,
        message: str,
        *,
        scope: RateLimitScope,
        basis: ClassificationBasis,
        metric: RateLimitMetric | None = None,
        retry_after_s: float | None = None,
        detail: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.scope: RateLimitScope = scope
        self.basis: ClassificationBasis = basis
        self.metric = metric
        self.retry_after_s = retry_after_s
        # Numbers quoted in the error body, e.g. {"limit": 6000, "used": 5900, ...}.
        self.detail = detail or {}
        self.error_code = "daily_budget_exhausted" if scope == "daily" else "upstream_rate_limited"
        self.retryable = scope != "daily"


class ProviderAuthError(ProviderError):
    """HTTP 401/403: credentials rejected."""

    error_code: ErrorCode = "provider_auth_error"


class ProviderPayloadTooLargeError(ProviderError):
    """HTTP 413: request too large."""

    error_code: ErrorCode = "payload_too_large"


class ProviderBadRequestError(ProviderError):
    """Other 4xx: the request itself is wrong (our bug), so retrying cannot help."""

    error_code: ErrorCode = "internal_error"


class ProviderServerError(ProviderError):
    """5xx, Groq 498 capacity errors, or an error event inside a stream."""

    error_code: ErrorCode = "upstream_error"
    retryable = True


class ProviderTimeoutError(ProviderError):
    """No bytes for ``read`` / no SSE event for ``stall`` / overall ``total`` exceeded."""

    error_code: ErrorCode = "provider_stalled"
    retryable = True

    def __init__(self, message: str, *, phase: TimeoutPhase, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.phase: TimeoutPhase = phase


class ProviderConnectionError(ProviderError):
    """Connect/write/pool failures and connections dropped mid-response."""

    error_code: ErrorCode = "upstream_error"
    retryable = True

    def __init__(self, message: str, *, reason: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.reason = reason


class ProviderProtocolError(ProviderError):
    """The response violates the expected wire format (bad JSON, missing fields, ...)."""

    error_code: ErrorCode = "upstream_error"


class ProviderStreamIncompleteError(ProviderProtocolError):
    """The stream ended before the ``[DONE]`` sentinel."""

    retryable = True


class RealCallBudgetExceededError(RelayError):
    """A real provider call was refused locally because the hard call cap is reached."""


class ScriptExhaustedError(RelayError):
    """A ScriptedProvider received more calls than it has scripted replies."""


__all__ = [
    "ProviderAuthError",
    "ProviderBadRequestError",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderPayloadTooLargeError",
    "ProviderProtocolError",
    "ProviderRateLimitError",
    "ProviderServerError",
    "ProviderStreamIncompleteError",
    "ProviderTimeoutError",
    "RealCallBudgetExceededError",
    "ScriptExhaustedError",
]
