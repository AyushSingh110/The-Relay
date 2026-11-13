"""P05 tests: tool gateway, evidence gate, prompt, verifier and the pipeline.

Fully offline: retrieval is a scripted stub and the model is a ``ScriptedProvider``.
Passages, answers and questions here are synthetic fixtures, not evaluation content.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Collection, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from relay.errors import ContractViolationError
from relay.orchestrator.evidence_gate import (
    RERANK_REFUSAL_SCORE,
    decide,
    expansion_size,
    expected_papers,
    merge_expansion,
    requery_text,
)
from relay.orchestrator.pipeline import (
    LLM_REFUSAL_MESSAGE,
    OUT_OF_CORPUS_MESSAGE,
    SAFE_REFUSAL_MESSAGE,
    VERSION_MESSAGE,
    GenerationResult,
    Pipeline,
    PipelineConfig,
    PipelineOutcome,
    PipelineRequest,
    SingleCallGenerator,
)
from relay.orchestrator.prompt import PROMPT_VERSION, REFUSAL_MARKER, build_messages
from relay.orchestrator.tools import (
    ToolExecutionError,
    ToolGateway,
    ToolInputError,
    ToolTimeoutError,
)
from relay.orchestrator.verifier import split_sentences, verify
from relay.providers.base import (
    ChatRequest,
    ProviderEvent,
    ScriptedProvider,
    ScriptedReply,
    TokenUsage,
)
from relay.providers.errors import ProviderRateLimitError, ProviderTimeoutError
from relay.retrieval.hybrid_index import RetrievalResult, RetrievedPassage
from relay.retrieval.scope import analyze_scope
from relay.schemas import ResultRow
from relay.telemetry.spans import ServerSpanTracker, SpanSummary

ATTENTION, MOSHI, HNET = "1706.03762v7", "2410.00037v1", "2507.07955v2"
RUN_ID = "exp-20261113T080000Z-abc1234"
USAGE = TokenUsage(prompt_tokens=900, completion_tokens=40, total_tokens=940)


def passage(text: str, *, paper: str = ATTENTION, page: int = 3, page_end: int | None = None,
            number: str | None = "3.1", title: str = "Encoder and Decoder Stacks",
            rerank: float | None = None, chunk_id: str | None = None,
            is_reference: bool = False, rank: int = 1) -> RetrievedPassage:
    return RetrievedPassage(
        rank=rank, chunk_id=chunk_id or f"{paper}_p{page}_c{abs(hash(text)) % 997}",
        paper_id=paper, arxiv_id=paper.rsplit("v", 1)[0], version=int(paper.rsplit("v", 1)[1]),
        short_name="x", section_number=number, section_title=title,
        section_path=(title,), section_kind="references" if is_reference else "body",
        page_start=page, page_end=page_end or page, is_reference=is_reference, text=text,
        bm25_rank=rank, dense_rank=rank, rrf_score=0.03, rerank_score=rerank,
        final_score=0.03 if rerank is None else rerank, reference_downweighted=False,
    )


P1 = passage("The encoder is composed of a stack of N = 6 identical layers. All sub-layers "
             "produce outputs of dimension d_model = 512.", chunk_id="A_p3_c0")
P2 = passage("In this work we employ h = 8 parallel attention layers, or heads. For each of "
             "these we use d_k = d_v = d_model/h = 64.", page=4, page_end=5, number="3.2.2",
             title="Multi-Head Attention", chunk_id="A_p4_c1", rank=2)
P3 = passage("With Q = 8 codebooks at a frame rate of 12.5 Hz one would need 30,000 timesteps.",
             paper=MOSHI, page=12, number="3.4", title="Generative audio modeling",
             chunk_id="M_p12_c0", rank=3)
P4 = passage("Deep-Att + PosUnk Ensemble costs 8.0 · 10^20 FLOPs, following Vaswani et al. and "
             "the encoding PEpos.", paper=HNET, page=8, number="2.3", title="Improved Techniques",
             chunk_id="H_p8_c0", rank=4)
PASSAGES = (P1, P2, P3, P4)
GOOD_ANSWER = ("The encoder has N = 6 identical layers and d_model = 512 [P1]. "
               "The model uses h = 8 heads with d_k = 64 [P2].")


# --- Verifier -----------------------------------------------------------------------


def test_cited_grounded_answer_is_verified() -> None:
    result = verify(GOOD_ANSWER, PASSAGES)
    assert result.verdict == "VERIFIED" and result.failures == ()
    assert [(c.tag, c.chunk_id, c.page_start, c.page_end) for c in result.citations] == [
        (1, "A_p3_c0", 3, 3), (2, "A_p4_c1", 4, 5)]
    assert (result.sentences, result.claim_sentences) == (2, 2)


@pytest.mark.parametrize(
    ("answer", "check", "code"),
    [
        ("The encoder has N = 6 identical layers [P7].", "b", "unknown_passage_tag"),
        ("The encoder has N = 6 identical layers [P1]. It is very deep.", "a",
         "uncited_sentence"),
        ("The encoder has six identical layers.", "a", "no_citations"),
        ("The encoder uses d_model = 1024 [P1].", "d", "ungrounded_number"),
        ("The inner layer has d_ff = 512 [P1].", "d", "ungrounded_identifier"),
        ("INSUFFICIENT_EVIDENCE, although the encoder has N = 6 layers [P1].", "e",
         "refusal_with_content"),
        ("Moshi stacks N = 6 identical layers [P1].", "c", "paper_mismatch"),
        ("According to arXiv:1706.03762v5 the encoder has N = 6 layers [P1].", "c",
         "arxiv_mismatch"),
        ("On page 9 the encoder is given N = 6 layers [P1].", "c", "page_mismatch"),
        ("", "a", "empty_answer"),
        ("   \n  ", "a", "empty_answer"),
    ],
)
def test_blocking_checks(answer: str, check: str, code: str) -> None:
    result = verify(answer, PASSAGES)
    assert result.verdict == "BLOCKED" and result.citations == ()
    assert (check, code) in {(f.check, f.code) for f in result.failures}


@pytest.mark.parametrize("answer", ["INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE.",
                                    "  insufficient evidence \n", "**INSUFFICIENT_EVIDENCE**"])
def test_exact_refusal_is_verified(answer: str) -> None:
    result = verify(answer, PASSAGES)
    assert result.verdict == "REFUSAL_VERIFIED" and result.passed


def test_metadata_mentions_that_match_the_citation_pass() -> None:
    answer = ("In arXiv:1706.03762v7, on page 3, the encoder has N = 6 layers [P1]. "
              "Vaswani et al. are followed for the 8.0 · 10^20 FLOPs figure [P4].")
    assert verify(answer, PASSAGES).verdict == "VERIFIED"  # 'Vaswani' is in P4's own text


def test_number_and_identifier_normalisation() -> None:
    assert verify("It needs 30000 timesteps at 12.50 Hz with Q = 8 codebooks [P3].",
                  PASSAGES).verdict == "VERIFIED"
    assert verify("The ensemble costs 8.0 × 10^{20} FLOPs [P4].", PASSAGES).verdict == "VERIFIED"
    assert verify("The encoding PE_pos is used [P4].", PASSAGES).verdict == "VERIFIED"
    assert verify("Outputs have dimension $d_{model}$ = 512 [P1].", PASSAGES).verdict == "VERIFIED"
    # 51 is not the same number as 512, and 8 is not grounded by 8.0 in another passage
    assert verify("Outputs have dimension 51 [P1].", PASSAGES).verdict == "BLOCKED"
    assert verify("There are 8 layers [P1].", PASSAGES).verdict == "BLOCKED"


def test_numbers_are_checked_per_sentence_against_its_own_citations() -> None:
    result = verify("The encoder uses 64 dimensions [P1]. Heads use d_k = 64 [P2].", PASSAGES)
    assert result.verdict == "BLOCKED"
    assert [(f.sentence, f.code) for f in result.failures] == [(1, "ungrounded_number")]


def test_tag_syntax_variants_and_trailing_tag_after_period() -> None:
    both = "The encoder has N = 6 layers and the model has h = 8 heads [P1, P2]."
    assert verify(both, PASSAGES).verdict == "VERIFIED"
    assert verify(both.replace("[P1, P2]", "[P1][P2]"), PASSAGES).verdict == "VERIFIED"
    trailing = "The encoder has N = 6 identical layers. [P1] The heads use d_k = 64. [P2]"
    result = verify(trailing, PASSAGES)
    assert result.verdict == "VERIFIED" and result.claim_sentences == 2
    # A leading tag is not credited twice: the second sentence here stays uncited.
    stolen = "The encoder has N = 6 identical layers. [P1] The heads are numerous."
    assert verify(stolen, PASSAGES).verdict == "BLOCKED"


def test_lists_lead_ins_and_markdown() -> None:
    answer = ("The architecture has these properties:\n"
              "- **Encoder:** N = 6 identical layers [P1].\n"
              "- Heads: h = 8 with d_k = 64 [P2].")
    result = verify(answer, PASSAGES)
    assert result.verdict == "VERIFIED" and result.claim_sentences == 2
    numbered = "The model has 6 parts:\n1. N = 6 identical layers [P1]."
    assert verify(numbered, PASSAGES).verdict == "BLOCKED"  # a lead-in with a number is a claim
    assert verify("These are the facts:", PASSAGES).verdict == "BLOCKED"  # nothing follows


def test_sentence_splitting_respects_abbreviations_and_decimals() -> None:
    units = split_sentences("Vaswani et al. use N = 6 layers, i.e. a deep stack [P1]. "
                            "The rate is 12.5 Hz [P3].")
    assert len(units) == 2 and units[1].startswith("The rate is 12.5 Hz")


def test_unknown_tag_next_to_a_valid_one_is_still_blocked() -> None:
    result = verify("The encoder has N = 6 layers [P1][P9].", PASSAGES)
    assert [(f.check, f.sentence) for f in result.failures] == [("b", 1)]


# --- Prompt -------------------------------------------------------------------------


def test_prompt_lists_tagged_passages_with_metadata_and_the_refusal_rule() -> None:
    system, user = build_messages("What is d_model?", PASSAGES)
    assert (system.role, user.role) == ("system", "user")
    assert REFUSAL_MARKER in system.content and "[P1]" in system.content
    assert ('[P2] "Attention Is All You Need" (arXiv:1706.03762v7), pp. 4-5, section: '
            "3.2.2 Multi-Head Attention") in user.content
    assert "[P3]" in user.content and "(arXiv:2410.00037v1), p. 12" in user.content
    assert user.content.index("[P4]") < user.content.index("Question: What is d_model?")
    assert P1.text in user.content
    with pytest.raises(ValueError):
        build_messages("q", ())
    assert PROMPT_VERSION


# --- Evidence gate ------------------------------------------------------------------


def test_gate_sufficient_when_expected_paper_is_present() -> None:
    scope = analyze_scope("How many codebooks does Mimi use?")
    decision = decide(scope, (P3, P1), loop_count=0)
    assert (decision.action, decision.reason) == ("SUFFICIENT", "evidence_found")


def test_gate_loops_once_for_a_missing_paper_then_never_again() -> None:
    scope = analyze_scope("How does H-Net decide chunk boundaries?")
    first = decide(scope, (P1, P2), loop_count=0)
    assert (first.action, first.paper_filter) == ("LOOP_EXPAND", (HNET,))
    assert first.requery == scope.question
    again = decide(scope, (P1, P2), loop_count=1)
    assert again.action == "SUFFICIENT"


def test_gate_expects_weakly_cued_papers_only_for_comparisons() -> None:
    compare = analyze_scope("In H-Net, what does the routing module do? How does it differ "
                            "from the attention weights in the Transformer architecture?")
    assert expected_papers(compare) == (ATTENTION, HNET)
    decision = decide(compare, (P4,), loop_count=0)
    assert decision.paper_filter == (ATTENTION,)
    assert decision.requery == ("How does it differ from the attention weights in the "
                                "Transformer architecture?")
    plain = analyze_scope("How many layers does the Transformer encoder have?")
    assert expected_papers(plain) == ()
    assert decide(plain, (P3,), loop_count=0).action == "SUFFICIENT"  # weak cue: no filter


def test_requery_text_falls_back_to_the_whole_question() -> None:
    assert requery_text("How does H-Net work?", (HNET,)) == "How does H-Net work?"
    two = "Tell me about speech. What is the weather?"
    assert requery_text(two, (ATTENTION,)) == two  # no sentence names the paper


@pytest.mark.parametrize(
    ("question", "passages", "vocabulary", "reason"),
    [
        ("What changed in 1706.03762v5 of the Transformer paper?", (P1,), None,
         "version_not_in_corpus"),
        ("How many layers are there?", (), None, "no_passages"),
        ("Recipe for chocolate sourdough bread", (P1,), frozenset({"attention"}),
         "low_corpus_coverage"),
        ("Who won the football league last year?", (passage("x", rerank=-9.2),), None,
         "rerank_evidence_below_threshold"),
    ],
)
def test_gate_early_refusal_reasons(question: str, passages: tuple[RetrievedPassage, ...],
                                    vocabulary: frozenset[str] | None, reason: str) -> None:
    decision = decide(analyze_scope(question, vocabulary), passages, loop_count=0)
    assert (decision.action, decision.reason) == ("EARLY_REFUSAL", reason)


def test_gate_rerank_rule_is_narrow() -> None:
    low = (passage("x", rerank=-9.2),)
    named = analyze_scope("What does Moshi say about football?")
    assert decide(named, (passage("x", paper=MOSHI, rerank=-9.2),), loop_count=0).action == \
        "SUFFICIENT"  # a named paper is never refused early on score alone
    generic = analyze_scope("How many layers are there?")
    assert decide(generic, (passage("x", rerank=RERANK_REFUSAL_SCORE + 0.1),),
                  loop_count=0).action == "SUFFICIENT"
    assert decide(generic, (passage("x", rerank=None),), loop_count=0).action == "SUFFICIENT"
    assert decide(generic, low, loop_count=0).action == "EARLY_REFUSAL"
    best_wins = (passage("x", rerank=-9.0), passage("y", rerank=1.5))
    assert decide(generic, best_wins, loop_count=0).action == "SUFFICIENT"


def test_merge_appends_bounded_expansion_and_renumbers() -> None:
    extra = [passage(f"h{i}", paper=HNET, chunk_id=f"H{i}") for i in range(4)]
    merged = merge_expansion(PASSAGES, extra, top_k=4)
    # Nothing from the first result is dropped; at most top_k // 2 passages are added.
    assert [p.chunk_id for p in merged] == ["A_p3_c0", "A_p4_c1", "M_p12_c0", "H_p8_c0", "H0",
                                            "H1"]
    assert [p.rank for p in merged] == [1, 2, 3, 4, 5, 6]
    assert merge_expansion(PASSAGES, [P1, P2], top_k=4) == PASSAGES  # duplicates add nothing
    assert len(merge_expansion(PASSAGES, extra, top_k=5)) == 6
    assert (expansion_size(4), expansion_size(5), expansion_size(1)) == (2, 2, 1)


# --- Tool gateway -------------------------------------------------------------------


class StubRetriever:
    """Returns scripted passage tuples in call order (the last one repeats)."""

    def __init__(self, *results: Sequence[RetrievedPassage], delay_s: float = 0.0,
                 error: Exception | None = None) -> None:
        self._results = [tuple(r) for r in results] or [()]
        self._delay_s = delay_s
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, *, paper_filter: Collection[str] | None = None,
                 include_references: bool = False) -> RetrievalResult:
        self.calls.append({"query": query, "paper_filter": paper_filter,
                           "include_references": include_references})
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        passages = self._results[min(len(self.calls), len(self._results)) - 1]
        return RetrievalResult(
            query=query, setting="unit",
            paper_filter=None if paper_filter is None else tuple(sorted(paper_filter)),
            include_references=include_references, passages=passages,
        )


async def test_tool_call_is_validated_timed_and_recorded() -> None:
    retriever = StubRetriever(PASSAGES)
    gateway = ToolGateway({"unit": retriever})
    call = await gateway.retrieve_passages({"query": "d_model?", "retrieval_setting": "unit",
                                            "paper_filter": (ATTENTION,)})
    assert call.result.passages == PASSAGES
    assert (call.record.tool, call.record.ok, call.record.passages) == ("retrieve_passages",
                                                                        True, 4)
    assert call.record.elapsed_ms >= 0 and call.record.args["paper_filter"] == (ATTENTION,)
    assert retriever.calls == [{"query": "d_model?", "paper_filter": (ATTENTION,),
                                "include_references": False}]


@pytest.mark.parametrize(
    "args",
    [
        {"retrieval_setting": "unit"},
        {"query": "", "retrieval_setting": "unit"},
        {"query": "   ", "retrieval_setting": "unit"},
        {"query": "q", "retrieval_setting": "other"},
        {"query": "q", "retrieval_setting": "unit", "paper_filter": ("9999.99999v1",)},
        {"query": "q", "retrieval_setting": "unit", "paper_filter": ()},
        {"query": "q", "retrieval_setting": "unit", "top_k": 50},
        {"query": 7, "retrieval_setting": "unit"},
    ],
    ids=["missing-query", "empty", "blank", "unknown-setting", "unknown-paper", "empty-filter",
         "extra-field", "wrong-type"],
)
async def test_invalid_tool_arguments_never_reach_the_retriever(args: dict[str, Any]) -> None:
    retriever = StubRetriever(PASSAGES)
    with pytest.raises(ToolInputError) as info:
        await ToolGateway({"unit": retriever}).retrieve_passages(args)
    assert retriever.calls == [] and not info.value.record.ok


async def test_tool_timeout_and_execution_errors_are_typed() -> None:
    slow = ToolGateway({"unit": StubRetriever(PASSAGES, delay_s=0.3)}, timeout_s=0.05)
    with pytest.raises(ToolTimeoutError) as timeout:
        await slow.retrieve_passages({"query": "q", "retrieval_setting": "unit"})
    assert "timed out" in (timeout.value.record.error or "")
    broken = ToolGateway({"unit": StubRetriever(error=RuntimeError("secret detail"))})
    with pytest.raises(ToolExecutionError) as failed:
        await broken.retrieve_passages({"query": "q", "retrieval_setting": "unit"})
    assert failed.value.record.error == "retrieve_passages failed: RuntimeError"
    with pytest.raises(ValueError):
        ToolGateway({})


# --- Pipeline -----------------------------------------------------------------------


class TickClock:
    """Monotonic fake clock: every reading advances by 1 ms."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        self.now += 1_000_000
        return self.now


