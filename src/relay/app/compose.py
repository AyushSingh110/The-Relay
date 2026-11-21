"""Composition root: build the running service from ``RelaySettings`` (plan V2-01).

This is what the v1 load script did by hand: provider, ProviderGuard, admission queue,
retrievers and the P08 app. Nothing in the v1 modules changes.

``open_runtime`` is an async context manager because an embedded stand-in is a second
uvicorn server on 127.0.0.1 that must start before the provider and stop after it.

In ``sqlite`` mode (V2-03) it also opens the collections: the development tenant, the
``v1-papers`` collection on first start-up (reading the v1 index, nothing re-embedded), the
per-collection index registry, the ingest worker, and the ``/v1`` collection routes. The
embedding and rerank models for collections load on first use.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from fastapi import FastAPI

from relay.api.app import MeteredProvider, RelayService, create_app
from relay.api.auth import DEV_KEY, Authenticator
from relay.api.routes.ask_v1 import AskV1Context, ask_v1_router
from relay.api.routes.requests import requests_router
from relay.app.bootstrap import V1_PRESET, ensure_dev_tenant, register_v1_papers
from relay.app.collections import LazyEmbedder, LazyReranker, StoreCollectionResolver
from relay.app.request_log import RequestLogSink
from relay.app.routes.ingestion import IngestionContext, fetch_arxiv_pdf, ingestion_router
from relay.app.routes.ui import mount_ui
from relay.app.settings import LaneGuardSettings, LaneSettings, RelaySettings
from relay.config import Settings
from relay.control.admission import (
    AdmissionQueue,
    CapacityDerivation,
    CapacityInputs,
    derive_capacity,
)
from relay.control.cache import corpus_digest
from relay.control.pricing import LanePricing
from relay.control.provider_guard import GuardConfig, ProviderGuard
from relay.control.router import ProviderLane, ProviderRouter, RoutedGenerator
from relay.errors import ConfigurationError, sanitize_error
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.ingestion.worker import FileLocator, IngestWorker
from relay.orchestrator.pipeline import AnswerGenerator, PipelineConfig
from relay.orchestrator.rewriter import LLMQueryRewriter, QueryRewriter, RuleQueryRewriter
from relay.orchestrator.tools import PassageRetriever
from relay.orchestrator.verifier import EntailmentScore, EntailmentScorer
from relay.providers.base import ChatProvider, RealCallBudget
from relay.providers.http_provider import HttpOpenAIProvider
from relay.retrieval.embedder import Embedder, Reranker
from relay.retrieval.hybrid_index import INDEX_MANIFEST, RETRIEVAL_SETTINGS
from relay.retrieval.registry import IndexRegistry
from relay.schemas import Source
from relay.standin.profile import StandinProfile, uncalibrated_defaults
from relay.standin.server import create_standin_app, load_profile, serve_on_loopback
from relay.store import Store, open_store

logger = logging.getLogger(__name__)

# The answer cache itself stays in memory in every mode; "sqlite" adds the metadata
# store and its request log (a persistent answer cache is not in the V2-02 schema).
CACHE_MODES: Final[dict[str, str]] = {"off": "off", "memory": "exact", "sqlite": "exact"}


@dataclass(frozen=True)
class Runtime:
    """What ``relay serve`` runs, and what it reports at start-up (no secrets)."""

    app: FastAPI
    lane: LaneSettings  # the primary lane
    source: Source
    provider_url: str
    capacity: CapacityDerivation
    real_call_cap: int | None
    store: Store | None = None
    request_log: RequestLogSink | None = None
    collections: StoreCollectionResolver | None = None
    ingest_worker: IngestWorker | None = None
    notes: tuple[str, ...] = ()
    ask_v1: AskV1Context | None = None  # POST /v1/ask (V2-05), sqlite mode
    fallback_lanes: tuple[LaneSettings, ...] = ()  # V2-09: the lanes after the primary

    @property
    def service(self) -> RelayService:
        return self.app.state.relay

    def describe(self) -> list[str]:
        cap = self.capacity
        lines = [
            f"lane: {self.lane.name} ({self.lane.kind}, model {self.lane.model}, "
            f"source {self.source})",
            f"provider: {self.provider_url}",
            *(f"fallback lane: {lane.name} ({lane.kind}, model {lane.model})"
              + (f", escalation target of {', '.join(_escalating(self, lane))}"
                 if _escalating(self, lane) else "")
              for lane in self.fallback_lanes),
            f"admission: capacity {cap.capacity} (bound {cap.binding_bound}, "
            f"service rate {cap.service_rate_per_s * 60:.1f}/min)",
        ]
        if self.real_call_cap is not None:
            lines.append(f"real-call cap for this process: {self.real_call_cap}")
        if self.store is not None:
            lines.append(f"store: {self.store.path} (schema v{self.store.schema_version}, "
                         f"journal {self.store.journal_mode}); request log on, answers cached in "
                         "memory")
        lines.extend(self.notes)
        return lines


def _escalating(runtime: Runtime, target: LaneSettings) -> list[str]:
    lanes = (runtime.lane, *runtime.fallback_lanes)
    return [lane.name for lane in lanes if lane.escalate_to == target.name]


def guard_config(settings: RelaySettings, lane: LaneSettings | None = None) -> GuardConfig:
    """``[guard]``, with the lane's own overrides (V2-09). Each lane gets its own guard."""
    g = settings.guard
    own = (None if lane is None else lane.guard) or LaneGuardSettings()
    return GuardConfig(
        max_concurrency=own.max_concurrency or g.max_concurrency,
        rpm_limit=own.rpm or g.rpm, tpm_limit=own.tpm or g.tpm, rpd_limit=own.rpd or g.rpd,
        tpd_limit=own.tpd or g.tpd,
        daily_safety_fraction=own.daily_safety_fraction or g.daily_safety_fraction)


