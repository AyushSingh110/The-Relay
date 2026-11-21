"""Domain error hierarchy and secret-safe exception summaries."""

from __future__ import annotations


class RelayError(Exception):
    """Base class for every error raised by The Relay."""


class ConfigurationError(RelayError):
    """Configuration is missing or invalid."""


class ContractViolationError(RelayError):
    """Data does not satisfy a schema contract (e.g. a malformed results row)."""


class RunStoreError(RelayError):
    """Run-directory or manifest management failed."""


class RunAlreadyExistsError(RunStoreError):
    """A run directory with this run_id already exists; runs are never reused."""


class RunNotFoundError(RunStoreError):
    """No run directory exists for the requested run_id."""


class RunClosedError(RunStoreError):
    """A write was attempted on a run that has already been finalised."""


class AggregationError(RelayError):
    """Building the canonical results.csv failed validation."""


class CanonicalRunsError(AggregationError):
    """`canonical_runs.json` is malformed."""


class DuplicateRunSelectionError(AggregationError):
    """The same run_id was selected more than once."""


class DuplicateRequestIdError(AggregationError):
    """The same request_id appears more than once across selected runs."""


class IncompleteRunError(AggregationError):
    """A selected run is not in the `complete` state, or is of a kind that has no result rows."""


class ResultsOverwriteRefusedError(AggregationError):
    """The output file exists with different content and replacement was not requested."""


def sanitize_error(exc: BaseException) -> str:
    """Summarise an exception without its message, which may contain secrets or payloads."""
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    category = getattr(exc, "category", None)  # V2-11: a sanitised transport category
    parts = []
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        parts.append(f"status_code={status_code}")
    if isinstance(category, str) and category.isupper():
        parts.append(f"category={category}")
    return f"{name}({', '.join(parts)})" if parts else name
