"""V2-07 L4 (advisory): an entailment score for every verified sentence.

The scorer is a local ONNX NLI cross-encoder (owner-approved; onnxruntime + tokenizers).
Tests use a fake scorer; the real-model tests are skipped when the model is not downloaded.
L4 never blocks an answer in this phase. Scripted provider, :memory: store; no network."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.orchestrator.streaming import score_sentence
from relay.orchestrator.verifier import EntailmentScorer, entailment_probability
from relay.store import open_store
from tests.test_ask_v1 import S1, S2, Harness, answer, ask, question
from tests.test_ingest_worker import Env
from tests.test_pipeline_and_verifier import PASSAGES

L4_MODEL = Path("data/models/nli/nli-MiniLM2-L6-H768")
needs_model = pytest.mark.skipif(not (L4_MODEL / "config.json").is_file(),
                                 reason="the L4 model is not downloaded")


class FakeScorer:
    def __init__(self, value: float = 0.8, delay_s: float = 0.0) -> None:
        self.value, self.delay_s = value, delay_s
        self.calls: list[tuple[str, str]] = []

    def score(self, sentence: str, passage: str) -> float:
        self.calls.append((sentence, passage))
        if self.delay_s:
            time.sleep(self.delay_s)  # blocking, like the real CPU-bound model
        return self.value


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


async def collection(env: Env, *, l4: bool = True, buffered: bool = False) -> str:
    created = await env.store.collections.create(DEV_TENANT_ID, "geology", buffered=buffered,
                                                  l4_enabled=l4)
    await env.upload(created.id, ["Basalt is a volcanic rock formed from lava"], title="Rocks")
    await env.worker().run_once()
    return created.id


# 6 --------------------------------------------------------------------------------------
def test_the_entailment_probability_is_a_probability_for_any_logits() -> None:
    """Property (seeded): softmax of any logits gives a value in [0, 1], and raising the
    entailment logit never lowers it."""
    rng = random.Random(20261120)
    for _ in range(500):
        logits = [rng.uniform(-50, 50) for _ in range(3)]
        index = rng.randrange(3)
        p = entailment_probability(logits, index)
        assert 0.0 <= p <= 1.0 and not math.isnan(p)
        higher = list(logits)
        higher[index] += rng.uniform(0.1, 10)
        assert entailment_probability(higher, index) >= p


@needs_model
def test_the_real_scorer_returns_a_probability_for_any_input() -> None:
    scorer = EntailmentScorer(L4_MODEL)
    rng = random.Random(7)
    words = ["attention", "layer", "6", "heads", "√d_k", "ß", "日本", "", "the", "[P1]"]
    cases = [("", ""), ("a", ""), ("", "a"), ("x " * 3000, "y " * 3000)]
    cases += [(" ".join(rng.choice(words) for _ in range(rng.randint(0, 60))),
               " ".join(rng.choice(words) for _ in range(rng.randint(0, 30))))
              for _ in range(30)]
    for premise, hypothesis in cases:
        value = scorer.score(hypothesis, premise)
        assert isinstance(value, float) and 0.0 <= value <= 1.0
    supported = scorer.score("The encoder has six layers.",
                             "The encoder is composed of a stack of N = 6 identical layers.")
    contradicted = scorer.score("The encoder has twelve layers.",
                                "The encoder is composed of a stack of N = 6 identical layers.")
    assert supported > contradicted  # the label order is read correctly from the config


# 7 --------------------------------------------------------------------------------------
async def test_scoring_does_not_block_the_event_loop() -> None:
    scorer = FakeScorer(delay_s=0.05)  # blocks its thread for 50 ms, like the real model
    # warm the worker pool (thread creation is a one-off cost, not blocking by the model)
    await asyncio.gather(*(score_sentence(scorer, "Warm [P1].", PASSAGES) for _ in range(10)))
    t = time.perf_counter()
    await score_sentence(scorer, "The encoder has N = 6 layers [P1].", PASSAGES)
    single = time.perf_counter() - t
    gaps: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    async def timed() -> float:
        start = time.perf_counter()
        await score_sentence(scorer, "The encoder has N = 6 layers [P1].", PASSAGES)
        return time.perf_counter() - start

    beat = asyncio.create_task(heartbeat())
    durations = await asyncio.gather(*(timed() for _ in range(10)))
    done.set()
    await beat
    assert max(durations) < 3 * single, (single, durations)
    # the loop kept running: had scoring run on it, one gap would be >= 10 x 50 ms
    assert max(gaps) < 0.05, gaps


async def test_a_sentence_is_scored_against_its_best_cited_passage() -> None:
    class ByPassage:
        def score(self, sentence: str, passage: str) -> float:
            return 0.9 if passage == PASSAGES[1].text else 0.2

    value = await score_sentence(ByPassage(), "Heads and layers [P1][P2].", PASSAGES)
    assert value == 0.9
    assert await score_sentence(ByPassage(), "No citation here.", PASSAGES) is None


# 8 and 9: the sentence event -------------------------------------------------------------
async def test_with_l4_disabled_the_scorer_is_not_called_and_confidence_is_none(
        env: Env) -> None:
    cid = await collection(env, l4=False)
    harness = Harness(env, [answer(S1, S2)])
    scorer = FakeScorer()
    harness.app.state.relay.entailment = scorer
    stream = await ask(harness.app, question(cid))
    sentences = stream.of("sentence")
    assert len(sentences) == 2 and all(s["confidence"] is None for s in sentences)
    assert scorer.calls == []


async def test_with_l4_enabled_every_sentence_event_carries_a_confidence(env: Env) -> None:
    cid = await collection(env)
    harness = Harness(env, [answer(S1, S2)])
    scorer = FakeScorer(value=0.73)
    harness.app.state.relay.entailment = scorer
    stream = await ask(harness.app, question(cid))
    sentences = stream.of("sentence")
    assert [s["confidence"] for s in sentences] == [0.73, 0.73]
    assert len(scorer.calls) == 2
    sentence, passage = scorer.calls[0]
    assert "[P" not in sentence and "Basalt" in passage  # tags removed; the cited text
    assert stream.of("final")[0]["verdict"] == "VERIFIED"  # advisory: nothing blocked


async def test_a_low_confidence_never_blocks_an_answer(env: Env) -> None:
    cid = await collection(env)
    harness = Harness(env, [answer(S1, S2)])
    harness.app.state.relay.entailment = FakeScorer(value=0.01)
    final = (await ask(harness.app, question(cid))).of("final")[0]
    assert (final["status"], final["verdict"], final["partial"]) == ("ok", "VERIFIED", False)


async def test_buffered_collections_get_confidences_too(env: Env) -> None:
    cid = await collection(env, buffered=True)
    harness = Harness(env, [answer(S1, S2)])
    harness.app.state.relay.entailment = FakeScorer(value=0.6)
    sentences = (await ask(harness.app, question(cid))).of("sentence")
    assert [s["confidence"] for s in sentences] == [0.6, 0.6]


async def test_without_a_loaded_model_confidence_is_none(env: Env) -> None:
    cid = await collection(env)
    harness = Harness(env, [answer(S1)])  # the service has no scorer (model not installed)
    sentences = (await ask(harness.app, question(cid))).of("sentence")
    assert sentences and all(s["confidence"] is None for s in sentences)


# the per-collection setting ---------------------------------------------------------------
async def test_l4_enabled_defaults_to_true_and_is_stored(env: Env) -> None:
    on = await env.store.collections.create(DEV_TENANT_ID, "a")
    off = await env.store.collections.create(DEV_TENANT_ID, "b", l4_enabled=False)
    assert (await env.store.collections.get_by_id(on.id)).l4_enabled is True  # type: ignore[union-attr]
    assert (await env.store.collections.get_by_id(off.id)).l4_enabled is False  # type: ignore[union-attr]


# 10: the calibration run ------------------------------------------------------------------
@pytest.mark.skipif(not Path("evidence/experiments/exp-20261113T064542Z-c482696").is_dir(),
                    reason="v1 evidence not present")
def test_the_calibration_run_covers_the_21_v1_questions_from_recorded_answers() -> None:
    from relay.experiments.l4_calibration import calibration_rows, calibration_table

    run = Path("evidence/experiments/exp-20261113T064542Z-c482696")
    rows = calibration_rows(run, FakeScorer(value=0.5), labels={})
    asked = {json.loads(line)["question_id"]
             for line in (run / "traces.jsonl").read_text(encoding="utf-8").splitlines()}
    assert len(asked) == 21  # all 21 v1 questions were read
    # q18 was refused before any model call in every setting: it has no sentence to score
    assert {r.question_id for r in rows} == asked - {"q18"}
    assert {r.setting for r in rows} == {"small_fast", "balanced_rerank", "large_wide"}
    shown = [r for r in rows if r.l2_passed and not r.l3_refusal]
    assert len(shown) == 224 and all(r.l4 == 0.5 for r in shown)
    table = calibration_table(rows, thresholds=(0.3, 0.5, 0.7, 0.9))
    assert [line.threshold for line in table] == [0.3, 0.5, 0.7, 0.9]


# start-up: the scorer is loaded once, or L4 is off with a note -------------------------
async def test_without_the_model_files_the_service_starts_with_l4_off(tmp_path: Path) -> None:
    from relay.app import compose
    from relay.app.settings import with_overrides
    from tests.test_compose_collections import env as no_env
    from tests.test_compose_collections import settings_for
    from tests.test_index_registry import FakeEmbedder
    from tests.test_pipeline_and_verifier import StubRetriever

    settings = with_overrides(settings_for(tmp_path, index_dir=tmp_path / "none"),
                              {"verifier": {"l4_model_dir": str(tmp_path / "missing")}})
    async with compose.open_runtime(settings, no_env(),
                                    retrievers={"small_fast": StubRetriever(PASSAGES)},
                                    collection_embedder=FakeEmbedder()) as runtime:
        assert runtime.app.state.relay.entailment is None
        assert any("L4" in note and "off" in note for note in runtime.notes)


@needs_model
async def test_with_the_model_files_the_scorer_is_loaded_once_at_start_up(
        tmp_path: Path) -> None:
    from relay.app import compose
    from relay.app.settings import with_overrides
    from tests.test_compose_collections import env as no_env
    from tests.test_compose_collections import settings_for
    from tests.test_index_registry import FakeEmbedder
    from tests.test_pipeline_and_verifier import StubRetriever

    settings = with_overrides(settings_for(tmp_path, index_dir=tmp_path / "none"),
                              {"verifier": {"l4_model_dir": str(L4_MODEL)}})
    async with compose.open_runtime(settings, no_env(),
                                    retrievers={"small_fast": StubRetriever(PASSAGES)},
                                    collection_embedder=FakeEmbedder()) as runtime:
        scorer = runtime.app.state.relay.entailment
        assert isinstance(scorer, EntailmentScorer)
        assert any("L4" in note and "on" in note for note in runtime.notes)


async def test_a_failing_model_never_fails_the_answer(env: Env) -> None:
    """Advisory means advisory: an error inside the scorer leaves confidence None."""
    class Broken:
        def score(self, sentence: str, passage: str) -> float:
            raise RuntimeError("onnxruntime failed")

    cid = await collection(env)
    harness = Harness(env, [answer(S1, S2)])
    harness.app.state.relay.entailment = Broken()
    stream = await ask(harness.app, question(cid))
    assert [s["confidence"] for s in stream.of("sentence")] == [None, None]
    final = stream.of("final")[0]
    assert (final["status"], final["verdict"]) == ("ok", "VERIFIED")


@pytest.mark.skipif(not Path("evidence/experiments/exp-20261113T064542Z-c482696").is_dir(),
                    reason="v1 evidence not present")
def test_a_label_applies_only_to_the_passage_it_was_judged_against() -> None:
    """Review finding: the same tag number points to different chunks in different
    retrieval settings, so labels are keyed on the cited chunk ids, not the tag numbers."""
    from relay.experiments.l4_calibration import calibration_rows

    run = Path("evidence/experiments/exp-20261113T064542Z-c482696")
    rows = calibration_rows(run, FakeScorer(), labels={})
    sentence = "This instability prevents the model from learning consistent chunking boundaries  ."
    pairs = {(r.setting, r.chunk_ids) for r in rows if r.sentence == sentence}
    assert len({chunks for _, chunks in pairs}) == 2  # same words, two different passages
    (setting, chunks), *_ = sorted(pairs)
    labelled = calibration_rows(run, FakeScorer(), labels={(sentence, chunks): "supported"})
    got = {(r.setting, r.label) for r in labelled if r.sentence == sentence}
    assert (setting, "supported") in got and any(label is None for _, label in got)


@pytest.mark.parametrize("damage", ["no_id2label", "bad_tokenizer", "bad_model"])
async def test_a_corrupt_model_leaves_l4_off_instead_of_stopping_the_service(
        tmp_path: Path, damage: str) -> None:
    """Review finding: onnxruntime and tokenizers raise their own error types (not OSError),
    so a damaged download must still leave L4 off rather than fail start-up."""
    from relay.app import compose
    from relay.app.settings import with_overrides
    from tests.test_compose_collections import env as no_env
    from tests.test_compose_collections import settings_for
    from tests.test_index_registry import FakeEmbedder
    from tests.test_pipeline_and_verifier import StubRetriever

    model = tmp_path / "model"
    (model / "onnx").mkdir(parents=True)
    (model / "config.json").write_text(
        '{"model_type": "x"}' if damage == "no_id2label" else
        '{"id2label": {"0": "contradiction", "1": "entailment", "2": "neutral"}}')
    (model / "tokenizer.json").write_text("{not json" if damage == "bad_tokenizer" else "{}")
    (model / "onnx" / "m.onnx").write_bytes(b"not a model")
    settings = with_overrides(settings_for(tmp_path, index_dir=tmp_path / "none"), {
        "verifier": {"l4_model_dir": str(model), "l4_model_file": "onnx/m.onnx"}})
    async with compose.open_runtime(settings, no_env(),
                                    retrievers={"small_fast": StubRetriever(PASSAGES)},
                                    collection_embedder=FakeEmbedder()) as runtime:
        assert runtime.app.state.relay.entailment is None
        assert any("L4" in note and "could not be loaded" in note for note in runtime.notes)


async def test_leaving_while_a_buffered_answer_is_scored_records_its_real_spend(
        env: Env) -> None:
    """Review finding: the model call is over (its usage known) while L4 scores; a client
    leaving then must not record the flight as 'provider not called'."""
    from typing import Any

    cid = await collection(env, buffered=True)
    records: list[dict[str, Any]] = []
    harness = Harness(env, [answer(S1, S2)])
    harness.app.state.relay.entailment = FakeScorer(delay_s=0.3)
    harness.app.state.relay.record_trace = records.append
    gone = asyncio.Event()

    def leave(event: tuple[float, str, dict[str, Any]]) -> None:
        if event[1] == "status" and event[2]["stage"] == "verifying":
            gone.set()  # the answer is generated; scoring has started

    await asyncio.wait_for(ask(harness.app, question(cid), gone=gone, on_event=leave), 5)
    for _ in range(100):
        if any(r.get("end") == "cancelled" for r in records):
            break
        await asyncio.sleep(0.01)
    cancelled = next(r for r in records if r.get("end") == "cancelled")
    assert cancelled["provider_called"] is True and cancelled["tokens_in"] > 0
    assert cancelled["usage_source"] != "none"