def build(replies: Sequence[ScriptedReply], *results: Sequence[RetrievedPassage],
          retriever: StubRetriever | None = None, timeout_s: float = 5.0,
          vocabulary: frozenset[str] | None = None,
          ) -> tuple[Pipeline, ScriptedProvider, StubRetriever, TickClock]:
    clock = TickClock()
    stub = retriever or StubRetriever(*results)
    provider = ScriptedProvider(replies, clock=clock)
    pipeline = Pipeline(
        gateway=ToolGateway({"unit": stub}, timeout_s=timeout_s, clock=clock),
        generator=SingleCallGenerator(provider),
        config=PipelineConfig(model_id="test-model", max_completion_tokens=300),
        top_k={"unit": 4}, vocabulary=vocabulary,
    )
    return pipeline, provider, stub, clock


async def ask(pipeline: Pipeline, clock: TickClock,
              question: str = "What is d_model in the encoder?",
              ) -> tuple[PipelineOutcome, SpanSummary]:
    spans = ServerSpanTracker(clock=clock)
    request = PipelineRequest(request_id=f"{RUN_ID}-000001", question=question,
                              retrieval_setting="unit")
    outcome = await pipeline.run(request, spans)
    return outcome, spans.finish()


def reply(text: str, *, usage: TokenUsage | None = USAGE, reasoning: tuple[str, ...] = (),
          chunks: int = 3) -> ScriptedReply:
    size = max(1, len(text) // chunks)
    pieces = tuple(text[i:i + size] for i in range(0, len(text), size)) if text else ()
    return ScriptedReply(content_chunks=pieces, reasoning_chunks=reasoning, usage=usage)


def assert_spans(summary: SpanSummary) -> None:
    parts = summary.waiting_ms + summary.working_ms + summary.retrying_ms
    assert abs(parts - summary.server_total_ms) <= 1.0
    assert summary.working_ms > 0 and summary.retrying_ms == 0


async def test_verified_answer_is_delivered() -> None:
    pipeline, provider, retriever, clock = build([reply(GOOD_ANSWER, reasoning=("think",))],
                                                 PASSAGES)
    outcome, summary = await ask(pipeline, clock)
    assert (outcome.kind, outcome.status, outcome.error_code) == ("answer", "ok", None)
    assert outcome.verification_verdict == "VERIFIED" and not outcome.refused
    assert outcome.delivered_text == GOOD_ANSWER
    assert [c.tag for c in outcome.citations] == [1, 2]
    assert (outcome.tokens_in, outcome.tokens_out, outcome.usage_source) == (900, 40, "provider")
    assert (outcome.provider_called, outcome.retries, outcome.model_id) == (True, 0,
                                                                            "test-model")
    assert len(provider.requests) == 1 and len(retriever.calls) == 1
    sent = provider.requests[0]
    assert sent.max_completion_tokens == 300 and sent.temperature == 0.0
    assert "[P4]" in sent.messages[1].content
    assert outcome.trace.loop_count == 0 and outcome.trace.prompt_version == PROMPT_VERSION
    assert outcome.trace.reasoning_chars == 5 and outcome.retrieval_ms > 0
    assert summary.provider_first_token_ms is not None
    assert 0 < summary.provider_first_token_ms <= summary.server_total_ms
    assert_spans(summary)


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ("The encoder has N = 6 identical layers [P9].", "unknown_passage_tag"),
        ("The encoder has N = 6 identical layers [P1]. It is the best model ever.",
         "uncited_sentence"),
        ("The encoder uses d_model = 1024 [P1].", "ungrounded_number"),
        ("INSUFFICIENT_EVIDENCE. However the encoder has N = 6 layers [P1].",
         "refusal_with_content"),
    ],
    ids=["out-of-range-tag", "untagged-claim", "unsupported-number", "mixed-refusal"],
)
async def test_blocked_answers_become_a_safe_refusal_with_one_provider_call(
    answer: str, code: str
) -> None:
    # A second scripted reply is available: the pipeline must never ask for it.
    pipeline, provider, _, clock = build([reply(answer), reply(GOOD_ANSWER)], PASSAGES)
    outcome, summary = await ask(pipeline, clock)
    assert (outcome.kind, outcome.status) == ("safe_refusal", "ok")
    assert outcome.error_code == "citation_verification_failed"
    assert outcome.verification_verdict == "BLOCKED" and outcome.refused
    assert outcome.delivered_text == SAFE_REFUSAL_MESSAGE and outcome.citations == ()
    assert len(provider.requests) == 1  # exactly one LLM call, no repair
    assert outcome.trace.raw_answer == answer  # kept for the audit trail only
    assert outcome.trace.verification is not None
    assert code in {f.code for f in outcome.trace.verification.failures}
    assert (outcome.tokens_in, outcome.usage_source) == (900, "provider")  # tokens were spent
    assert_spans(summary)


