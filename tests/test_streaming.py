"""V2-05 tests: incremental splitting, per-sentence verification and the sentence stream
(tests 1-5 and 11 of the phase prompt). Pure code and seeded random cases; no network.

Property tests use a seeded generator (owner decision 2026-11-19: no new dependency).
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from relay.orchestrator.streaming import (
    BackgroundScores,
    IncrementalSentenceSplitter,
    SentenceStream,
    StoppedEvent,
    StreamingVerifier,
    StreamRefusal,
    VerifiedSentence,
)
from relay.orchestrator.verifier import split_sentences
from tests.test_pipeline_and_verifier import PASSAGES

GOOD = ("The encoder has N = 6 identical layers [P1].",
        "The model uses h = 8 heads [P2].",
        "Each layer has two sub-layers [P1].")
TABLE3 = Path("evidence/experiments/exp-20261113T064542Z-c482696/traces.jsonl")


def chunks(text: str, rng: random.Random) -> list[str]:
    """``text`` cut into random token-like pieces (1-12 characters)."""
    out, i = [], 0
    while i < len(text):
        step = rng.randint(1, 12)
        out.append(text[i:i + step])
        i += step
    return out


def split_streaming(pieces: Sequence[str]) -> list[str]:
    splitter = IncrementalSentenceSplitter()
    emitted = [s for piece in pieces for s in splitter.feed(piece)]
    return emitted + splitter.flush()


async def token_stream(pieces: Sequence[str], log: list[str]) -> AsyncIterator[str]:
    try:
        for piece in pieces:
            log.append(piece)
            yield piece
    finally:
        log.append("<closed>")


async def collect(stream: SentenceStream) -> list[object]:
    return [event async for event in stream]


# 1 --------------------------------------------------------------------------------------
def test_the_splitter_handles_tags_decimals_ellipses_and_abbreviations() -> None:
    cases = {
        "Attention is all you need. It works.": ["Attention is all you need.", "It works."],
        "Transformers use attention [1] in every layer. Next one.":
            ["Transformers use attention [1] in every layer.", "Next one."],
        "Pi is about 3.14 here. Done.": ["Pi is about 3.14 here.", "Done."],
        "Wait... then it goes on. Done.": ["Wait... then it goes on.", "Done."],
        "Use small models, e.g. distilled ones. Done.":
            ["Use small models, e.g. distilled ones.", "Done."],
        "The claim holds. [P1] The next one too [P2].":
            ["The claim holds. [P1]", "The next one too [P2]."],
    }
    for text, expected in cases.items():
        assert split_streaming(list(text)) == expected, text  # one character per token
        assert split_streaming([text]) == expected, text


def test_a_sentence_is_held_until_a_following_tag_is_known() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("The claim holds. [P") == []  # the tag may belong to it
    assert splitter.feed("1] Next") == ["The claim holds. [P1]"]


# 2 --------------------------------------------------------------------------------------
def test_flush_returns_the_last_fragment_without_terminal_punctuation() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("First sentence [P1]. And an unfinished tail [P2]") == [
        "First sentence [P1]."]
    assert splitter.flush() == ["And an unfinished tail [P2]"]
    assert splitter.flush() == []


def test_streaming_splits_exactly_as_the_whole_answer_splitter() -> None:
    """Property: for any chunking, the emitted sentences are v1's ``split_sentences``."""
    texts = [
        " ".join(GOOD), "1. First item [P1].\n2. Second item [P2].\n\nDone [P1].",
        "Claim. [P1][P2] Next claim [P2]. Last [P1]", "See Fig. 3 and Sec. 2 [P1]. Ok [P2].",
        "Values: 3.5 and 12.5 Hz [P1]... Then more [P2]! Why? Because [P1].",
        "- bullet one [P1]\n- bullet two [P2]\n* starred [P1]", "INSUFFICIENT_EVIDENCE",
    ]
    if TABLE3.is_file():  # the 63 real recorded answers, when the evidence is present
        for line in TABLE3.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)["outcome"]["trace"].get("raw_answer")
            if raw:
                texts.append(raw)
    rng = random.Random(20261119)
    for text in texts:
        for _ in range(40):
            assert split_streaming(chunks(text, rng)) == split_sentences(text), text


# 3 --------------------------------------------------------------------------------------
def test_the_streaming_verifier_passes_a_cited_sentence_and_fails_an_uncited_one() -> None:
    verifier = StreamingVerifier()
    good = verifier.verify_sentence(GOOD[0], PASSAGES)
    assert good.passed and good.reason_class is None and good.confidence is None
    assert [c.tag for c in good.citations] == [1]
    uncited = verifier.verify_sentence("The encoder has N = 6 identical layers.", PASSAGES)
    assert not uncited.passed and uncited.reason_class == "uncited"
    ungrounded = verifier.verify_sentence("The encoder has 999 layers [P1].", PASSAGES)
    assert (ungrounded.passed, ungrounded.reason_class) == (False, "ungrounded")
    wrong_tag = verifier.verify_sentence("The encoder has N = 6 layers [P9].", PASSAGES)
    assert (wrong_tag.passed, wrong_tag.reason_class) == (False, "unknown_citation")


