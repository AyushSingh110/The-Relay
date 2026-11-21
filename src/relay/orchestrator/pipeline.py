"""The request pipeline (ARCHITECTURE.md sections 5-7)."""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.control.pricing import ROUTE_NOTE, RouteNote, RouteRecord
from relay.errors import ContractViolationError, sanitize_error
from relay.ingestion.v1_corpus import V1_CUE_SET
from relay.orchestrator.evidence_gate import EvidenceGate, GateDecision, merge_expansion
from relay.orchestrator.messages import RefusalMessages, refusal_messages
from relay.orchestrator.prompt import PROMPT_VERSION, build_request, prompt_version
from relay.orchestrator.streaming import SentenceStream, StoppedEvent, StreamingVerifier
from relay.orchestrator.tools import PassageRetriever, ToolCallRecord, ToolError, ToolGateway
from relay.orchestrator.verifier import CitedPassage, VerificationResult, Verifier
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
from relay.retrieval.cues import CueSet, DocumentCues
from relay.retrieval.hybrid_index import RetrievedPassage
from relay.retrieval.scope import ScopeAnalyser, ScopeDecision
from relay.schemas import ErrorCode, Status, UsageSource, VerificationVerdict
from relay.telemetry.spans import ServerSpanTracker

DEFAULT_MAX_COMPLETION_TOKENS: Final = 512  # [PROPOSED]; P02's 256 was never reached
MAX_QUESTION_CHARS: Final = 2000

# The v1 messages, rendered from the v1 cue set (byte-identical to v1; see
# tests/test_collection_aware_safety.py). A collection's messages name its own documents.
_V1_MESSAGES: Final = refusal_messages(V1_CUE_SET)
LLM_REFUSAL_MESSAGE: Final = _V1_MESSAGES.llm_refusal
OUT_OF_CORPUS_MESSAGE: Final = _V1_MESSAGES.out_of_corpus
VERSION_MESSAGE: Final = _V1_MESSAGES.version
SAFE_REFUSAL_MESSAGE: Final = _V1_MESSAGES.safe_refusal

# "partial" (V2-05 streaming): verified sentences were delivered, then the answer stopped.
OutcomeKind = Literal["answer", "llm_refusal", "early_refusal", "safe_refusal", "failed",
                      "partial"]
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


# --- Contracts ------------------------------------------------------------------------


class PipelineRequest(BaseModel):
    model_config = _STRICT

    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
    retrieval_setting: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    # V2-03: answer from this collection; None = the v1 three-paper path (unchanged).
    collection_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None

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


class StreamStop(BaseModel):
    """Why a streamed answer stopped (V2-05): the failed check, not only its class."""

    model_config = _STRICT

    reason_class: str
    sentence_index: int | None
    failure_codes: tuple[str, ...]


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
    stream_stop: StreamStop | None = None  # streaming only: the sentence that failed


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
    # V2-09: the lane(s) that served it and what each call cost (None: no model call, or a
    # generator without a router, as in some tests)
    route: RouteRecord | None = None
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
    """Outcome of the LLM step."""

    completion: CompletionResult | None
    error: ProviderError | None
    stream_opened: bool
    received_chars: int
    retries: int = 0
    attempts: int = 1
    tokens: TokenTotals | None = None
    route: RouteRecord | None = None  # V2-09: set by the router

    def __post_init__(self) -> None:
        if (self.completion is None) == (self.error is None):
            raise ValueError("exactly one of completion / error must be set")
        if self.attempts < 0 or not 0 <= self.retries <= max(0, self.attempts - 1):
            raise ValueError("retries must be between 0 and attempts - 1")
        if self.attempts == 0 and self.completion is not None:
            raise ValueError("a completion needs at least one provider attempt")


ContentSink = Callable[[str], None]


class AnswerGenerator(Protocol):
    async def generate(
        self, request: ChatRequest, spans: ServerSpanTracker, *,
        deadline_ns: int | None = None, on_content: ContentSink | None = None,
    ) -> GenerationResult:
        """``on_content`` (V2-05 streaming) receives each content piece as it arrives."""
        ...


@runtime_checkable
class Escalating(Protocol):
    """A generator that can retry a truncated or empty answer once on a stronger lane
    (V2-09). ``escalation`` returns the generator for that one retry, or None (no
    escalation lane, it cannot admit, or the retry budget is spent)."""

    def escalation(self, result: GenerationResult,
                   request: ChatRequest) -> AnswerGenerator | None: ...