async def test_llm_refusal_is_verified_and_delivered_as_a_fixed_message() -> None:
    pipeline, provider, _, clock = build([reply(REFUSAL_MARKER)], PASSAGES)
    outcome, _ = await ask(pipeline, clock)
    assert (outcome.kind, outcome.status, outcome.error_code) == ("llm_refusal", "ok", None)
    assert outcome.verification_verdict == "REFUSAL_VERIFIED" and outcome.refused
    assert outcome.delivered_text == LLM_REFUSAL_MESSAGE and len(provider.requests) == 1


async def test_requery_loop_runs_at_most_once() -> None:
    hnet = [passage(f"routing {i}", paper=HNET, chunk_id=f"H{i}") for i in range(4)]
    # First result has no H-Net passage; so would every later one.
    pipeline, provider, retriever, clock = build(
        [reply("The encoder has N = 6 identical layers [P1].")], PASSAGES[:3], hnet,
        hnet, hnet)
    outcome, summary = await ask(pipeline, clock, "How does H-Net decide chunk boundaries?")
    assert len(retriever.calls) == 2 and outcome.trace.loop_count == 1
    assert retriever.calls[0]["paper_filter"] is None
    assert retriever.calls[1]["paper_filter"] == (HNET,)
    assert [d.action for d in outcome.trace.gate_decisions] == ["LOOP_EXPAND", "SUFFICIENT"]
    assert [p.chunk_id for p in outcome.passages] == ["A_p3_c0", "A_p4_c1", "M_p12_c0", "H0",
                                                      "H1"]  # first result kept, 2 appended
    assert "[P5]" in provider.requests[0].messages[1].content
    assert len(outcome.trace.tool_calls) == 2 and len(provider.requests) == 1
    assert outcome.kind == "answer"
    assert_spans(summary)