def lane_pricing(lane: LaneSettings) -> LanePricing | None:
    p = lane.pricing
    return None if p is None else LanePricing(input_per_m=float(p.input_per_m),
                                              output_per_m=float(p.output_per_m))


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


def dev_key_enabled(settings: RelaySettings) -> bool:
    """``[auth] dev_key_enabled``, or (unset) whether the server listens on loopback only."""
    if settings.auth.dev_key_enabled is not None:
        return settings.auth.dev_key_enabled
    host = settings.server.host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a host name other than localhost: assume it is reachable


def admission_queue(settings: RelaySettings, capacity: CapacityDerivation) -> AdmissionQueue:
    """The queue with per-tenant buckets, priorities and (unless the capacity is set by
    hand) a capacity that follows the measured tokens per answer (V2-10)."""
    q = settings.queue
    return AdmissionQueue.from_derivation(
        capacity, max_active=q.max_active or settings.guard.max_concurrency,
        tenant_rate_per_s=capacity.service_rate_per_s * q.tenant_rate_share,
        tenant_burst=q.tenant_burst, adaptive=q.adaptive_capacity and q.capacity is None,
        ewma_alpha=q.ewma_alpha)


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


def _fastembed_embedder(models_dir: Path) -> Embedder:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from relay.retrieval.embedder import FastEmbedEmbedder

    return FastEmbedEmbedder(models_dir, local_files_only=True)


def _fastembed_reranker(models_dir: Path) -> Reranker:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from relay.retrieval.embedder import FastEmbedReranker

    return FastEmbedReranker(models_dir, local_files_only=True)


@dataclass(frozen=True)
class _Collections:
    resolver: StoreCollectionResolver
    worker: IngestWorker
    routes: IngestionContext
    note: str


async def _open_collections(settings: RelaySettings, store: Store, *,
                            embedder: Embedder | None,
                            reranker: Reranker | None) -> _Collections:
    paths, limits = settings.paths, settings.ingestion
    await ensure_dev_tenant(store)
    if (paths.index_dir / V1_PRESET / INDEX_MANIFEST).is_file():
        v1 = await register_v1_papers(store, papers_dir=paths.papers_dir,
                                      index_dir=paths.index_dir,
                                      collections_dir=paths.collections_dir)
        note = ("collections: v1-papers not registered (the store already has collections)"
                if v1 is None else
                f"collections: v1-papers ready (version {v1.version}, reads the v1 "
                f"{V1_PRESET} index)")
    else:
        note = (f"collections: v1-papers not registered (no v1 {V1_PRESET} index in "
                f"{paths.index_dir})")
        logger.warning(note)
    models_dir = paths.models_dir
    embedder = embedder or LazyEmbedder(lambda: _fastembed_embedder(models_dir))
    reranker = reranker or LazyReranker(lambda: _fastembed_reranker(models_dir))
    indexes = IndexRegistry(paths.collections_dir, embedder=embedder, reranker=reranker,
                            max_loaded=limits.max_loaded_collections)
    files = FileLocator(papers_dir=paths.papers_dir, uploads_dir=paths.uploads_dir)
    worker = IngestWorker(store, index_root=paths.collections_dir, files=files,
                          embedder=embedder, poll_interval_s=limits.poll_interval_s,
                          max_attempts=limits.max_attempts)
    routes = IngestionContext(store=store, files=files, collections_dir=paths.collections_dir,
                              max_upload_bytes=limits.max_upload_bytes,
                              max_pages=limits.max_pages, fetch_arxiv=fetch_arxiv_pdf)
    return _Collections(StoreCollectionResolver(store, indexes), worker, routes, note)


