"""The request pipeline (ARCHITECTURE.md sections 5-7).

    scope analysis -> retrieve_passages (tool) -> evidence gate
        -> [one paper-scoped re-query, at most once] -> gate
        -> one buffered LLM call -> verifier -> delivery | safe refusal

Guarantees:

- **At most one LLM call per request.** A verification failure produces a safe refusal;
  there is no repair loop and no second call. Retries are not this module's job: the
  pipeline talks to an :class:`AnswerGenerator`, and P06's ProviderGuard will provide one
  that owns retries, reservations and the ``retrying`` span.
- **Nothing unverified is delivered.** The stream is buffered; ``delivered_text`` is the
  verified answer or one of the fixed refusal messages.
- **Early refusals cost nothing.** No provider call, 0 tokens, ``usage_source="none"``.
- The caller owns the :class:`ServerSpanTracker` (it starts in ``waiting`` while the
  request queues) and calls ``finish()``; the pipeline switches it to ``working`` and
  marks the provider's first content token.
- ``asyncio.CancelledError`` (client disconnect) is never caught here.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.errors import ContractViolationError, sanitize_error
from relay.orchestrator.evidence_gate import GateDecision, decide, merge_expansion
from relay.orchestrator.prompt import PROMPT_VERSION, build_request
from relay.orchestrator.tools import ToolCallRecord, ToolError, ToolGateway
from relay.orchestrator.verifier import CitedPassage, VerificationResult, verify
from relay.providers.base import (
    ChatProvider,
    ChatRequest,
    CompletionResult,
    ContentDelta,
    ProviderEvent,
    ReasoningDelta,
    ResponseOpened,
    collect_stream,
)
from relay.providers.errors import ProviderError
from relay.providers.probing import CONSERVATIVE_CHARS_PER_TOKEN, status_for_error_code
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.scope import ScopeDecision, analyze_scope
from relay.schemas import ErrorCode, Status, UsageSource, VerificationVerdict
from relay.telemetry.spans import ServerSpanTracker

DEFAULT_MAX_COMPLETION_TOKENS: Final = 512  # [PROPOSED]; P02's 256 was never reached
MAX_QUESTION_CHARS: Final = 2000

LLM_REFUSAL_MESSAGE: Final = (
    "I can't answer this from the three papers in the library: the passages I found do not "
    "contain the information needed."
)
OUT_OF_CORPUS_MESSAGE: Final = (
    "I can only answer from three papers (\"Attention Is All You Need\", arXiv:1706.03762v7; "
    "Moshi, arXiv:2410.00037v1; H-Net, arXiv:2507.07955v2), and they do not cover this "
    "question."
)
VERSION_MESSAGE: Final = (
    "I can only answer from the exact paper versions in the library (arXiv:1706.03762v7, "
    "arXiv:2410.00037v1, arXiv:2507.07955v2), not from other versions of these papers."
)
SAFE_REFUSAL_MESSAGE: Final = (
    "I could not produce an answer that I can verify against the papers, so I am not "
    "answering rather than risk an unsupported claim."
)

OutcomeKind = Literal["answer", "llm_refusal", "early_refusal", "safe_refusal", "failed"]
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


# --- Contracts ------------------------------------------------------------------------


class PipelineRequest(BaseModel):
    model_config = _STRICT

    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
    retrieval_setting: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]

    @model_validator(mode="after")
    def _not_blank(self) -> PipelineRequest:
        if not self.question.strip():
            raise ValueError("question must not be blank")
        return self


class PipelineConfig(BaseModel):
    model_config = _STRICT

    model_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_completion_tokens: Annotated[int, Field(ge=1)] = DEFAULT_MAX_COMPLETION_TOKENS
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = 0.0
    reasoning_effort: str | None = None


class PipelineTrace(BaseModel):
    """Everything needed to audit one request (goes to the run's traces, not results.csv)."""

    model_config = _STRICT

    prompt_version: str
    scope: ScopeDecision
    gate_decisions: tuple[GateDecision, ...]
    tool_calls: tuple[ToolCallRecord, ...]
    loop_count: int
    prompt_chars: int | None = None
    raw_answer: str | None = None
    finish_reason: str | None = None
    truncated: bool = False
    reasoning_chars: int = 0
    verification: VerificationResult | None = None
    provider_error: str | None = None


class PipelineOutcome(BaseModel):
    """Result of one request, shaped so a ``ResultRow`` can be built from it directly."""

    model_config = _STRICT

    request_id: str
    retrieval_setting: str
    kind: OutcomeKind
    status: Status
    error_code: ErrorCode | None
    verification_verdict: VerificationVerdict
    delivered_text: str | None
    refused: bool
    citations: tuple[CitedPassage, ...]
    passages: tuple[RetrievedPassage, ...]
    provider_called: bool
    retries: int
    tokens_in: int
    tokens_out: int
    usage_source: UsageSource
    model_id: str | None
    degraded: bool
    retrieval_ms: float
    trace: PipelineTrace

    @model_validator(mode="after")
    def _consistent(self) -> PipelineOutcome:
        problems: list[str] = []
        if (self.status == "ok") != (self.delivered_text is not None):
            problems.append("status 'ok' exactly when something is delivered")
        blocked = self.verification_verdict == "BLOCKED"
        if blocked != (self.error_code == "citation_verification_failed"):
            problems.append("BLOCKED and citation_verification_failed must occur together")
        if self.status == "ok" and self.error_code not in (None, "citation_verification_failed"):
            problems.append("ok outcomes carry no other error code")
        if self.status != "ok" and self.error_code is None:
            problems.append("failed outcomes need an error code")
        unspent = not (self.tokens_in or self.tokens_out)
        if not self.provider_called and not (unspent and self.retries == 0
                                             and self.usage_source == "none"):
            problems.append("no provider call means no tokens, retries or usage")
        if self.usage_source == "none" and not unspent:
            problems.append("usage_source 'none' requires zero tokens")
        if problems:
            raise ValueError("; ".join(problems))
        return self


# --- Generation seam ------------------------------------------------------------------


@dataclass(frozen=True)
class TokenTotals:
    """Tokens summed over every provider attempt of one request."""

    tokens_in: int
    tokens_out: int
    usage_source: UsageSource


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of the LLM step. Exactly one of ``completion`` / ``error`` is set.

    ``attempts`` counts provider calls actually sent (0 when a guard refused locally).
    ``tokens`` carries totals across attempts; when ``None`` the pipeline derives them
    from the single completion.
    """

    completion: CompletionResult | None
    error: ProviderError | None
    stream_opened: bool
    received_chars: int
    retries: int = 0
    attempts: int = 1
    tokens: TokenTotals | None = None

    def __post_init__(self) -> None:
        if (self.completion is None) == (self.error is None):
            raise ValueError("exactly one of completion / error must be set")
        if self.attempts < 0 or not 0 <= self.retries <= max(0, self.attempts - 1):
            raise ValueError("retries must be between 0 and attempts - 1")
        if self.attempts == 0 and self.completion is not None:
            raise ValueError("a completion needs at least one provider attempt")


class AnswerGenerator(Protocol):
    async def generate(
        self, request: ChatRequest, spans: ServerSpanTracker, *,
        deadline_ns: int | None = None,
    ) -> GenerationResult: ...


class PromptBudget(Protocol):
    """Says whether a request fits the current token headroom (ProviderGuard in P06)."""

    def fits(self, request: ChatRequest) -> bool: ...


class SingleCallGenerator:
    """One streamed provider call, buffered, with no retry of any kind."""

    def __init__(self, provider: ChatProvider) -> None:
        self._provider = provider

    async def generate(self, request: ChatRequest, spans: ServerSpanTracker, *,
                       deadline_ns: int | None = None) -> GenerationResult:
        opened = False
        received = 0

        async def observed() -> AsyncIterator[ProviderEvent]:
            nonlocal opened, received
            async for event in self._provider.stream_chat(request):
                if isinstance(event, ResponseOpened):
                    opened = True
                elif isinstance(event, ContentDelta):
                    if event.text:
                        spans.mark_provider_first_token()
                    received += len(event.text)
                elif isinstance(event, ReasoningDelta):
                    received += len(event.text)
                yield event

        try:
            completion = await collect_stream(observed())
        except ProviderError as exc:
            return GenerationResult(None, exc, opened, received)
        return GenerationResult(completion, None, opened, received)


@dataclass(frozen=True)
class _Spend:
    """Provider-side accounting for one request."""

    provider_called: bool = False
    retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usage_source: UsageSource = "none"
    model_id: str | None = None


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CONSERVATIVE_CHARS_PER_TOKEN)


def _spend(result: GenerationResult, request: ChatRequest) -> _Spend:
    if result.attempts == 0:
        return _Spend()  # refused locally: the provider was never called
    if result.tokens is not None:
        return _Spend(provider_called=True, retries=result.retries,
                      tokens_in=result.tokens.tokens_in, tokens_out=result.tokens.tokens_out,
                      usage_source=result.tokens.usage_source, model_id=request.model)
    usage = None if result.completion is None else result.completion.usage
    if usage is not None:
        tokens: tuple[int, int, UsageSource] = (usage.prompt_tokens, usage.completion_tokens,
                                                "provider")
    elif not result.stream_opened:
        tokens = (0, 0, "none")  # rejected before any response: nothing to count
    else:
        tokens = (_estimate_tokens(request.prompt_chars),
                  _estimate_tokens(result.received_chars), "estimated")
    return _Spend(provider_called=True, retries=result.retries, tokens_in=tokens[0],
                  tokens_out=tokens[1], usage_source=tokens[2], model_id=request.model)


@dataclass
class _Run:
    """Mutable per-request state: what was decided and which tools ran."""

    request: PipelineRequest
    scope: ScopeDecision
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)

    def trace(self, *, prompt_chars: int | None = None, raw_answer: str | None = None,
              finish_reason: str | None = None, reasoning_chars: int = 0,
              verification: VerificationResult | None = None,
              provider_error: str | None = None) -> PipelineTrace:
        return PipelineTrace(
            prompt_version=PROMPT_VERSION, scope=self.scope,
            gate_decisions=tuple(self.decisions), tool_calls=tuple(self.tool_calls),
            loop_count=sum(1 for d in self.decisions if d.action == "LOOP_EXPAND"),
            prompt_chars=prompt_chars, raw_answer=raw_answer, finish_reason=finish_reason,
            truncated=finish_reason == "length", reasoning_chars=reasoning_chars,
            verification=verification, provider_error=provider_error,
        )

    def outcome(self, kind: OutcomeKind, *, status: Status = "ok",
                error_code: ErrorCode | None = None,
                verdict: VerificationVerdict = "NOT_RUN", delivered_text: str | None = None,
                refused: bool = False, citations: tuple[CitedPassage, ...] = (),
                passages: tuple[RetrievedPassage, ...] = (), spend: _Spend | None = None,
                trace: PipelineTrace | None = None, degraded: bool = False) -> PipelineOutcome:
        spent = spend or _Spend()
        return PipelineOutcome(
            request_id=self.request.request_id,
            retrieval_setting=self.request.retrieval_setting, kind=kind, status=status,
            error_code=error_code, verification_verdict=verdict, delivered_text=delivered_text,
            refused=refused, citations=citations, passages=passages,
            provider_called=spent.provider_called, retries=spent.retries,
            tokens_in=spent.tokens_in, tokens_out=spent.tokens_out,
            usage_source=spent.usage_source, model_id=spent.model_id, degraded=degraded,
            retrieval_ms=round(float(sum(c.elapsed_ms for c in self.tool_calls)), 3),
            trace=trace or self.trace(),
        )


# --- Pipeline -------------------------------------------------------------------------


class Pipeline:
    def __init__(self, *, gateway: ToolGateway, generator: AnswerGenerator,
                 config: PipelineConfig, top_k: Mapping[str, int],
                 vocabulary: frozenset[str] | None = None,
                 budget: PromptBudget | None = None) -> None:
        missing = gateway.settings - set(top_k)
        if missing:
            raise ValueError(f"top_k missing for retrieval settings: {sorted(missing)}")
        self._gateway = gateway
        self._generator = generator
        self._config = config
        self._top_k = dict(top_k)
        self._vocabulary = vocabulary
        self._budget = budget

    def _build_chat(self, question: str, passages: tuple[RetrievedPassage, ...]) -> ChatRequest:
        return build_request(
            question, passages, model=self._config.model_id,
            max_completion_tokens=self._config.max_completion_tokens,
            temperature=self._config.temperature,
            reasoning_effort=self._config.reasoning_effort,
        )

    def _fit_to_budget(self, question: str, passages: tuple[RetrievedPassage, ...],
                       ) -> tuple[ChatRequest, tuple[RetrievedPassage, ...], bool]:
        """Drop the lowest-ranked passages while token headroom is too tight (degraded)."""
        chat = self._build_chat(question, passages)
        if self._budget is None:
            return chat, passages, False
        kept = passages
        while len(kept) > 1 and not self._budget.fits(chat):
            kept = kept[:-1]
            chat = self._build_chat(question, kept)
        return chat, kept, len(kept) < len(passages)

    async def run(self, request: PipelineRequest, spans: ServerSpanTracker, *,
                  deadline_ns: int | None = None) -> PipelineOutcome:
        if request.retrieval_setting not in self._gateway.settings:
            raise ContractViolationError(
                f"unknown retrieval_setting {request.retrieval_setting!r}"
            )
        spans.switch("working")
        # Step 1: deterministic scope analysis.
        run = _Run(request, analyze_scope(request.question, self._vocabulary))

        # Step 2: the tool call.
        try:
            call = await self._gateway.retrieve_passages({
                "query": request.question,
                "retrieval_setting": request.retrieval_setting,
                "include_references": run.scope.asks_references,
            })
        except ToolError as exc:
            run.tool_calls.append(exc.record)
            return run.outcome("failed", status="error", error_code="internal_error")
        run.tool_calls.append(call.record)
        passages = call.result.passages

        # Step 3: evidence gate, with at most one paper-scoped re-query.
        decision = decide(run.scope, passages, loop_count=0)
        run.decisions.append(decision)
        if decision.action == "LOOP_EXPAND":
            passages = await self._expand(run, decision, passages)
            decision = decide(run.scope, passages, loop_count=1)
            run.decisions.append(decision)
        if decision.action == "EARLY_REFUSAL":
            message = (VERSION_MESSAGE if decision.reason == "version_not_in_corpus"
                       else OUT_OF_CORPUS_MESSAGE)
            return run.outcome("early_refusal", verdict="EARLY_REFUSAL",
                               delivered_text=message, refused=True, passages=passages)

        # Step 4: the only LLM call, buffered.
        chat, passages, degraded = self._fit_to_budget(request.question, passages)
        generated = await self._generator.generate(chat, spans, deadline_ns=deadline_ns)
        spend = _spend(generated, chat)
        if generated.error is not None:
            code = generated.error.error_code
            return run.outcome(
                "failed", status=status_for_error_code(code), error_code=code,
                passages=passages, spend=spend, degraded=degraded,
                trace=run.trace(prompt_chars=chat.prompt_chars,
                                provider_error=sanitize_error(generated.error)),
            )
        completion = generated.completion
        if completion is None:  # excluded by GenerationResult.__post_init__
            raise RuntimeError("generator returned neither a completion nor an error")

        # Step 5: verify before anything is delivered.
        verification = verify(completion.content, passages)
        audit = run.trace(
            prompt_chars=chat.prompt_chars, raw_answer=completion.content,
            finish_reason=completion.finish_reason, reasoning_chars=len(completion.reasoning),
            verification=verification,
        )
        if verification.verdict == "VERIFIED":
            return run.outcome("answer", verdict="VERIFIED",
                               delivered_text=completion.content.strip(),
                               citations=verification.citations, passages=passages,
                               spend=spend, trace=audit, degraded=degraded)
        if verification.verdict == "REFUSAL_VERIFIED":
            return run.outcome("llm_refusal", verdict="REFUSAL_VERIFIED",
                               delivered_text=LLM_REFUSAL_MESSAGE, refused=True,
                               passages=passages, spend=spend, trace=audit, degraded=degraded)
        return run.outcome("safe_refusal", verdict="BLOCKED",
                           error_code="citation_verification_failed",
                           delivered_text=SAFE_REFUSAL_MESSAGE, refused=True,
                           passages=passages, spend=spend, trace=audit, degraded=degraded)

    async def _expand(self, run: _Run, decision: GateDecision,
                      passages: tuple[RetrievedPassage, ...]) -> tuple[RetrievedPassage, ...]:
        """The single paper-scoped re-query. If it fails, keep the first result."""
        try:
            call = await self._gateway.retrieve_passages({
                "query": decision.requery or run.request.question,
                "retrieval_setting": run.request.retrieval_setting,
                "paper_filter": decision.paper_filter,
                "include_references": run.scope.asks_references,
            })
        except ToolError as exc:
            run.tool_calls.append(exc.record)
            return passages
        run.tool_calls.append(call.record)
        return merge_expansion(passages, call.result.passages,
                               self._top_k[run.request.retrieval_setting])


__all__ = [
    "LLM_REFUSAL_MESSAGE",
    "OUT_OF_CORPUS_MESSAGE",
    "SAFE_REFUSAL_MESSAGE",
    "VERSION_MESSAGE",
    "AnswerGenerator",
    "GenerationResult",
    "Pipeline",
    "PipelineConfig",
    "PipelineOutcome",
    "PipelineRequest",
    "PipelineTrace",
    "PromptBudget",
    "SingleCallGenerator",
    "TokenTotals",
]