async def test_loop_never_repeats_even_if_the_paper_is_still_missing() -> None:
    pipeline, _, retriever, clock = build([reply("N = 6 identical layers are used [P1].")],
                                          PASSAGES[:2], PASSAGES[:2], PASSAGES[:2])
    outcome, _ = await ask(pipeline, clock, "How does H-Net decide chunk boundaries?")
    assert len(retriever.calls) == 2
    assert [d.action for d in outcome.trace.gate_decisions] == ["LOOP_EXPAND", "SUFFICIENT"]


async def test_clear_out_of_corpus_question_is_refused_without_the_provider() -> None:
    off_topic = tuple(passage(f"t{i}", rerank=-9.0 - i, chunk_id=f"X{i}") for i in range(4))
    pipeline, provider, _, clock = build([reply(GOOD_ANSWER)], off_topic)
    outcome, summary = await ask(pipeline, clock, "Who won the football league last year?")
    assert (outcome.kind, outcome.status, outcome.error_code) == ("early_refusal", "ok", None)
    assert outcome.verification_verdict == "EARLY_REFUSAL" and outcome.refused
    assert outcome.delivered_text == OUT_OF_CORPUS_MESSAGE
    assert provider.requests == [] and not outcome.provider_called
    assert (outcome.tokens_in, outcome.tokens_out, outcome.usage_source) == (0, 0, "none")
    assert outcome.model_id is None and outcome.retries == 0
    assert summary.provider_first_token_ms is None
    assert_spans(summary)