def rewrite_generator(settings: RelaySettings, router: ProviderRouter) -> AnswerGenerator:
    """The rewriter's calls go through the router (V2-09): its lanes, guards and prices,
    with the rewriter's own 64-token budget and no escalation. ``[rewriter].model`` pins
    them to the lane serving that model."""
    routed = RoutedGenerator(router, purpose="rewrite", escalate=False)
    model = settings.rewriter.model
    if model is None:
        return routed
    for lane in router.lanes:
        if lane.model == model:
            return routed.pinned(lane, "rewrite")
    raise ConfigurationError(f"[rewriter] model {model!r} is not the model of any lane")


def make_rewriter(settings: RelaySettings, lane: LaneSettings,
                  generator: AnswerGenerator) -> tuple[QueryRewriter, str]:
    """The follow-up rewriter (V2-08). ``auto`` uses the model only on a Groq lane: the
    stand-in returns scripted text and cannot rewrite, so it gets the rule rewriter."""
    r = settings.rewriter
    use_llm = r.mode == "llm" or (r.mode == "auto" and lane.kind == "groq")
    if not use_llm:
        return RuleQueryRewriter(), "follow-up rewriting: rules (no model call)"
    model = r.model or lane.model
    rewriter = LLMQueryRewriter(generator, model=model, timeout_s=r.timeout_s,
                                max_completion_tokens=r.max_completion_tokens,
                                reasoning_effort=lane.reasoning_effort if r.model is None
                                else None)
    return rewriter, (f"follow-up rewriting: {model}, at most {r.max_completion_tokens} "
                      f"tokens, {r.timeout_s:g} s timeout (then the original question)")


async def _open_l4(settings: RelaySettings) -> tuple[EntailmentScore | None, str]:
    """Load the L4 entailment model once (in a worker thread), or report why L4 is off."""
    v = settings.verifier
    if not (v.l4_model_dir / "config.json").is_file() \
            or not (v.l4_model_dir / v.l4_model_file).is_file():
        return None, (f"L4 entailment: off (no model in {v.l4_model_dir}; run "
                      "scripts/download_l4_model.py)")
    try:
        scorer = await asyncio.to_thread(EntailmentScorer, v.l4_model_dir,
                                         model_file=v.l4_model_file, threads=v.l4_threads)
    except Exception as exc:  # advisory: onnxruntime/tokenizers raise their own error types
        logger.warning("L4 entailment model could not be loaded: %s", sanitize_error(exc))
        return None, f"L4 entailment: off (the model could not be loaded: {type(exc).__name__})"
    return scorer, (f"L4 entailment: on, advisory ({v.l4_model_dir.name}/{v.l4_model_file}, "
                    f"{v.l4_threads} thread(s) per call)")