def escalation_reason(result: GenerationResult | None) -> str | None:
    """Why this result deserves one escalation retry, or None.

    ``finish_reason == "length"`` is a truncated answer; with no content and some reasoning
    it is the v1 gpt-oss case (the whole budget spent reasoning). An empty answer that ended
    normally is escalated too: it can never be verified."""
    if result is None or result.completion is None:
        return None
    completion = result.completion
    empty = not completion.content.strip()
    if completion.finish_reason == "length":
        return "reasoning_exhausted" if empty and completion.reasoning else "truncated"
    return "empty" if empty else None


def _sources(*totals: TokenTotals | None) -> UsageSource:
    seen = {t.usage_source for t in totals if t is not None}
    return "estimated" if "estimated" in seen else "provider" if "provider" in seen else "none"


def merge_escalation(prior: GenerationResult, second: GenerationResult, reason: str, *,
                     keep_prior_on_failure: bool = True) -> GenerationResult:
    """One result for a generation and its escalation retry: the retry's answer, every
    call's tokens and cost. Buffered: when the retry failed, the first answer is kept (it is
    still verified as usual). Streaming (``keep_prior_on_failure=False``): the retry's own
    failure is the outcome, since the first answer's text is not what the stream holds."""
    a, b = prior.tokens, second.tokens
    tokens = TokenTotals((a.tokens_in if a else 0) + (b.tokens_in if b else 0),
                         (a.tokens_out if a else 0) + (b.tokens_out if b else 0),
                         _sources(a, b))
    first, then = prior.route, second.route
    uses = (first.uses if first else ()) + (then.uses if then else ())
    keep_prior = keep_prior_on_failure and second.error is not None
    chosen = prior if keep_prior else second
    served = first if keep_prior or then is None or then.lane is None else then
    route = RouteRecord(
        lane=None if served is None else served.lane,
        provider=None if served is None else served.provider,
        model=None if served is None else served.model,
        fallback=served is not None and served.fallback, escalated=True,
        escalation_reason=reason, uses=uses,
        skipped=(first.skipped if first else ()) + (then.skipped if then else ()))
    attempts = prior.attempts + second.attempts
    retries = prior.retries + second.retries + (1 if second.attempts else 0)
    return GenerationResult(chosen.completion, chosen.error, chosen.stream_opened,
                            chosen.received_chars, retries=retries, attempts=attempts,
                            tokens=tokens, route=route)


class PromptBudget(Protocol):
    """Says whether a request fits the current token headroom (ProviderGuard in P06)."""

    def fits(self, request: ChatRequest) -> bool: ...


class SingleCallGenerator:
    """One streamed provider call, buffered, with no retry of any kind."""

    def __init__(self, provider: ChatProvider) -> None:
        self._provider = provider

    async def generate(self, request: ChatRequest, spans: ServerSpanTracker, *,
                       deadline_ns: int | None = None,
                       on_content: ContentSink | None = None,
                       on_received: Callable[[int], None] | None = None) -> GenerationResult:
        """``on_received`` (V2-14) is told the size of every content and reasoning piece as
        it arrives, so a caller cancelled mid-stream still knows what was generated."""
        opened = False
        received = 0

        async def observed() -> AsyncIterator[ProviderEvent]:
            nonlocal opened, received
            async for event in self._provider.stream_chat(request):
                if isinstance(event, ResponseOpened):
                    opened = True
                elif isinstance(event, ContentDelta):
                    received += len(event.text)
                    if on_received is not None and event.text:
                        on_received(len(event.text))
                    if event.text:
                        spans.mark_provider_first_token()
                        if on_content is not None:
                            on_content(event.text)
                elif isinstance(event, ReasoningDelta):
                    received += len(event.text)
                    if on_received is not None and event.text:
                        on_received(len(event.text))
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
    route: RouteRecord | None = None


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CONSERVATIVE_CHARS_PER_TOKEN)


def _spend(result: GenerationResult, request: ChatRequest) -> _Spend:
    route = result.route
    model = request.model if route is None or route.model is None else route.model
    if result.attempts == 0:
        return _Spend(route=route)  # refused locally: the provider was never called
    if result.tokens is not None:
        return _Spend(provider_called=True, retries=result.retries,
                      tokens_in=result.tokens.tokens_in, tokens_out=result.tokens.tokens_out,
                      usage_source=result.tokens.usage_source, model_id=model, route=route)
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
                  tokens_out=tokens[1], usage_source=tokens[2], model_id=model, route=route)