async def test_other_arxiv_version_is_refused_early() -> None:
    pipeline, provider, _, clock = build([reply(GOOD_ANSWER)], PASSAGES)
    outcome, _ = await ask(pipeline, clock, "In 1706.03762v5, what is d_model?")
    assert outcome.kind == "early_refusal" and outcome.delivered_text == VERSION_MESSAGE
    assert provider.requests == []


async def test_tool_timeout_is_handled_without_a_crash() -> None:
    slow = StubRetriever(PASSAGES, delay_s=0.3)
    pipeline, provider, _, clock = build([reply(GOOD_ANSWER)], retriever=slow, timeout_s=0.05)
    outcome, summary = await ask(pipeline, clock)
    assert (outcome.kind, outcome.status, outcome.error_code) == ("failed", "error",
                                                                  "internal_error")
    assert outcome.delivered_text is None and outcome.verification_verdict == "NOT_RUN"
    assert provider.requests == [] and not outcome.provider_called
    assert not outcome.trace.tool_calls[0].ok
    assert "timed out" in (outcome.trace.tool_calls[0].error or "")
    assert_spans(summary)


async def test_failed_requery_keeps_the_first_result() -> None:
    class FlakyRetriever(StubRetriever):
        def retrieve(self, query: str, *, paper_filter: Collection[str] | None = None,
                     include_references: bool = False) -> RetrievalResult:
            if paper_filter is not None:
                self.calls.append({"paper_filter": paper_filter})
                raise RuntimeError("index unavailable")
            return super().retrieve(query, paper_filter=paper_filter,
                                    include_references=include_references)

    first = PASSAGES[:3]  # no H-Net passage, so the gate asks for a re-query
    pipeline, provider, retriever, clock = build([reply(GOOD_ANSWER)],
                                                 retriever=FlakyRetriever(first))
    outcome, _ = await ask(pipeline, clock, "How does H-Net decide chunk boundaries?")
    assert outcome.kind == "answer" and outcome.passages == first
    assert [c.ok for c in outcome.trace.tool_calls] == [True, False]
    assert len(retriever.calls) == 2 and len(provider.requests) == 1


