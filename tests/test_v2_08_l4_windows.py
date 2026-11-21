"""V2-08 prerequisite (V2-07 follow-up): L4 scores a sentence against 2-sentence windows of
the passage, all windows in ONE batched ONNX call, with at most 4 ONNX threads per call.
Owner decision (V2-08 prompt). Real-model tests are skipped when the model is absent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from relay.orchestrator.verifier import EntailmentScorer, passage_windows

L4_MODEL = Path("data/models/nli/nli-MiniLM2-L6-H768")
needs_model = pytest.mark.skipif(not (L4_MODEL / "config.json").is_file(),
                                 reason="the L4 model is not downloaded")


def test_a_passage_becomes_overlapping_two_sentence_windows() -> None:
    passage = "One is here. Two is there. Three is last. Four ends it."
    assert passage_windows(passage) == ["One is here. Two is there.",
                                        "Two is there. Three is last.",
                                        "Three is last. Four ends it."]


def test_a_short_passage_is_one_window() -> None:
    assert passage_windows("Only one sentence here.") == ["Only one sentence here."]
    assert passage_windows("First. Second.") == ["First. Second."]
    assert passage_windows("") == [""]


def test_windows_are_bounded_for_very_long_passages() -> None:
    passage = " ".join(f"Sentence number {i} is here." for i in range(200))
    windows = passage_windows(passage)
    assert 1 <= len(windows) <= 64  # one batch stays bounded


class CountingSession:
    def __init__(self, inner: Any) -> None:
        self.inner, self.calls, self.batches = inner, 0, []

    def run(self, outputs: Any, feed: dict[str, Any]) -> Any:
        self.calls += 1
        self.batches.append(next(iter(feed.values())).shape[0])
        return self.inner.run(outputs, feed)

    def get_inputs(self) -> Any:
        return self.inner.get_inputs()


@needs_model
def test_all_windows_are_scored_in_one_batched_call() -> None:
    scorer = EntailmentScorer(L4_MODEL, model_file="onnx/model_quint8_avx2.onnx")
    counting = CountingSession(scorer._session)
    scorer._session = counting  # type: ignore[assignment]
    passage = ("Recurrent models are sequential. The encoder is composed of a stack of N = 6 "
               "identical layers. Each layer has two sub-layers. Dropout is applied.")
    supported = scorer.score("The encoder has six layers.", passage)
    # one call; the 2 windows sharing the most words with the sentence (of 3)
    assert counting.calls == 1 and counting.batches == [2]
    unrelated = scorer.score("The model was trained on speech data.", passage)
    assert 0.0 <= unrelated < supported <= 1.0
    assert supported > 0.5  # a window holding the evidence entails it (whole passage: ~0.05)


@needs_model
def test_onnx_threads_are_capped_at_four() -> None:
    assert EntailmentScorer(L4_MODEL, model_file="onnx/model_quint8_avx2.onnx",
                            threads=8).threads == 4
    assert EntailmentScorer(L4_MODEL, model_file="onnx/model_quint8_avx2.onnx",
                            threads=2).threads == 2


def test_the_default_thread_setting_is_four() -> None:
    from relay.app.settings import RelaySettings

    assert RelaySettings().verifier.l4_threads == 4


def test_the_windows_sharing_most_words_with_the_sentence_are_scored() -> None:
    """V2-08 measurement: the top-2 windows by word overlap separate the V2-07 labels as
    well as all windows (AUROC 0.771 vs 0.768) at a quarter of the latency (178 vs 685 ms)."""
    from relay.orchestrator.verifier import best_windows

    windows = ["Dropout is applied to each layer.", "The encoder stacks six identical layers.",
               "Training used eight GPUs.", "The decoder also stacks six layers."]
    assert best_windows("The encoder has six identical layers.", windows) == [
        "The encoder stacks six identical layers.", "The decoder also stacks six layers."]
    assert best_windows("Anything.", ["Only one."]) == ["Only one."]
    assert best_windows("No overlap at all.", windows) == windows[:2]  # stable on ties
