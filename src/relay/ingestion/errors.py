"""Ingestion failures (fetching, validating and parsing the pinned papers)."""

from __future__ import annotations

from relay.errors import RelayError


class IngestionError(RelayError):
    """Base class for PDF ingestion failures."""


class PaperFetchError(IngestionError):
    """A paper could not be downloaded from its version-specific arXiv URL."""


class PaperValidationError(IngestionError):
    """A downloaded or cached PDF is not the expected, intact, exact-version paper."""


class ManifestError(IngestionError):
    """`data/papers/manifest.json` is malformed or disagrees with the files on disk."""