async def test_reference_intent_is_passed_to_the_tool() -> None:
    pipeline, _, retriever, clock = build([reply(REFUSAL_MARKER)], PASSAGES)
    await ask(pipeline, clock, "Which work is cited for layer normalization?")
    assert retriever.calls[0]["include_references"] is True


async def test_provider_failure_is_reported_without_retry_or_delivery() -> None:
    limited = ProviderRateLimitError("limited", scope="minute", basis="body")
    pipeline, provider, _, clock = build([ScriptedReply(error=limited), reply(GOOD_ANSWER)],
                                         PASSAGES)
    outcome, summary = await ask(pipeline, clock)
    assert (outcome.kind, outcome.status, outcome.error_code) == ("failed", "rate_limited",
                                                                  "upstream_rate_limited")
    assert outcome.delivered_text is None and outcome.verification_verdict == "NOT_RUN"
    assert outcome.provider_called and len(provider.requests) == 1  # no retry in P05
    assert (outcome.tokens_in, outcome.tokens_out, outcome.usage_source) == (0, 0, "none")
    assert outcome.trace.provider_error == "ProviderRateLimitError"
    assert summary.provider_first_token_ms is None
    assert_spans(summary)


async def test_stalled_stream_counts_estimated_tokens() -> None:
    stalled = ProviderTimeoutError("stall", phase="stall")
    partial = ScriptedReply(content_chunks=("The encoder has", " N = 6"), error=stalled,
                            fail_after_chunks=1)
    pipeline, _, _, clock = build([partial], PASSAGES)
    outcome, summary = await ask(pipeline, clock)
    assert (outcome.status, outcome.error_code) == ("timeout", "provider_stalled")
    assert outcome.usage_source == "estimated" and outcome.tokens_in > 0 < outcome.tokens_out
    assert outcome.delivered_text is None  # the partial text is never delivered
    assert summary.provider_first_token_ms is not None