@dataclass
class _Run:
    """Mutable per-request state: what was decided and which tools ran."""

    request: PipelineRequest
    scope: ScopeDecision
    prompt_version: str = PROMPT_VERSION
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)

    def trace(self, *, prompt_chars: int | None = None, raw_answer: str | None = None,
              finish_reason: str | None = None, reasoning_chars: int = 0,
              verification: VerificationResult | None = None,
              provider_error: str | None = None,
              stream_stop: StreamStop | None = None) -> PipelineTrace:
        return PipelineTrace(
            prompt_version=self.prompt_version, scope=self.scope,
            gate_decisions=tuple(self.decisions), tool_calls=tuple(self.tool_calls),
            loop_count=sum(1 for d in self.decisions if d.action == "LOOP_EXPAND"),
            prompt_chars=prompt_chars, raw_answer=raw_answer, finish_reason=finish_reason,
            truncated=finish_reason == "length", reasoning_chars=reasoning_chars,
            verification=verification, provider_error=provider_error,
            stream_stop=stream_stop,
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
            route=spent.route,
            retrieval_ms=round(float(sum(c.elapsed_ms for c in self.tool_calls)), 3),
            trace=trace or self.trace(),
        )


# --- Collections (V2-03) ---------------------------------------------------------------


@dataclass(frozen=True)
class CollectionResources:
    """Everything the pipeline needs to answer from one collection."""

    collection_id: str
    version: int
    setting_name: str
    retriever: PassageRetriever
    top_k: int
    vocabulary: frozenset[str] | None
    cues: CueSet
    paper_ids: frozenset[str] = frozenset()  # the documents in the collection's index
    # the tenant's documents in other collections: their names are blocked (isolation)
    foreign: tuple[DocumentCues, ...] = ()
    safety: CorpusSafety | None = None  # prebuilt by the resolver (cached); else built here


@dataclass(frozen=True)
class CorpusSafety:
    """The v1 safety logic, bound to one corpus's cue set (V2-04): scope analysis, the
    evidence gate, the verifier (with foreign names), the refusal messages and the prompt.

    The v1 path builds it once, when the pipeline is created. A collection request builds
    it per request from the resolver's cue set (cached per collection version) and the
    tenant's foreign documents (read per request, since other collections change)."""

    cues: CueSet
    analyser: ScopeAnalyser
    gate: EvidenceGate
    verifier: Verifier
    messages: RefusalMessages
    prompt_version: str

    @classmethod
    def for_cues(cls, cues: CueSet, *, foreign: tuple[DocumentCues, ...] = ()) -> CorpusSafety:
        analyser = ScopeAnalyser(cues)
        return cls(cues=cues, analyser=analyser, gate=EvidenceGate(cues, analyser=analyser),
                   verifier=Verifier(cues, foreign=foreign), messages=refusal_messages(cues),
                   prompt_version=prompt_version(cues))


class CollectionResolver(Protocol):
    """Resolves a collection id (``relay.app.collections.StoreCollectionResolver``); raises
    ``CollectionNotFoundError`` / ``CollectionNotReadyError``."""

    async def resolve(self, collection_id: str) -> CollectionResources: ...


@dataclass(frozen=True)
class _Corpus:
    """What one request answers from: the v1 corpus or a collection."""

    gateway: ToolGateway
    top_k: Mapping[str, int]
    vocabulary: frozenset[str] | None
    safety: CorpusSafety


# --- Streaming plumbing (V2-05) ---------------------------------------------------------


@dataclass
class StreamPlan:
    """What steps 1-3 decided for one request: an early outcome, or the prompt to run."""

    run: _Run
    safety: CorpusSafety
    passages: tuple[RetrievedPassage, ...]
    early: PipelineOutcome | None = None
    chat: ChatRequest | None = None
    degraded: bool = False
    streaming_verifier: StreamingVerifier = field(init=False)

    def __post_init__(self) -> None:
        self.streaming_verifier = StreamingVerifier(self.safety.verifier)

    @property
    def request(self) -> PipelineRequest:
        return self.run.request


