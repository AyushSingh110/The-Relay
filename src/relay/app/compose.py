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
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import FastAPI

from relay.api.app import RelayService, create_app
from relay.api.routes.ask_v1 import AskV1Context, ask_v1_router
from relay.app.bootstrap import V1_PRESET, ensure_dev_tenant, register_v1_papers
from relay.app.collections import LazyEmbedder, LazyReranker, StoreCollectionResolver
from relay.app.request_log import RequestLogSink
from relay.app.routes.ingestion import IngestionContext, fetch_arxiv_pdf, ingestion_router
from relay.app.routes.ui import mount_ui
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
from relay.errors import ConfigurationError, sanitize_error
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.ingestion.worker import FileLocator, IngestWorker
from relay.orchestrator.pipeline import PipelineConfig
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
    lane: LaneSettings
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
        if self.store is not None:
            lines.append(f"store: {self.store.path} (schema v{self.store.schema_version}, "
                         f"journal {self.store.journal_mode}); request log on, answers cached in "
                         "memory")
        lines.extend(self.notes)
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
        profile = standin_profile(lane) if lane.kind == "standin" and lane.embedded else None
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
            cache_mode=mode, vocabulary=vocabulary, run=sink,  # type: ignore[arg-type]
            collections=None if collections is None else collections.resolver,
        )
        app.state.relay.entailment, l4_note = await _open_l4(settings)
        ask_v1: AskV1Context | None = None
        if collections is not None and store is not None:
            app.include_router(ingestion_router(collections.routes))
            ask_v1 = AskV1Context(service=app.state.relay, store=store)
            app.include_router(ask_v1_router(ask_v1))
            stack.push_async_callback(ask_v1.aclose)  # runs first: before provider and store
        mount_ui(app)  # V2-06: the built web UI at / (after the API routes)
        yield Runtime(app=app, lane=lane, source=source, provider_url=url,
                      capacity=capacity, real_call_cap=real_cap, store=store,
                      request_log=sink,
                      collections=None if collections is None else collections.resolver,
                      ingest_worker=None if collections is None else collections.worker,
                      notes=(l4_note,) if collections is None
                      else (collections.note, l4_note),
                      ask_v1=ask_v1)


__all__ = ["Runtime", "cache_mode", "capacity_derivation", "corpus_sha256", "guard_config",
           "open_runtime", "standin_profile"]
