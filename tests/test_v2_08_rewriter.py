"""V2-08: standalone-question rewriting for follow-ups. The LLM rewriter is exercised with a
scripted generator (no network); the rule rewriter is deterministic (stand-in, tests)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relay.orchestrator.pipeline import GenerationResult, SingleCallGenerator
from relay.orchestrator.rewriter import (
    HISTORY_CHARS,
    LLMQueryRewriter,
    RuleQueryRewriter,
)
from relay.providers.base import ChatRequest, ScriptedProvider, ScriptedReply, TokenUsage
from relay.providers.errors import ProviderConnectionError
from relay.store.repositories import Turn

MOSHI = [Turn("How does Moshi achieve full-duplex dialogue?",
              "Moshi models two audio streams at once [P1].")]


# --- rules (stand-in mode and offline tests) ---------------------------------------------
async def test_a_pronoun_follow_up_is_resolved_to_the_previous_subject() -> None:
    result = await RuleQueryRewriter().resolve("And what is its inference latency?", MOSHI)
    assert result.question == "What is Moshi's inference latency?"
    assert result.rewritten and result.method == "rules"


async def test_a_self_contained_follow_up_is_unchanged() -> None:
    question = "What is the hidden dimension of BERT?"
    result = await RuleQueryRewriter().resolve(question, MOSHI)
    assert (result.question, result.rewritten) == (question, False)


async def test_without_a_subject_in_the_history_the_question_is_unchanged() -> None:
    history = [Turn("what is going on here?", "nothing much [P1].")]
    result = await RuleQueryRewriter().resolve("What is its latency?", history)
    assert (result.question, result.rewritten) == ("What is its latency?", False)


async def test_the_most_recent_subject_wins_and_older_turns_are_searched() -> None:
    history = [Turn("How does Moshi work?", "a"), Turn("What is its latency?", "b")]
    assert (await RuleQueryRewriter().resolve("And how is it trained?", history)).question \
        == "How is Moshi trained?"
    history = [Turn("How does Moshi work?", "a"), Turn("How does H-Net chunk text?", "b")]
    assert (await RuleQueryRewriter().resolve("What does it compress?", history)).question \
        == "What does H-Net compress?"


@pytest.mark.parametrize(("question", "expected"), [
    ("What is it?", "What is Moshi?"),
    ("Is this model open source?", "Is Moshi open source?"),
    ("What are their results?", "What are Moshi's results?"),
])
async def test_other_references_resolve(question: str, expected: str) -> None:
    assert (await RuleQueryRewriter().resolve(question, MOSHI)).question == expected


# --- LLM rewriter -----------------------------------------------------------------------
class Scripted:
    """The real single-call generator over a scripted provider, optionally slowed down."""

    def __init__(self, text: str | None = None, *, delay_s: float = 0.0,
                 error: ProviderConnectionError | None = None) -> None:
        reply = (ScriptedReply(error=error) if error is not None else
                 ScriptedReply(content_chunks=(text,) if text else (),
                               usage=TokenUsage(prompt_tokens=120, completion_tokens=12)))
        self._inner = SingleCallGenerator(ScriptedProvider([reply]))
        self.delay_s = delay_s
        self.requests: list[ChatRequest] = []

    async def generate(self, request: ChatRequest, spans: Any, **_: Any) -> GenerationResult:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return await self._inner.generate(request, spans)


def llm(generator: Scripted, timeout_s: float = 1.5) -> LLMQueryRewriter:
    return LLMQueryRewriter(generator, model="m", timeout_s=timeout_s)


async def test_the_llm_rewrite_is_used_with_a_small_budget_and_no_temperature() -> None:
    gen = Scripted("What is Moshi's inference latency?")
    result = await llm(gen).resolve("And what is its inference latency?", MOSHI)
    assert (result.question, result.rewritten, result.method) == (
        "What is Moshi's inference latency?", True, "llm")
    request = gen.requests[0]
    assert (request.max_completion_tokens, request.temperature) == (64, 0.0)
    assert result.tokens == 132


async def test_a_timeout_falls_back_to_the_original_question() -> None:
    result = await llm(Scripted("never used", delay_s=2.0), timeout_s=0.05).resolve(
        "What is its latency?", MOSHI)
    assert (result.question, result.rewritten, result.fallback) == (
        "What is its latency?", False, "timeout")


async def test_a_provider_failure_falls_back_to_the_original_question() -> None:
    gen = Scripted(error=ProviderConnectionError("reset", reason="test"))
    result = await llm(gen).resolve("What is its latency?", MOSHI)
    assert (result.question, result.fallback) == ("What is its latency?", "provider_error")


@pytest.mark.parametrize(("output", "expected"), [
    ("Standalone question: What is Moshi's latency?", "What is Moshi's latency?"),
    ('"What is Moshi\'s latency?"', "What is Moshi's latency?"),
    ("What is Moshi's latency?\nThis resolves 'its' to Moshi.", "What is Moshi's latency?"),
])
async def test_the_llm_output_is_cleaned(output: str, expected: str) -> None:
    assert (await llm(Scripted(output)).resolve("What is its latency?", MOSHI)).question \
        == expected


@pytest.mark.parametrize("output", ["", "   ", "x" * 400, "I cannot help with that."])
async def test_unusable_llm_output_falls_back(output: str) -> None:
    result = await llm(Scripted(output)).resolve("What is its latency?", MOSHI)
    assert (result.question, result.rewritten) == ("What is its latency?", False)
    assert result.fallback == "unusable_output"


async def test_the_prompt_is_bounded_whatever_the_history() -> None:
    gen = Scripted("What is Moshi's latency?")
    history = [Turn(f"question {n} " + "q" * 5000, "answer " + "a" * 5000) for n in range(30)]
    await llm(gen).resolve("What is its latency?", history)
    prompt = "\n".join(m.content for m in gen.requests[0].messages)
    assert "question 28" in prompt and "question 29" in prompt  # the last 2 turns only
    assert "question 27" not in prompt
    assert len(prompt) < 4 * HISTORY_CHARS + 4000  # each turn is truncated


async def test_citation_tags_in_stored_answers_are_never_taken_for_a_subject() -> None:
    history = [Turn("explain the routing", "It uses [P1][P2, P3] cosine similarity [P4].")]
    result = await RuleQueryRewriter().resolve("Why is it stable?", history)
    assert "P1" not in result.question and "P4" not in result.question


# --- compose: which rewriter a lane gets -------------------------------------------------
@pytest.mark.parametrize(("kind", "mode", "expected"), [
    ("standin", "auto", RuleQueryRewriter), ("groq", "auto", LLMQueryRewriter),
    ("standin", "llm", LLMQueryRewriter), ("groq", "rules", RuleQueryRewriter),
])
def test_the_lane_and_mode_choose_the_rewriter(kind: str, mode: str, expected: type) -> None:
    from relay.app.compose import make_rewriter
    from relay.app.settings import LaneSettings, RelaySettings

    lane = LaneSettings(name="l", kind=kind, model="m", **(
        {"embedded": True} if kind == "standin" else {}))
    settings = RelaySettings.model_validate({"rewriter": {"mode": mode}})
    rewriter, note = make_rewriter(settings, lane, Scripted("x"))
    assert isinstance(rewriter, expected)
    assert note.startswith("follow-up rewriting:")
