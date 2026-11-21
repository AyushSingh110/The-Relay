"""The refusal every route raises and turns into a JSON error (shared by the /v1 routes and
the API-key check, without an import cycle)."""

from __future__ import annotations


class Rejected(Exception):
    """A request that is refused with ``status`` and a stable ``code``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


__all__ = ["Rejected"]