_END: Final = object()


class TokenStream:
    """The model's content pieces as an async iterator. The model call runs in its own
    task; ``aclose`` cancels it (generation stops; what was spent is estimated)."""

    def __init__(self, generator: AnswerGenerator, chat: ChatRequest,
                 spans: ServerSpanTracker, deadline_ns: int | None, *,
                 prior: GenerationResult | None = None, reason: str | None = None) -> None:
        self._generator = generator
        self._chat = chat
        self._spans = spans
        self._deadline_ns = deadline_ns
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._task: asyncio.Task[GenerationResult] | None = None
        self.received_chars = 0
        self.cancelled = False
        # V2-09: an escalation retry carries the generation it replaces (its tokens and
        # cost count too) and why it was made; the router notes the lane it picks.
        self.prior = prior
        self.reason = reason
        self.note = RouteNote()

    def _push(self, text: str) -> None:
        self.received_chars += len(text)
        self._queue.put_nowait(text)

    async def _produce(self) -> GenerationResult:
        ROUTE_NOTE.set(self.note)  # this task's own context: the router fills it in
        try:
            return await self._generator.generate(self._chat, self._spans,
                                                  deadline_ns=self._deadline_ns,
                                                  on_content=self._push)
        finally:
            self._queue.put_nowait(_END)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        if self._task is None:
            self._task = asyncio.create_task(self._produce(), name="relay-generation")
        while True:
            item = await self._queue.get()
            if item is _END:
                break
            yield str(item)
        await self._task  # an unexpected generator exception surfaces here

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self.cancelled = True
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the caller itself is being cancelled: let that through

    @property
    def failed(self) -> bool:
        """The model call ended with a provider error (its output is incomplete)."""
        result = self.result
        return result is not None and result.error is not None

    @property
    def result(self) -> GenerationResult | None:
        """The generation result (with an escalation, both calls merged); None while running
        or when it was cancelled."""
        own = self._own_result
        if own is None or self.prior is None:
            return own
        return merge_escalation(self.prior, own, self.reason or "escalated",
                                keep_prior_on_failure=False)

    @property
    def _own_result(self) -> GenerationResult | None:
        if self._task is None or not self._task.done() or self._task.cancelled():
            return None
        return self._task.result()

    @property
    def truncated(self) -> bool:
        """The model stopped at the completion-token limit (its last words are cut off)."""
        own = self._own_result
        return (own is not None and own.completion is not None
                and own.completion.finish_reason == "length")

    @property
    def escalation_reason(self) -> str | None:
        """Why this stream may be escalated; None for an escalation retry itself (exactly
        one escalation per request)."""
        return None if self.prior is not None else escalation_reason(self._own_result)

    def estimated_route(self, tokens_in: int, tokens_out: int) -> RouteRecord | None:
        """The route of a stream cancelled mid-way: the noted lane, with estimated tokens
        (and the replaced generation's calls, for an escalation retry)."""
        use = self.note.use(tokens_in, tokens_out, "estimated")
        prior = None if self.prior is None else self.prior.route
        if use is None and prior is None:
            return None
        uses = (prior.uses if prior else ()) + ((use,) if use else ())
        return RouteRecord(lane=self.note.lane, provider=self.note.provider,
                           model=self.note.model, fallback=self.note.fallback,
                           escalated=self.prior is not None, escalation_reason=self.reason,
                           uses=uses, skipped=tuple(self.note.skipped))


def _merged_citations(sentences: tuple[Any, ...]) -> tuple[CitedPassage, ...]:
    seen: dict[int, CitedPassage] = {}
    for sentence in sentences:
        for citation in sentence.citations:
            seen.setdefault(citation.tag, citation)
    return tuple(seen.values())


# --- Pipeline -------------------------------------------------------------------------


