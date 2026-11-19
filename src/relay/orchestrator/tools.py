"""The pipeline's single tool, ``retrieve_passages``, behind a validated, timed gateway."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Mapping
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.errors import RelayError, sanitize_error
from relay.ingestion.arxiv_fetcher import PAPERS
from relay.retrieval.hybrid_index import RetrievalResult
from relay.telemetry.spans import MonotonicClock, ns_to_ms

TOOL_NAME: Final = "retrieve_passages"
DEFAULT_TOOL_TIMEOUT_S: Final = 10.0  # [PROPOSED]; rerank measured ~0.6-0.8 s per query (P04)
MAX_QUERY_CHARS: Final = 2000
_PAPER_IDS: Final = frozenset(p.paper_id for p in PAPERS)

_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


class PassageRetriever(Protocol):
    """What the gateway needs from a retriever (``HybridRetriever`` satisfies it)."""

    def retrieve(self, query: str, *, paper_filter: Collection[str] | None = None,
                 include_references: bool = False) -> RetrievalResult: ...


class RetrievePassagesArgs(BaseModel):
    model_config = _STRICT

    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)]
    retrieval_setting: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    paper_filter: tuple[str, ...] | None = None
    include_references: bool = False


class ToolCallRecord(BaseModel):
    """Trace entry for one tool call (successful or not)."""

    model_config = _STRICT

    tool: Literal["retrieve_passages"] = TOOL_NAME
    args: dict[str, Any]
    ok: bool
    elapsed_ms: float
    passages: int
    error: str | None = None


class ToolError(RelayError):
    """A tool call failed. ``record`` is the trace entry for the failed call."""

    def __init__(self, message: str, record: ToolCallRecord) -> None:
        super().__init__(message)
        self.record = record


class ToolInputError(ToolError):
    """Arguments failed validation; the tool was not executed."""


class ToolTimeoutError(ToolError):
    """The tool did not finish within its time limit."""


class ToolExecutionError(ToolError):
    """The tool raised while executing."""


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record: ToolCallRecord
    result: RetrievalResult


class ToolGateway:
    def __init__(
        self,
        retrievers: Mapping[str, PassageRetriever],
        *,
        timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        clock: MonotonicClock = time.perf_counter_ns,
        paper_ids: frozenset[str] | None = None,
    ) -> None:
        if not retrievers:
            raise ValueError("at least one retriever is required")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._retrievers = dict(retrievers)
        self._timeout_s = timeout_s
        self._clock = clock
        # valid paper_filter values: the v1 papers, or a collection's documents (V2-03)
        self._paper_ids = _PAPER_IDS if paper_ids is None else paper_ids

    @property
    def settings(self) -> frozenset[str]:
        return frozenset(self._retrievers)

    def with_retrievers(self, retrievers: Mapping[str, PassageRetriever], *,
                        paper_ids: frozenset[str] | None = None) -> ToolGateway:
        """A gateway over other retrievers (a collection's) with the same timeout and clock;
        ``paper_ids`` are the paper filters it accepts (default: this gateway's)."""
        return ToolGateway(retrievers, timeout_s=self._timeout_s, clock=self._clock,
                           paper_ids=self._paper_ids if paper_ids is None else paper_ids)

    async def retrieve_passages(self, raw_args: Mapping[str, Any]) -> ToolCall:
        started = self._clock()
        shown = {k: raw_args[k] for k in sorted(raw_args)}

        def record(ok: bool, passages: int = 0, error: str | None = None) -> ToolCallRecord:
            return ToolCallRecord(args=shown, ok=ok, passages=passages, error=error,
                                  elapsed_ms=ns_to_ms(self._clock() - started))

        try:
            args = RetrievePassagesArgs.model_validate(dict(raw_args))
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) or "args" for e in exc.errors()})
            message = f"invalid arguments: {', '.join(fields)}"
            raise ToolInputError(message, record(False, error=message)) from None
        problem = self._semantic_problem(args)
        if problem is not None:
            raise ToolInputError(problem, record(False, error=problem))

        retriever = self._retrievers[args.retrieval_setting]
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await asyncio.to_thread(
                    retriever.retrieve, args.query, paper_filter=args.paper_filter,
                    include_references=args.include_references,
                )
        except TimeoutError:
            message = f"{TOOL_NAME} timed out after {self._timeout_s:g}s"
            raise ToolTimeoutError(message, record(False, error=message)) from None
        except Exception as exc:  # tool boundary: any failure becomes a typed tool error
            message = f"{TOOL_NAME} failed: {sanitize_error(exc)}"
            raise ToolExecutionError(message, record(False, error=message)) from exc
        return ToolCall(record=record(True, passages=len(result.passages)), result=result)

    def _semantic_problem(self, args: RetrievePassagesArgs) -> str | None:
        if not args.query.strip():
            return "invalid arguments: query is blank"
        if args.retrieval_setting not in self._retrievers:
            return f"unknown retrieval_setting {args.retrieval_setting!r}"
        if args.paper_filter is not None:
            unknown = sorted(set(args.paper_filter) - self._paper_ids)
            if not args.paper_filter or unknown:
                return f"invalid paper_filter {list(args.paper_filter)}"
        return None


__all__ = [
    "DEFAULT_TOOL_TIMEOUT_S",
    "TOOL_NAME",
    "PassageRetriever",
    "RetrievePassagesArgs",
    "ToolCall",
    "ToolCallRecord",
    "ToolError",
    "ToolExecutionError",
    "ToolGateway",
    "ToolInputError",
    "ToolTimeoutError",
]