@asynccontextmanager
async def open_runtime(settings: RelaySettings, env: Settings, *,
                       retrievers: Mapping[str, PassageRetriever] | None = None,
                       vocabulary: frozenset[str] | None = None,
                       collection_embedder: Embedder | None = None,
                       collection_reranker: Reranker | None = None,
                       ) -> AsyncGenerator[Runtime]:
    """Start everything the primary lane needs and yield the ready app.

    In ``sqlite`` mode the store is opened (and migrated) first, so a bad store path fails
    before any model is loaded or server started. Shutdown runs in reverse: the provider
    and stand-in stop, then the request log is flushed, then the store is closed.
    """
    lane = settings.primary_lane
    mode = cache_mode(settings)
    capacity = capacity_derivation(settings)
    async with AsyncExitStack() as stack:
        store: Store | None = None
        sink: RequestLogSink | None = None
        collections: _Collections | None = None
        if settings.cache.mode == "sqlite":
            store = await stack.enter_async_context(open_store(settings.paths.store_path))
            sink = RequestLogSink(store.request_log)
            await sink.start()
            stack.push_async_callback(sink.aclose)
            collections = await _open_collections(settings, store,
                                                  embedder=collection_embedder,
                                                  reranker=collection_reranker)
            await collections.worker.start()
            stack.push_async_callback(collections.worker.aclose)  # stops before the store
        # Blocking work (file hashing, ONNX model and index loading) runs in a worker
        # thread and finishes before any server is started on the event loop.
        corpus = await asyncio.to_thread(corpus_sha256, settings)
        if retrievers is None:
            retrievers, vocabulary = await asyncio.to_thread(_default_retrievers, settings)
        real_cap: int | None = None
        budget: RealCallBudget | None = None  # one hard cap shared by every Groq lane
        lanes: list[ProviderLane] = []
        url, source = "", "stand-in"
        for spec in settings.lanes:
            if spec.kind == "groq" and budget is None:
                if env.groq_api_key is None or not env.groq_api_key.get_secret_value().strip():
                    raise ConfigurationError("a groq lane needs GROQ_API_KEY in the "
                                             "environment or .env")
                if env.max_real_calls < 1:
                    raise ConfigurationError("a groq lane needs MAX_REAL_CALLS >= 1: it is "
                                             "the hard cap on real calls for this process")
                real_cap = env.max_real_calls
                budget = RealCallBudget(real_cap)
            provider, lane_url, lane_source = await _open_provider(spec, env, stack, budget)
            if not lanes:
                url, source = lane_url, lane_source
            lanes.append(ProviderLane(
                name=spec.name, provider=MeteredProvider(provider), model=spec.model,
                guard=ProviderGuard(guard_config(settings, spec)),
                pricing=lane_pricing(spec), max_completion_tokens=spec.max_completion_tokens,
                is_reasoning_model=spec.is_reasoning_model,
                reasoning_effort=spec.reasoning_effort, escalate_to=spec.escalate_to))
        router = ProviderRouter(lanes, max_wait_ms=settings.providers.max_wait_ms)
        queue = admission_queue(settings, capacity)
        app = create_app(
            router=router, retrievers=retrievers,
            top_k={name: RETRIEVAL_SETTINGS[name].top_k for name in retrievers},
            pipeline_config=PipelineConfig(model_id=lane.model,
                                           max_completion_tokens=lane.max_completion_tokens,
                                           reasoning_effort=lane.reasoning_effort),
            queue=queue, corpus_sha256=corpus,
            cache_mode=mode, vocabulary=vocabulary, run=sink,  # type: ignore[arg-type]
            collections=None if collections is None else collections.resolver,
        )
        app.state.relay.entailment, l4_note = await _open_l4(settings)
        app.state.relay.rewriter, rewrite_note = make_rewriter(
            settings, lane, rewrite_generator(settings, router))
        ask_v1: AskV1Context | None = None
        auth_note = "auth: /v1 routes are off (no metadata store)"
        if collections is not None and store is not None:
            dev = dev_key_enabled(settings)
            auth = Authenticator(store, dev_key=DEV_KEY if dev else None)
            service: RelayService = app.state.relay
            resolver = collections.resolver

            ask_v1 = AskV1Context(service=service, store=store, auth=auth)
            context = ask_v1

            def forget(collection_id: str) -> None:  # a deleted collection
                context.forget(collection_id)  # in-flight answers: no cache, no log
                if service.cache is not None:
                    service.cache.purge_collection(collection_id)
                resolver.forget(collection_id)

            app.include_router(ingestion_router(replace(collections.routes, auth=auth,
                                                        on_deleted=forget)))
            app.include_router(requests_router(store, auth))  # V2-11: GET /v1/requests/{id}
            auth_note = ("auth: API keys (relay keys create); development key "
                         + ("accepted (loopback only)" if dev and settings.auth.dev_key_enabled
                            is None else "accepted" if dev else "NOT accepted"))
            app.include_router(ask_v1_router(ask_v1))
            stack.push_async_callback(ask_v1.aclose)  # runs first: before provider and store
        mount_ui(app)  # V2-06: the built web UI at / (after the API routes)
        yield Runtime(app=app, lane=lane, source=source, provider_url=url,
                      capacity=capacity, real_call_cap=real_cap, store=store,
                      request_log=sink,
                      collections=None if collections is None else collections.resolver,
                      ingest_worker=None if collections is None else collections.worker,
                      notes=(l4_note, rewrite_note) if collections is None
                      else (collections.note, l4_note, rewrite_note, auth_note),
                      ask_v1=ask_v1, fallback_lanes=tuple(settings.lanes[1:]))


async def _open_provider(lane: LaneSettings, env: Settings, stack: AsyncExitStack,
                         budget: RealCallBudget | None) -> tuple[ChatProvider, str, Source]:
    """One lane's provider (and its embedded stand-in, if any), closed with ``stack``."""
    provider: ChatProvider
    if lane.kind == "standin":
        url = lane.base_url or ""
        if lane.embedded:
            url = await stack.enter_async_context(
                serve_on_loopback(create_standin_app(standin_profile(lane))))
        source: Source = "stand-in"
        provider = HttpOpenAIProvider(base_url=url, source=source)
    else:
        url = lane.base_url or env.groq_base_url
        source = "real"
        provider = HttpOpenAIProvider(base_url=url, source=source, api_key=env.groq_api_key,
                                      call_budget=budget)
    stack.push_async_callback(provider.aclose)
    return provider, url, source


__all__ = ["Runtime", "admission_queue", "cache_mode", "capacity_derivation", "corpus_sha256",
           "dev_key_enabled", "guard_config", "lane_pricing", "make_rewriter", "open_runtime",
           "rewrite_generator", "standin_profile"]
