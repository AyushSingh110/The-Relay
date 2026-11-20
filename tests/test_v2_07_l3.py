"""V2-07 L3: refusal-shaped sentences ("the passages do not state ...") are refusals, not
verified claims (the v1 gap: such a sentence carried a citation tag and passed as VERIFIED).

Owner decisions (docs/DECISIONS.md, 2026-11-20): streaming holds a refusal-shaped first
sentence and decides at the end; after verified sentences it stops the answer as a partial;
the whole-answer verifier blocks a refusal-shaped sentence among claims and treats an answer
made only of refusal-shaped sentences as a verified refusal. Recorded v1 evidence is read only.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from relay.app.bootstrap import ensure_dev_tenant
from relay.experiments.table3_replay import load_rows
from relay.orchestrator.streaming import StreamingVerifier
from relay.orchestrator.verifier import split_sentences, verify
from relay.store import open_store
from tests.test_ask_v1 import S1, S2, Harness, answer, ask, geology, question
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import PASSAGES

MODEL_COMPARISON_RUN = Path("evidence/experiments/exp-20261113T063331Z-e17e9c1")
TABLE3_RUN = Path("evidence/experiments/exp-20261113T064542Z-c482696")

REFUSAL_SHAPED = (
    "The passage does not give the dropout rate [P1].",
    "The passages do not state the number of parameters [P1].",
    "The provided passages do not report a latency figure [P1].",
    "The passages cannot tell us how long training took [P2].",
    "The document does not mention the batch size [P1].",
    "The documents do not describe the hardware [P2].",
    "The learning rate is not mentioned in the passages [P1].",
    "The vocabulary size is not stated in the passage [P1].",
    "No information is provided about the optimizer [P1].",
    "No information was provided on the warmup schedule [P2].",
    "The exact cost cannot be determined from the passages [P1].",
    "Positional encodings for audio are not addressed in the documents [P2].",
    # common phrasings found missing by the L3 review
    "The passages provided do not state the model size [P1].",
    "The passages given do not specify it [P1].",
    "There is no information in the passages about the batch size [P1].",
    "The context does not specify the dropout rate [P1].",
    "The provided context does not specify the dropout rate [P1].",
    "The batch size does not appear in the passages [P1].",
    "None of the passages mention the optimizer [P1].",
    "The passages contain no information about training time [P1].",
    "This is not explicitly stated in the provided passages [P1].",
    "The specific value is not provided in the passages [P1].",
    "Based on the provided passages, it is not possible to determine the cost [P1].",
    "The provided text does not state the number of heads [P1].",
    "The provided excerpts do not report a latency figure [P1].",
)
CLAIMS = (
    "The attention mechanism uses queries and keys [1].",
    "The encoder has N = 6 identical layers [P1].",
    "The decoder does not use recurrence [P1].",  # a negative claim, not a refusal
    "The boundary cannot be determined from local context alone [P1].",
    "Long-range dependencies are not addressed in RNNs without attention [P1].",
    # genuine claims found misclassified by the L3 review
    "No information is provided to the decoder about future positions, via masking [P1].",
    "Masking ensures no information is provided from subsequent positions [P1].",
    "Chunk boundaries cannot be determined from context in advance, so the router "
    "predicts them [P1].",
    "Recurrence is not addressed in the context of this architecture [P1].",
    "Speaker identity is not mentioned in the source text [P2].",
    "The document's authors do not report BLEU on this set [P1].",
    "The retrieved documents cannot exceed the context window [P1].",
    "It is not possible to determine boundaries without the router [P1].",
)


# 1 --------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", REFUSAL_SHAPED)
def test_each_refusal_shape_is_classified_refusal_as_claim(sentence: str) -> None:
    verdict = StreamingVerifier().verify_sentence(sentence, PASSAGES)
    assert (verdict.passed, verdict.reason_class) == (False, "refusal_as_claim")


def test_a_refusal_shape_without_a_citation_is_still_a_refusal() -> None:
    verdict = StreamingVerifier().verify_sentence("The passages do not state it.", PASSAGES)
    assert verdict.reason_class == "refusal_as_claim"  # not "uncited": it is no claim


# 2 --------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", CLAIMS)
def test_claims_including_negative_ones_do_not_trigger_l3(sentence: str) -> None:
    verdict = StreamingVerifier().verify_sentence(sentence, PASSAGES)
    assert verdict.reason_class != "refusal_as_claim"


# 3 --------------------------------------------------------------------------------------
@pytest.mark.skipif(not MODEL_COMPARISON_RUN.is_dir(), reason="v1 evidence not present")
def test_the_v1_q21_case_is_now_classified_refusal_as_claim() -> None:
    """The plan's documented gap: v1 qwen q21 balanced_rerank was VERIFIED with "The
    provided passages do not state how Wav LM performs masked prediction ...". (The phase
    prompt named q02 small_fast; that recorded answer has no refusal-shaped sentence and was
    BLOCKED in v1 for ungrounded numbers, so it is checked to stay unaffected below.)"""
    # the run compares several models; the qwen row is the one with the recorded sentence
    row = next(r for r in load_rows(MODEL_COMPARISON_RUN)
               if r.question_id == "q21" and r.setting == "balanced_rerank"
               and r.raw_answer is not None and "do not state" in r.raw_answer)
    assert row.verdict == "VERIFIED" and row.raw_answer is not None  # what v1 delivered
    verifier = StreamingVerifier()
    shaped = [s for s in split_sentences(row.raw_answer) if "do not state" in s]
    assert shaped, "the recorded refusal-shaped sentence"
    for sentence in shaped:
        verdict = verifier.verify_sentence(sentence, PASSAGES)
        assert verdict.reason_class == "refusal_as_claim"


@pytest.mark.skipif(not TABLE3_RUN.is_dir(), reason="v1 evidence not present")
def test_q02_small_fast_has_no_refusal_shaped_sentence() -> None:
    row = next(r for r in load_rows(TABLE3_RUN)
               if r.question_id == "q02" and r.setting == "small_fast")
    assert row.raw_answer is not None and row.verdict == "BLOCKED"
    verifier = StreamingVerifier()
    assert all(verifier.verify_sentence(s, PASSAGES).reason_class != "refusal_as_claim"
               for s in split_sentences(row.raw_answer))


def test_the_whole_answer_verifier_blocks_a_refusal_shape_among_claims() -> None:
    result = verify("The encoder has N = 6 identical layers [P1]. "
                    "The passages do not state the number of parameters [P1].", PASSAGES)
    assert result.verdict == "BLOCKED"
    assert [f.code for f in result.failures] == ["refusal_as_claim"]


def test_an_answer_made_only_of_refusal_shapes_is_a_verified_refusal() -> None:
    result = verify("The passages do not state the number of parameters [P1]. "
                    "No information is provided about the optimizer.", PASSAGES)
    assert result.verdict == "REFUSAL_VERIFIED"


# 4, 5 and the held case: POST /v1/ask --------------------------------------------------
@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


SHAPED = "The passages do not state how hot basalt lava is [P1]."


async def test_a_single_sentence_refusal_is_a_verified_refusal_and_is_cached(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(SHAPED)])
    first = await ask(harness.app, question(cid))
    assert "sentence" not in first.names  # the model's sentence is never shown as a claim
    final = first.of("final")[0]
    assert (final["refused"], final["verdict"], final["status"]) == (True, "REFUSAL_VERIFIED",
                                                                     "ok")
    assert final["message"] and "do not state" not in final["message"]  # collection message
    again = await ask(harness.app, question(cid))
    assert again.of("final")[0]["cache_status"] == "exact_hit"  # a correct refusal is cached


async def test_a_mixed_answer_stops_at_the_refusal_shape_and_keeps_a_partial(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(S1, S2, SHAPED), answer(S1, S2, SHAPED)])
    stream = await ask(harness.app, question(cid))
    assert [s["text"] for s in stream.of("sentence")] == [S1, S2]
    stopped = stream.of("stopped")[0]
    assert (stopped["reason_class"], stopped["partial"]) == ("refusal_as_claim", True)
    assert stream.of("final")[0]["partial"] is True
    again = await ask(harness.app, question(cid))
    assert again.of("final")[0]["cache_status"] != "exact_hit"  # a partial is never cached


async def test_a_refusal_shape_followed_by_a_claim_is_refused_and_not_cached(env: Env) -> None:
    cid = await geology(env)
    harness = Harness(env, [answer(SHAPED, S1), answer(SHAPED, S1)])
    stream = await ask(harness.app, question(cid))
    assert "sentence" not in stream.names  # nothing released: refused, not partial
    final = stream.of("final")[0]
    assert (final["refused"], final["partial"]) == (True, False)
    again = await ask(harness.app, question(cid))
    assert again.of("final")[0]["cache_status"] != "exact_hit"
