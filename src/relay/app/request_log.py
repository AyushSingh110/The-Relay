"""Request-log writer: the service's trace records become ``request_log`` rows (V2-02).

``RelayService`` reports every finished HTTP request by calling ``append_trace(record)``
synchronously on its trace sink (v1 design, ``relay.api.app``). This sink keeps only the
``"request"`` records and writes one ``request_log`` row for each, from a background task,
so the request path never waits on the database.

Failure policy: a log row is never worth failing or delaying an answer, but no loss is
silent. Every outcome is counted (``written``, ``failed``, ``dropped``), the counters are
reported by ``GET /healthz`` and summarised in the log at shutdown, and:

- ``append_trace`` never raises; a record it cannot map counts as ``failed``;
- the queue is bounded; a record that does not fit, or arrives after ``aclose()``, counts
  as ``dropped``;
- a failed insert counts as ``failed`` and the writer carries on. Store errors are logged
  with their message; anything else (a bug) is logged with its traceback;
- ``aclose()`` waits at most ``flush_timeout_s`` for the queue to drain, then stops the
  writer and counts what is left as ``dropped``. It never hangs, even if the writer died;
- if the writer task ever stops unexpectedly, that is logged.

Field mapping, from the record's ``final`` metadata: ``ttft_ms`` is the server-side
``provider_first_token_ms`` and ``total_ms`` the server-side ``server_total_ms`` (the
server cannot see client timing); ``cache_hit`` / ``coalesced`` come from
``cache_status``; ``verdict`` from ``verification_verdict``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any, Final

import aiosqlite

from relay.errors import sanitize_error
from relay.store.repositories import RequestLogRepo, StoreError

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING: Final = 10_000
DEFAULT_FLUSH_TIMEOUT_S: Final = 10.0


def log_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    """``RequestLogRepo.insert`` arguments for one ``"request"`` trace record."""
    final = record.get("final") or {}
    if not isinstance(final, Mapping):
        raise TypeError("trace record 'final' is not a mapping")
    cache_status = final.get("cache_status")
    return {
        "status": final.get("status"),
        "question_hash": record.get("question_sha256"),
        "ttft_ms": final.get("provider_first_token_ms"),
        "total_ms": final.get("server_total_ms"),
        "tokens_in": final.get("tokens_in"),
        "tokens_out": final.get("tokens_out"),
        "cache_hit": cache_status == "exact_hit",
        "coalesced": cache_status == "coalesced",
        "verdict": final.get("verification_verdict"),
    }


class RequestLogSink:
    """A ``TraceSink`` for ``RelayService`` that persists request records."""

    run_id: str | None = None  # not an evidence run; /healthz shows no run id

    def __init__(self, repo: RequestLogRepo, *, max_pending: int = DEFAULT_MAX_PENDING,
                 flush_timeout_s: float = DEFAULT_FLUSH_TIMEOUT_S) -> None:
        if max_pending < 1 or flush_timeout_s <= 0:
            raise ValueError("max_pending and flush_timeout_s must be positive")
        self._repo = repo
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_pending)
        self._flush_timeout_s = flush_timeout_s
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.written = 0
        self.failed = 0
        self.dropped = 0

    def stats(self) -> dict[str, int]:
        return {"written": self.written, "failed": self.failed, "dropped": self.dropped,
                "pending": self._queue.qsize()}

    def append_trace(self, record: Mapping[str, Any]) -> None:
        if record.get("record") != "request":
            return
        if self._closed:
            self.dropped += 1
            logger.error("request log already closed: record dropped (%d dropped so far)",
                         self.dropped)
            return
        try:
            fields = log_fields(record)
        except (TypeError, AttributeError) as exc:
            self.failed += 1
            logger.error("request log record could not be mapped: %s", exc)
            return
        try:
            self._queue.put_nowait(fields)
        except asyncio.QueueFull:
            self.dropped += 1
            logger.error("request log queue full: record dropped (%d dropped so far)",
                         self.dropped)

    async def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._drain(), name="request-log-writer")
            self._task.add_done_callback(self._writer_stopped)

    def _writer_stopped(self, task: asyncio.Task[None]) -> None:
        if self._closed:
            return  # a normal shutdown
        if task.cancelled():
            logger.error("request log writer was cancelled before shutdown; new records "
                         "will queue until the store is closed")
            return
        error = task.exception()
        if error is not None:
            logger.error("request log writer stopped: %s", sanitize_error(error))

    async def flush(self) -> None:
        """Wait until every queued record has been written (or has failed)."""
        if self._task is None or self._task.done():
            return  # nothing is draining the queue; waiting would never end
        await self._queue.join()

    async def aclose(self) -> None:
        """Drain for at most ``flush_timeout_s``, stop the writer, report the totals."""
        if self._closed:
            return
        try:
            if self._task is not None and not self._task.done():
                try:
                    await asyncio.wait_for(self.flush(), self._flush_timeout_s)
                except TimeoutError:
                    logger.error("request log flush did not finish within %.1f s",
                                 self._flush_timeout_s)
        finally:
            self._closed = True
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
            left = self._queue.qsize()
            if left:
                self.dropped += left
                logger.error("request log closed with %d unwritten record(s)", left)
            level = logging.ERROR if self.failed or self.dropped else logging.INFO
            logger.log(level, "request log closed: %d written, %d failed, %d dropped",
                       self.written, self.failed, self.dropped)

    async def _drain(self) -> None:
        while True:
            fields = await self._queue.get()
            try:
                await self._repo.insert(**fields)
                self.written += 1
            except asyncio.CancelledError:
                self.dropped += 1  # stopped mid-write at shutdown: count it, then stop
                raise
            except (StoreError, aiosqlite.Error) as exc:
                self.failed += 1
                logger.error("request log write failed (%d so far): %s: %s", self.failed,
                             type(exc).__name__, exc)
            except Exception:  # a bug: keep the writer alive, but show the traceback
                self.failed += 1
                logger.exception("request log write failed unexpectedly (%d so far)",
                                 self.failed)
            finally:
                self._queue.task_done()


__all__ = ["DEFAULT_FLUSH_TIMEOUT_S", "DEFAULT_MAX_PENDING", "RequestLogSink", "log_fields"]