async def test_missing_usage_is_estimated_and_reasoning_is_not_a_first_token() -> None:
    pipeline, _, _, clock = build([reply(GOOD_ANSWER, usage=None)], PASSAGES)
    outcome, _ = await ask(pipeline, clock)
    assert outcome.kind == "answer" and outcome.usage_source == "estimated"
    assert outcome.tokens_in > 100 and outcome.tokens_out > 0
    # Only reasoning arrives, no content: blocked as empty, and no provider first token.
    pipeline, _, _, clock = build([reply("", reasoning=("thinking hard",))], PASSAGES)
    outcome, summary = await ask(pipeline, clock)
    assert outcome.kind == "safe_refusal" and summary.provider_first_token_ms is None
    assert outcome.trace.verification is not None
    assert outcome.trace.verification.failures[0].code == "empty_answer"


async def test_truncated_generation_is_flagged_in_the_trace() -> None:
    cut = ScriptedReply(content_chunks=("The encoder has N = 6 identical layers [P1]. The",),
                        usage=USAGE, finish_reason="length")
    pipeline, _, _, clock = build([cut], PASSAGES)
    outcome, _ = await ask(pipeline, clock)
    assert outcome.trace.truncated and outcome.kind == "safe_refusal"  # dangling claim


async def test_client_disconnect_propagates_as_cancellation() -> None:
    class CancellingProvider(ScriptedProvider):
        async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
            self.requests.append(request)
            raise asyncio.CancelledError
            yield  # pragma: no cover

    clock = TickClock()
    pipeline = Pipeline(
        gateway=ToolGateway({"unit": StubRetriever(PASSAGES)}, clock=clock),
        generator=SingleCallGenerator(CancellingProvider([], clock=clock)),
        config=PipelineConfig(model_id="test-model"), top_k={"unit": 4},
    )
    with pytest.raises(asyncio.CancelledError):
        await ask(pipeline, clock)


