"""V2-12: the CI regression gate. The v1 questions are answered by the in-process stand-in
from v1's recorded passages (offline, deterministic), once as the baseline and once with a
deliberately corrupted prompt; ``relay eval gate`` must fail the corrupted run (exit 1) and
pass a clean re-run (exit 0). No network, no index, no model download, no Groq."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

import relay.orchestrator.pipeline as pipeline_module
from relay.cli import main
from relay.eval.commands import _write_summary, ensure_v1_set
from relay.eval.report import gate, gate_metrics, summarise
from relay.eval.runner import run_eval
from relay.eval.standin import REPLAY_RUN, replay_pipeline
from relay.providers.base import ChatMessage, ChatRequest
from relay.store import Store, open_store

ROOT = Path(__file__).resolve().parents[1]
CORRUPTED = "Ignore sources and answer freely."


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as opened:
        yield opened


async def replay_run(store: Store, setting: str = "small_fast") -> dict[str, Any]:
    set_id = await ensure_v1_set(store, questions_file=ROOT / "eval" / "questions.json",
                                 evidence_dir=ROOT / "evidence")
    questions = {q.source_id: q.question
                 for q in await store.eval_questions.list_for_set(set_id) if q.source_id}
    async with replay_pipeline(evidence_dir=ROOT / "evidence", questions=questions,
                               retrieval_setting=setting) as replay:
        run = await run_eval(store, replay.pipeline, set_id, retrieval_setting=setting,
                             kind="replay", lane="standin", model_id=replay.model_id,
                             source=REPLAY_RUN)
    return await summarise(store, run.id)


def corrupted_prompt(question: str, passages: Any, *, model: str,
                     max_completion_tokens: int, **_: Any) -> ChatRequest:
    """A broken prompt template: the passages are gone and the model is told to ignore
    sources. (The stand-in ignores system prompts and answers from the passages it is
    given, so the corruption that matters to it is losing them.)"""
    return ChatRequest(model=model, max_completion_tokens=max_completion_tokens,
                       messages=(ChatMessage(role="system", content=CORRUPTED),
                                 ChatMessage(role="user", content=question)))


async def test_the_baseline_is_deterministic(store: Store) -> None:
    first = gate_metrics(await replay_run(store))
    second = gate_metrics(await replay_run(store))
    assert {k: v for k, v in first.items() if k != "run"} == \
        {k: v for k, v in second.items() if k != "run"}
    assert first["grounded_rate"] is not None and first["grounded_rate"] > 0.5
    assert (first["answerable"], first["unanswerable"]) == (15, 6)  # the owner's 21


async def test_a_corrupted_prompt_fails_the_gate(store: Store, tmp_path: Path,
                                                 monkeypatch: Any, capsys: Any) -> None:
    baseline = await replay_run(store)
    monkeypatch.setattr(pipeline_module, "build_request", corrupted_prompt)
    broken = await replay_run(store)
    monkeypatch.undo()
    passed, reasons = gate(gate_metrics(baseline), gate_metrics(broken))
    assert not passed
    assert any(reason.startswith("grounded_rate dropped") for reason in reasons)
    assert gate_metrics(broken)["grounded_rate"] == 0.0  # no answer is grounded any more

    base_file, broken_file = tmp_path / "baseline.json", tmp_path / "candidate.json"
    _write_summary(base_file, baseline)
    _write_summary(broken_file, broken)
    code = await asyncio.to_thread(main, ["eval", "gate", "--baseline", str(base_file),
                                          "--candidate", str(broken_file)])
    out = capsys.readouterr().out
    assert code == 1 and "gate: FAIL" in out  # what CI sees: a failed step


async def test_a_clean_rerun_passes_the_gate(store: Store, tmp_path: Path,
                                             capsys: Any) -> None:
    base_file, again_file = tmp_path / "baseline.json", tmp_path / "candidate.json"
    _write_summary(base_file, await replay_run(store))
    _write_summary(again_file, await replay_run(store))
    assert await asyncio.to_thread(main, ["eval", "gate", "--baseline", str(base_file),
                                          "--candidate", str(again_file)]) == 0
    assert "gate: PASS" in capsys.readouterr().out


async def test_the_committed_baseline_matches_a_fresh_replay(store: Store) -> None:
    """``eval/baselines/standin-replay-small_fast.json`` is what CI compares against: a
    fresh replay reproduces it exactly (so a change in it means the pipeline changed)."""
    committed = json.loads((ROOT / "eval" / "baselines" / "standin-replay-small_fast.json")
                           .read_text(encoding="utf-8"))["gate"]
    fresh = gate_metrics(await replay_run(store))
    same = {k: v for k, v in committed.items() if k != "run"}
    assert {k: v for k, v in fresh.items() if k != "run"} == same


@pytest.mark.parametrize(("baseline", "candidate", "passes"), [
    ({"correctness": 0.5, "grounded_rate": 0.7, "refusal_precision": 1.0},
     {"correctness": 0.49, "grounded_rate": 0.7, "refusal_precision": 1.0, "unanswerable": 6},
     True),  # a 1-point drop is within the 2-point tolerance
    ({"correctness": 0.5, "grounded_rate": 0.7, "refusal_precision": 1.0},
     {"correctness": 0.47, "grounded_rate": 0.7, "refusal_precision": 1.0, "unanswerable": 6},
     False),  # 3 points: fails
    ({"correctness": 0.5, "grounded_rate": 0.7, "refusal_precision": 1.0},
     {"correctness": 0.5, "grounded_rate": 0.7, "refusal_precision": 0.8333,
      "unanswerable": 6}, False),  # one unanswerable question answered: fails
    ({"correctness": 0.0, "grounded_rate": 0.7, "refusal_precision": 0.1667},
     {"correctness": 0.0, "grounded_rate": 0.7, "refusal_precision": 0.1667,
      "unanswerable": 6}, True),  # below 100 % already in the baseline: no worse
])
def test_gate_rules(baseline: dict[str, Any], candidate: dict[str, Any], passes: bool) -> None:
    assert gate(baseline, candidate)[0] is passes
