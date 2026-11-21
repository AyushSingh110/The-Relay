"""An offline evaluation pipeline: the calibrated stand-in in process, v1's recorded passages
(plan V2-12, the CI gate).

No port, no network, no index, no embedding model: the stand-in app is reached through
``httpx.ASGITransport``, its calibrated profile is read (never written) from the v1
evidence, faults are switched off and time is scaled down (latency is not what the gate
measures), and retrieval is a ``ReplayRetriever`` over a v1 run's traces.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from relay.api.app import create_app
from relay.app.settings import CALIBRATED_PROFILE
from relay.control.admission import AdmissionQueue
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.eval.runner import ReplayRetriever
from relay.orchestrator.pipeline import Pipeline, PipelineConfig
from relay.providers.http_provider import HttpOpenAIProvider
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.standin.server import API_PREFIX, create_standin_app, load_profile

REPLAY_RUN: Final = "exp-20261113T064542Z-c482696"  # v1 retrieval_eval: 21 questions x 3
STANDIN_MODEL: Final = "qwen/qwen3.8-27b"


@dataclass
class ReplayPipeline:
    pipeline: Pipeline
    retriever: ReplayRetriever
    model_id: str
    calls: Callable[[], int]


@asynccontextmanager
async def replay_pipeline(*, evidence_dir: Path, questions: Mapping[str, str],
                          retrieval_setting: str = "small_fast", run_id: str = REPLAY_RUN,
                          profile_path: Path | None = None, time_scale: float = 0.001,
                          overrides: Mapping[str, Any] | None = None,
                          ) -> AsyncGenerator[ReplayPipeline]:
    """``questions`` maps owner question ids to their text (to match v1's traces). The
    profile defaults to the calibrated one inside ``evidence_dir`` (any working directory)."""
    if profile_path is None:
        profile_path = evidence_dir / CALIBRATED_PROFILE.relative_to("evidence")
    profile = load_profile(profile_path).with_overrides({
        "time_scale": time_scale,
        "faults": {"error_rate": 0.0, "straggler_rate": 0.0, "stall_rate": 0.0},
        # quota is not what the gate measures: no throttling by the calibrated TPM/RPD
        "limits": {"rpm": None, "tpm": None, "rpd": None, "tpd": None},
        **(overrides or {})})
    standin = create_standin_app(profile)
    provider = HttpOpenAIProvider(base_url=f"http://standin{API_PREFIX}", source="stand-in",
                                  transport=httpx.ASGITransport(app=standin))
    retriever = ReplayRetriever.from_evidence(evidence_dir, run_id, retrieval_setting,
                                              questions)
    model = STANDIN_MODEL if STANDIN_MODEL in profile.models else sorted(profile.models)[0]
    app = create_app(
        provider=provider, retrievers={retrieval_setting: retriever},
        top_k={retrieval_setting: RETRIEVAL_SETTINGS[retrieval_setting].top_k},
        pipeline_config=PipelineConfig(model_id=model, max_completion_tokens=512),
        guard=ProviderGuard(GuardConfig(max_concurrency=1)),
        queue=AdmissionQueue(capacity=4, service_rate_per_s=10.0), corpus_sha256="replay",
        cache_mode="off")
    try:
        yield ReplayPipeline(app.state.relay.pipeline, retriever, model,
                             lambda: int(standin.state.standin.stats.requests))
    finally:
        await provider.aclose()


__all__ = ["REPLAY_RUN", "STANDIN_MODEL", "ReplayPipeline", "replay_pipeline"]