async def test_invalid_requests_and_wiring_fail_loudly() -> None:
    pipeline, _, _, clock = build([reply(GOOD_ANSWER)], PASSAGES)
    spans = ServerSpanTracker(clock=clock)
    with pytest.raises(ContractViolationError, match="unknown retrieval_setting"):
        await pipeline.run(PipelineRequest(request_id="r", question="q",
                                           retrieval_setting="other"), spans)
    with pytest.raises(ValidationError):
        PipelineRequest(request_id="r", question="   ", retrieval_setting="unit")
    with pytest.raises(ValidationError):
        PipelineRequest(request_id="r", question="q" * 2001, retrieval_setting="unit")
    with pytest.raises(ValueError, match="top_k missing"):
        Pipeline(gateway=ToolGateway({"unit": StubRetriever(PASSAGES)}),
                 generator=SingleCallGenerator(ScriptedProvider([])),
                 config=PipelineConfig(model_id="m"), top_k={})
    with pytest.raises(ValueError):
        GenerationResult(None, None, False, 0)


async def test_every_outcome_kind_builds_a_valid_result_row() -> None:
    """The pipeline's outcome plus the span summary satisfy the results.csv contract."""
    off_topic = tuple(passage(f"t{i}", rerank=-9.0, chunk_id=f"X{i}") for i in range(2))
    limited = ProviderRateLimitError("limited", scope="minute", basis="body")
    cases = [
        ([reply(GOOD_ANSWER)], PASSAGES, "What is d_model in the encoder?"),
        ([reply("The encoder uses d_model = 1024 [P1].")], PASSAGES, "What is d_model?"),
        ([reply(REFUSAL_MARKER)], PASSAGES, "What is d_model?"),
        ([], off_topic, "Who won the football league last year?"),
        ([ScriptedReply(error=limited)], PASSAGES, "What is d_model?"),
    ]
    kinds = []
    for replies, passages, question in cases:
        pipeline, _, _, clock = build(replies, passages)
        outcome, summary = await ask(pipeline, clock, question)
        kinds.append(outcome.kind)
        delivered = outcome.status == "ok"
        row = ResultRow(
            request_id=outcome.request_id, scenario="quality_eval", source="stand-in",
            start_time=datetime(2026, 11, 13, 8, 0, tzinfo=UTC),
            first_token_ms=summary.server_total_ms if delivered else None,
            total_ms=summary.server_total_ms + 1.0, status=outcome.status,
            retries=outcome.retries, tokens_in=outcome.tokens_in, tokens_out=outcome.tokens_out,
            retrieval_setting=outcome.retrieval_setting, answer_grade=None, run_id=RUN_ID,
            question_id=None, provider_first_token_ms=summary.provider_first_token_ms,
            server_total_ms=summary.server_total_ms, queue_depth_at_entry=0,
            waiting_ms=summary.waiting_ms, working_ms=summary.working_ms,
            retrying_ms=summary.retrying_ms, retrieval_ms=outcome.retrieval_ms,
            cache_status="off", provider_called=outcome.provider_called,
            degraded=outcome.degraded, model_id=outcome.model_id,
            usage_source=outcome.usage_source, cost_usd=None,
            verification_verdict=outcome.verification_verdict, error_code=outcome.error_code,
        )
        assert row.status == outcome.status
    assert kinds == ["answer", "safe_refusal", "llm_refusal", "early_refusal", "failed"]
