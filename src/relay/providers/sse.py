"""Incremental Server-Sent Events decoder (WHATWG HTML "event stream" rules)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEMessage:
    event: str | None
    data: str


class SSEDecoder:
    def __init__(self) -> None:
        self._data: list[str] = []
        self._event: str | None = None

    def feed_line(self, line: str) -> SSEMessage | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        return None

    def flush(self) -> SSEMessage | None:
        """Dispatch a pending event at end of stream."""
        return self._dispatch()

    def _dispatch(self) -> SSEMessage | None:
        if not self._data:
            self._event = None
            return None
        message = SSEMessage(event=self._event, data="\n".join(self._data))
        self._data = []
        self._event = None
        return message
