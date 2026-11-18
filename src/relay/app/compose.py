"""Composition root: build the running service from ``RelaySettings`` (plan V2-01).

This is what the v1 load script did by hand: provider, ProviderGuard, admission queue,
retrievers and the P08 app. Nothing in the v1 modules changes.

``open_runtime`` is an async context manager because an embedded stand-in is a second
uvicorn server on 127.0.0.1 that must start before the provider and stop after it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI

from relay.api.app import RelayService, create_app
from relay.app.settings import LaneSettings, RelaySettings
from relay.config import Settings
from relay.control.admission import (
    AdmissionQueue,
    CapacityDerivation,
    CapacityInputs,
    derive_capacity,
)
from relay.control.cache import corpus_digest
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.errors import ConfigurationError
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.orchestrator.pipeline import PipelineConfig
from relay.orchestrator.tools import PassageRetriever
from relay.providers.base import ChatProvider, RealCallBudget
from relay.providers.http_provider import HttpOpenAIProvider
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.schemas import Source
from relay.standin.profile import StandinProfile, uncalibrated_defaults
from relay.standin.server import create_standin_app, load_profile, serve_on_loopback

CACHE_MODES: Final[dict[str, str]] = {"off": "off", "memory": "exact"}


@dataclass(frozen=True)
class Runtime:
    """What ``relay serve`` runs, and what it reports at start-up (no secrets)."""

    app: FastAPI
    lane: LaneSettings
    source: Source
    provider_url: str
    capacity: CapacityDerivation
    real_call_cap: int | None

    @property
    def service(self) -> RelayService:
        return self.app.state.relay

    def describe(self) -> list[str]:
        cap = self.capacity
        lines = [
            f"lane: {self.lane.name} ({self.lane.kind}, model {self.lane.model}, "
            f"source {self.source})",
            f"provider: {self.provider_url}",
            f"admission: capacity {cap.capacity} (bound {cap.binding_bound}, "
            f"service rate {cap.service_rate_per_s * 60:.1f}/min)",
        ]
        if self.real_call_cap is not None:
            lines.append(f"real-call cap for this process: {self.real_call_cap}")
        return lines


def guard_config(settings: RelaySettings) -> GuardConfig:
    g = settings.guard
    return GuardConfig(max_concurrency=g.max_concurrency, rpm_limit=g.rpm, tpm_limit=g.tpm,
                       rpd_limit=g.rpd, tpd_limit=g.tpd,
                       daily_safety_fraction=g.daily_safety_fraction)


def capacity_derivation(settings: RelaySettings) -> CapacityDerivation:
    """v1 ARCHITECTURE §15. An explicit ``queue.capacity`` overrides the derived value."""
    g, q = settings.guard, settings.queue
    if g.tpm is None and g.rpm is None:
        raise ConfigurationError("set guard.tpm or guard.rpm: the queue capacity is derived "
                                 "from them")
    derived = derive_capacity(CapacityInputs(
        patience_quantile_s=q.patience_median_s,
        tpm=None if g.tpm is None else float(g.tpm),
        rpm=None if g.rpm is None else float(g.rpm),
        tokens_per_answer=q.tokens_per_answer,
    ))
    if q.capacity is None:
        return derived
    return derived.model_copy(update={"capacity": q.capacity})


def cache_mode(settings: RelaySettings) -> str:
    mode = settings.cache.mode
    if mode not in CACHE_MODES:
        raise ConfigurationError(f"cache mode {mode!r} is not available yet: the SQLite "
                                 "store arrives in V2-02; use 'memory' or 'off'")
    return CACHE_MODES[mode]


def standin_profile(lane: LaneSettings) -> StandinProfile:
    if lane.profile is None:
        profile = uncalibrated_defaults()
    else:
        try:
            profile = load_profile(lane.profile)
        except (OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            raise ConfigurationError(f"stand-in profile {lane.profile} could not be loaded "
                                     f"({type(exc).__name__})") from None
    if lane.model not in profile.models:
        raise ConfigurationError(f"the stand-in profile has no model {lane.model!r}; "
                                 f"it has {sorted(profile.models)}")
    return profile


def corpus_sha256(settings: RelaySettings) -> str:
    papers = load_corpus_manifest(settings.paths.papers_dir).papers
    if not papers:
        raise ConfigurationError(f"no corpus manifest in {settings.paths.papers_dir}; "
                                 "run scripts/ingest_papers.py")
    return corpus_digest(p.sha256 for p in papers)


def _default_retrievers(settings: RelaySettings,
                        ) -> tuple[dict[str, PassageRetriever], frozenset[str]]:
    from relay.experiments.eval_runner import load_retrievers  # heavy: loads ONNX models

    return load_retrievers(settings.retrieval.settings, papers_dir=settings.paths.papers_dir,
                           indexes_dir=settings.paths.index_dir,
                           models_dir=settings.paths.models_dir)


@asynccontextmanager
async def open_runtime(settings: RelaySettings, env: Settings, *,
                       retrievers: Mapping[str, PassageRetriever] | None = None,
                       vocabulary: frozenset[str] | None = None,
                       ) -> AsyncGenerator[Runtime]:
    """Start everything the primary lane needs and yield the ready app."""
    lane = settings.primary_lane
    mode = cache_mode(settings)
    capacity = capacity_derivation(settings)
    # Blocking work (file hashing, ONNX model and index loading) runs in a worker thread
    # and finishes before anything else is started on the event loop.
    corpus = await asyncio.to_thread(corpus_sha256, settings)
    if retrievers is None:
        retrievers, vocabulary = await asyncio.to_thread(_default_retrievers, settings)
    profile = standin_profile(lane) if lane.kind == "standin" and lane.embedded else None
    async with AsyncExitStack() as stack:
        provider: ChatProvider
        real_cap: int | None = None
        if lane.kind == "standin":
            url = lane.base_url or ""
            if profile is not None:
                url = await stack.enter_async_context(
                    serve_on_loopback(create_standin_app(profile)))
            source: Source = "stand-in"
            provider = HttpOpenAIProvider(base_url=url, source=source)
        else:
            if env.groq_api_key is None or not env.groq_api_key.get_secret_value().strip():
                raise ConfigurationError("the groq lane needs GROQ_API_KEY in the "
                                         "environment or .env")
            if env.max_real_calls < 1:
                raise ConfigurationError("the groq lane needs MAX_REAL_CALLS >= 1: it is the "
                                         "hard cap on real calls for this process")
            real_cap = env.max_real_calls
            url = lane.base_url or env.groq_base_url
            source = "real"
            provider = HttpOpenAIProvider(base_url=url, source=source,
                                          api_key=env.groq_api_key,
                                          call_budget=RealCallBudget(real_cap))
        stack.push_async_callback(provider.aclose)
        guard = ProviderGuard(guard_config(settings))
        queue = AdmissionQueue.from_derivation(
            capacity, max_active=settings.queue.max_active or settings.guard.max_concurrency)
        app = create_app(
            provider=provider, retrievers=retrievers,
            top_k={name: RETRIEVAL_SETTINGS[name].top_k for name in retrievers},
            pipeline_config=PipelineConfig(model_id=lane.model,
                                           max_completion_tokens=lane.max_completion_tokens,
                                           reasoning_effort=lane.reasoning_effort),
            guard=guard, queue=queue, corpus_sha256=corpus,
            cache_mode=mode, vocabulary=vocabulary,  # type: ignore[arg-type]
        )
        yield Runtime(app=app, lane=lane, source=source, provider_url=url,
                      capacity=capacity, real_call_cap=real_cap)


__all__ = ["Runtime", "cache_mode", "capacity_derivation", "corpus_sha256", "guard_config",
           "open_runtime", "standin_profile"]
