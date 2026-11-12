"""Incremental Server-Sent Events decoder (WHATWG HTML "event stream" rules).

Fed one line at a time (line terminators already removed), it returns a complete
``SSEMessage`` when a blank line ends an event. Comment lines (``:...``) are ignored,
multiple ``data:`` lines are joined with ``\\n``, and one leading space after the colon
is stripped. ``id`` and ``retry`` fields are not needed by the provider and are ignored.
"""

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
        """Dispatch a pending event at end of stream.

        The spec discards an unterminated final event; we keep it so a server that omits
        the trailing blank line after ``[DONE]`` is still read correctly.
        """
        return self._dispatch()

    def _dispatch(self) -> SSEMessage | None:
        if not self._data:
            self._event = None
            return None
        message = SSEMessage(event=self._event, data="\n".join(self._data))
        self._data = []
        self._event = None
        return message