class Pipeline:
    def __init__(self, *, gateway: ToolGateway, generator: AnswerGenerator,
                 config: PipelineConfig, top_k: Mapping[str, int],
                 vocabulary: frozenset[str] | None = None,
                 budget: PromptBudget | None = None,
                 collections: CollectionResolver | None = None) -> None:
        missing = gateway.settings - set(top_k)
        if missing:
            raise ValueError(f"top_k missing for retrieval settings: {sorted(missing)}")
        self._gateway = gateway
        self._generator = generator
        self._config = config
        self._top_k = dict(top_k)
        self._vocabulary = vocabulary
        self._budget = budget
        self._collections = collections
        self._v1_safety = CorpusSafety.for_cues(V1_CUE_SET)

    def with_generator(self, generator: AnswerGenerator) -> Pipeline:
        """The same pipeline on another generator (V2-12: an eval run pinned to one lane)."""
        twin = copy.copy(self)
        twin._generator = generator
        return twin

    @property
    def generator(self) -> AnswerGenerator:
        """The guarded generator every model call goes through (V2-08: the rewriter uses it,
        so its call obeys the same ProviderGuard limits and real-call budget)."""
        return self._generator

    async def _corpus(self, request: PipelineRequest) -> tuple[PipelineRequest, _Corpus]:
        """The v1 corpus, or the request's collection (its retriever, cues, vocabulary)."""
        if request.collection_id is None:
            return request, _Corpus(self._gateway, self._top_k, self._vocabulary,
                                    self._v1_safety)
        if self._collections is None:
            raise ContractViolationError("this pipeline has no collection resolver")
        res = await self._collections.resolve(request.collection_id)
        corpus = _Corpus(self._gateway.with_retrievers({res.setting_name: res.retriever},
                                                       paper_ids=res.paper_ids),
                         {res.setting_name: res.top_k}, res.vocabulary,
                         res.safety if res.safety is not None
                         else CorpusSafety.for_cues(res.cues, foreign=res.foreign))
        return request.model_copy(update={"retrieval_setting": res.setting_name}), corpus

    def _build_chat(self, question: str, passages: tuple[RetrievedPassage, ...],
                    cues: CueSet) -> ChatRequest:
        return build_request(
            question, passages, model=self._config.model_id,
            max_completion_tokens=self._config.max_completion_tokens,
            temperature=self._config.temperature,
            reasoning_effort=self._config.reasoning_effort, cues=cues,
        )

    def _fit_to_budget(self, question: str, passages: tuple[RetrievedPassage, ...],
                       cues: CueSet) -> tuple[ChatRequest, tuple[RetrievedPassage, ...], bool]:
        """Drop the lowest-ranked passages while token headroom is too tight (degraded)."""
        chat = self._build_chat(question, passages, cues)
        if self._budget is None:
            return chat, passages, False
        kept = passages
        while len(kept) > 1 and not self._budget.fits(chat):
            kept = kept[:-1]
            chat = self._build_chat(question, kept, cues)
        return chat, kept, len(kept) < len(passages)

    async def run(self, request: PipelineRequest, spans: ServerSpanTracker, *,
                  deadline_ns: int | None = None) -> PipelineOutcome:
        """The buffered path: steps 1-3, one model call, the whole-answer verifier."""
        plan = await self._prepare(request, spans)
        if plan.early is not None:
            return plan.early
        return await self.complete_buffered(plan, spans, deadline_ns=deadline_ns)

    async def complete_buffered(self, plan: StreamPlan, spans: ServerSpanTracker, *,
                                deadline_ns: int | None = None) -> PipelineOutcome:
        """Steps 4-5 of ``run`` on a prepared plan (``POST /v1/ask`` buffered mode)."""
        if plan.early is not None:
            return plan.early
        run, safety, passages, degraded = plan.run, plan.safety, plan.passages, plan.degraded
        chat = plan.chat
        if chat is None:  # set whenever there is no early outcome
            raise RuntimeError("a plan without an early outcome has no prompt")

        # Step 4: the only LLM call, buffered.
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
        verification = safety.verifier.verify(completion.content, passages)
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
                               delivered_text=safety.messages.llm_refusal, refused=True,
                               passages=passages, spend=spend, trace=audit, degraded=degraded)
        return run.outcome("safe_refusal", verdict="BLOCKED",
                           error_code="citation_verification_failed",
                           delivered_text=safety.messages.safe_refusal, refused=True,
                           passages=passages, spend=spend, trace=audit, degraded=degraded)

    # --- Streaming mode (V2-05) ----------------------------------------------------------

    async def prepare_streaming(self, request: PipelineRequest,
                                spans: ServerSpanTracker) -> StreamPlan:
        """Steps 1-3 (scope, retrieval, gate) and the prompt, exactly as ``run`` does."""
        return await self._prepare(request, spans)

    def run_streaming(self, plan: StreamPlan, spans: ServerSpanTracker, *,
                      deadline_ns: int | None = None) -> TokenStream:
        """The model call as a token stream. Closing it (``aclose``) cancels generation.
        Verification is not done here: ``SentenceStream`` checks each sentence and
        ``complete_streaming`` runs the whole-answer pass."""
        if plan.chat is None:
            raise ContractViolationError("this plan ended early; there is nothing to generate")
        return TokenStream(self._generator, plan.chat, spans, deadline_ns)

    def escalate_streaming(self, plan: StreamPlan, tokens: TokenStream,
                           spans: ServerSpanTracker, *,
                           deadline_ns: int | None = None) -> TokenStream | None:
        """The one escalation retry of a streamed answer that ended truncated or empty
        (V2-09), or None. The caller only asks when no sentence was delivered yet."""
        reason = tokens.escalation_reason
        first = tokens.result
        if reason is None or first is None or plan.chat is None:
            return None
        if not isinstance(self._generator, Escalating):
            return None
        generator = self._generator.escalation(first, plan.chat)
        if generator is None:
            return None
        return TokenStream(generator, plan.chat, spans, deadline_ns, prior=first,
                           reason=reason)

    async def complete_streaming(self, plan: StreamPlan, tokens: TokenStream,
                                 stream: SentenceStream, *, stop: StoppedEvent | None,
                                 refused: bool) -> PipelineOutcome:
        """The outcome of a streamed answer. When every sentence passed, the v1 whole-answer
        verifier runs as the final pass; only then is the answer ``VERIFIED``."""
        run, safety, passages, chat = plan.run, plan.safety, plan.passages, plan.chat
        if chat is None:
            raise ContractViolationError("this plan ended early; there is nothing to complete")
        await tokens.aclose()
        result = tokens.result
        if result is None:  # cancelled after a failed sentence: estimate what was spent
            tokens_in = _estimate_tokens(chat.prompt_chars)
            tokens_out = _estimate_tokens(tokens.received_chars)
            route = tokens.estimated_route(tokens_in, tokens_out)
            if route is not None:  # an escalation retry: the replaced call counts too
                tokens_in, tokens_out = route.tokens_in, route.tokens_out
            spend = _Spend(provider_called=True, tokens_in=tokens_in, tokens_out=tokens_out,
                           usage_source="estimated",
                           model_id=chat.model if route is None or route.model is None
                           else route.model, route=route)
        else:
            spend = _spend(result, chat)
        released = tuple(stream.sentences)
        delivered = " ".join(s.text for s in released) or None
        citations = _merged_citations(released)
        completion = None if result is None else result.completion
        audit_args: dict[str, object] = {
            "prompt_chars": chat.prompt_chars, "raw_answer": stream.text or None,
            "finish_reason": None if completion is None else completion.finish_reason,
            "reasoning_chars": 0 if completion is None else len(completion.reasoning)}
        common = {"passages": passages, "spend": spend, "degraded": plan.degraded}
        # A partial that ends in a failure (client gone, provider error) records the failure
        # status and no delivered text (v1's rule: status ok exactly when text is delivered);
        # the sentences already streamed stay with the client and in the trace.
        if stop is None and not refused and not stream.completed:
            # The consumer stopped reading (the client left): never a complete answer.
            return run.outcome("partial" if released else "failed",
                               status=status_for_error_code("client_gave_up"),
                               error_code="client_gave_up",
                               trace=run.trace(**audit_args),  # type: ignore[arg-type]
                               **common)  # type: ignore[arg-type]
        if result is not None and result.error is not None:
            code = result.error.error_code
            trace = run.trace(**audit_args, provider_error=sanitize_error(result.error))  # type: ignore[arg-type]
            return run.outcome("partial" if released else "failed",
                               status=status_for_error_code(code), error_code=code,
                               trace=trace, **common)  # type: ignore[arg-type]
        # the whole-answer pass; skipped when a sentence already failed
        verification = None if stop is not None else safety.verifier.verify(stream.text,
                                                                             passages)
        stream_stop = None if stop is None else StreamStop(
            reason_class=stop.reason_class, sentence_index=stop.sentence_index,
            failure_codes=tuple(f.code for f in stop.failures))
        trace = run.trace(**audit_args, verification=verification,  # type: ignore[arg-type]
                          stream_stop=stream_stop)
        if verification is not None and verification.verdict == "VERIFIED" and stop is None:
            return run.outcome("answer", verdict="VERIFIED", delivered_text=stream.text.strip(),
                               citations=verification.citations, trace=trace,
                               **common)  # type: ignore[arg-type]
        if refused and verification is not None and verification.verdict == "REFUSAL_VERIFIED":
            return run.outcome("llm_refusal", verdict="REFUSAL_VERIFIED",
                               delivered_text=safety.messages.llm_refusal, refused=True,
                               trace=trace, **common)  # type: ignore[arg-type]
        if released:  # stopped mid-answer, or the final pass failed after every sentence
            return run.outcome("partial", verdict="BLOCKED",
                               error_code="citation_verification_failed",
                               delivered_text=delivered, citations=citations, trace=trace,
                               **common)  # type: ignore[arg-type]
        return run.outcome("safe_refusal", verdict="BLOCKED",
                           error_code="citation_verification_failed",
                           delivered_text=safety.messages.safe_refusal, refused=True,
                           trace=trace, **common)  # type: ignore[arg-type]

    async def _prepare(self, request: PipelineRequest, spans: ServerSpanTracker) -> StreamPlan:
        """Steps 1-3 and the prompt, shared by the buffered and the streaming paths."""
        request, corpus = await self._corpus(request)
        if request.retrieval_setting not in corpus.gateway.settings:
            raise ContractViolationError(
                f"unknown retrieval_setting {request.retrieval_setting!r}"
            )
        spans.switch("working")
        # Step 1: deterministic scope analysis.
        safety = corpus.safety
        run = _Run(request, safety.analyser.analyze(request.question, corpus.vocabulary),
                   prompt_version=safety.prompt_version)

        # Step 2: the tool call.
        try:
            call = await corpus.gateway.retrieve_passages({
                "query": request.question,
                "retrieval_setting": request.retrieval_setting,
                "include_references": run.scope.asks_references,
            })
        except ToolError as exc:
            run.tool_calls.append(exc.record)
            return StreamPlan(run, safety, (), early=run.outcome(
                "failed", status="error", error_code="internal_error"))
        run.tool_calls.append(call.record)
        passages = call.result.passages

        # Step 3: evidence gate, with at most one paper-scoped re-query.
        decision = safety.gate.decide(run.scope, passages, loop_count=0)
        run.decisions.append(decision)
        if decision.action == "LOOP_EXPAND":
            passages = await self._expand(run, decision, passages, corpus)
            decision = safety.gate.decide(run.scope, passages, loop_count=1)
            run.decisions.append(decision)
        if decision.action == "EARLY_REFUSAL":
            message = (safety.messages.version if decision.reason == "version_not_in_corpus"
                       else safety.messages.out_of_corpus)
            return StreamPlan(run, safety, passages, early=run.outcome(
                "early_refusal", verdict="EARLY_REFUSAL", delivered_text=message,
                refused=True, passages=passages))

        # The prompt (step 4's input), trimmed to the token headroom when needed.
        chat, passages, degraded = self._fit_to_budget(request.question, passages,
                                                       safety.cues)
        return StreamPlan(run, safety, passages, chat=chat, degraded=degraded)

    async def _expand(self, run: _Run, decision: GateDecision,
                      passages: tuple[RetrievedPassage, ...],
                      corpus: _Corpus) -> tuple[RetrievedPassage, ...]:
        """The single paper-scoped re-query. If it fails, keep the first result."""
        try:
            call = await corpus.gateway.retrieve_passages({
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
                               corpus.top_k[run.request.retrieval_setting])


__all__ = [
    "LLM_REFUSAL_MESSAGE",
    "OUT_OF_CORPUS_MESSAGE",
    "SAFE_REFUSAL_MESSAGE",
    "VERSION_MESSAGE",
    "AnswerGenerator",
    "ContentSink",
    "CollectionResolver",
    "Escalating",
    "CorpusSafety",
    "StreamPlan",
    "StreamStop",
    "TokenStream",
    "CollectionResources",
    "GenerationResult",
    "Pipeline",
    "PipelineConfig",
    "PipelineOutcome",
    "PipelineRequest",
    "PipelineTrace",
    "PromptBudget",
    "SingleCallGenerator",
    "TokenTotals",
    "escalation_reason",
    "merge_escalation",
]