# 4 --------------------------------------------------------------------------------------
async def test_the_stream_cancels_the_token_generator_on_a_failed_sentence() -> None:
    log: list[str] = []
    text = f"{GOOD[0]} The encoder has 999 layers [P1]. {GOOD[1]} {GOOD[2]}"
    pieces = chunks(text, random.Random(4))
    events = await collect(SentenceStream(token_stream(pieces, log), PASSAGES))
    assert [type(e) for e in events] == [VerifiedSentence, StoppedEvent]
    stopped = events[1]
    assert isinstance(stopped, StoppedEvent)
    assert (stopped.reason_class, stopped.partial) == ("ungrounded", True)
    assert log[-1] == "<closed>"  # the generator was closed early
    assert len(log) - 1 < len(pieces)  # ... before all its tokens were produced


# 5 --------------------------------------------------------------------------------------
async def test_the_stream_yields_every_sentence_when_all_pass() -> None:
    log: list[str] = []
    stream = SentenceStream(token_stream(chunks(" ".join(GOOD), random.Random(5)), log),
                            PASSAGES)
    events = await collect(stream)
    assert [e.text for e in events if isinstance(e, VerifiedSentence)] == list(GOOD)
    assert all(isinstance(e, VerifiedSentence) for e in events)
    assert stream.text == " ".join(GOOD)


async def test_a_lone_refusal_marker_is_a_refusal_and_marker_with_content_stops() -> None:
    refusal = await collect(SentenceStream(token_stream(["INSUFFICIENT_", "EVIDENCE"], []),
                                           PASSAGES))
    assert [type(e) for e in refusal] == [StreamRefusal]
    mixed = await collect(SentenceStream(
        token_stream([f"{GOOD[0]} INSUFFICIENT_EVIDENCE. More."], []), PASSAGES))
    # the marker is checked on the whole answer (as v1 does): nothing is released
    assert [type(e) for e in mixed] == [StoppedEvent]
    assert mixed[0].reason_class == "refusal_with_content"  # type: ignore[union-attr]


# 11 -------------------------------------------------------------------------------------
BAD = ("The encoder has 999 layers [P1].", "The model has no citation at all.",
       "It uses Moshi heads [P1].", "The model uses h = 8 heads [P7].")


async def test_property_no_failed_sentence_is_ever_yielded() -> None:
    """For random answers with one failing sentence at a random position, cut into random
    tokens: every sentence before it is yielded, the failed one and everything after never."""
    rng = random.Random(1119)
    for case in range(500):
        sentences = [rng.choice(GOOD) for _ in range(rng.randint(0, 5))]
        bad_at = rng.randint(0, len(sentences))
        bad = rng.choice(BAD)
        sentences.insert(bad_at, bad)
        text = " ".join(sentences)
        events = await collect(SentenceStream(token_stream(chunks(text, rng), []), PASSAGES))
        texts = [e.text for e in events if isinstance(e, VerifiedSentence)]
        assert texts == sentences[:bad_at], (case, text)
        assert all(bad not in t for t in texts)
        assert isinstance(events[-1], StoppedEvent)
        assert events[-1].partial == (bad_at > 0)


# --- F-02c: L4 in the background (l4_mode "async") -----------------------------------------


class _SlowScorer:
    """Blocks until released; records how many calls ran at once."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def score(self, sentence: str, passage: str) -> float:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.release.wait(5.0)
            return 0.25 if "heads" in sentence else 0.75
        finally:
            with self._lock:
                self.active -= 1


async def test_background_scores_never_delay_the_caller_and_keep_sentence_order() -> None:
    scorer = _SlowScorer()
    scores = BackgroundScores(scorer, PASSAGES, asyncio.Semaphore(1))
    started = time.perf_counter()
    scores.submit("The encoder has six layers [P1].")
    scores.submit("It uses eight heads [P2].")
    scores.submit("No tag in this one.")
    assert time.perf_counter() - started < 0.05  # submitting never waits for the model
    await asyncio.sleep(0.05)
    assert scorer.peak == 1  # the gate lets one model call run at a time
    scorer.release.set()
    assert await scores.collect() == [0.75, 0.25, None]  # sentence order; no tag: None
    assert scorer.peak == 1


async def test_background_scores_are_cancelled_cleanly() -> None:
    scorer = _SlowScorer()
    scores = BackgroundScores(scorer, PASSAGES, asyncio.Semaphore(1))
    scores.submit("The encoder has six layers [P1].")
    scores.cancel()
    scorer.release.set()
    await asyncio.sleep(0.05)
    assert scores.pending == 0


async def test_collecting_background_scores_is_bounded_and_unfinished_ones_are_none() -> None:
    """Review F-02c: advisory work must never hold the final event indefinitely."""
    scorer = _SlowScorer()  # never released
    scores = BackgroundScores(scorer, PASSAGES, asyncio.Semaphore(1))
    scores.submit("The encoder has six layers [P1].")
    scores.submit("It uses eight heads [P2].")
    started = time.perf_counter()
    assert await scores.collect(timeout_s=0.1) == [None, None]
    assert time.perf_counter() - started < 1.0
    await asyncio.sleep(0.05)
    assert scores.pending == 0  # the unfinished ones were cancelled
    scorer.release.set()
