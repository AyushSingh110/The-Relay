"""Collection resolution for the pipeline and the service (V2-03).

``StoreCollectionResolver.resolve(collection_id)`` returns what the pipeline needs to answer
from one collection: its retriever (loaded through the ``IndexRegistry``, reloaded when the
collection's version changes), its retrieval setting, its BM25 vocabulary and its scope
cues (``DocumentRegistry.get_cues``, cached per collection version).

Errors: ``CollectionNotFoundError`` (no such collection), ``CollectionNotReadyError`` (no
document has been indexed yet).

``LazyEmbedder`` / ``LazyReranker`` load the embedding and rerank models on first use, so a
service that never touches a collection never loads them. Loading is guarded by a lock,
because retrieval runs in worker threads.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

import numpy as np

from relay.ingestion.registry import CollectionNotFoundError, DocumentRegistry
from relay.orchestrator.pipeline import CollectionResources
from relay.retrieval.embedder import EMBEDDING_MODEL, RERANK_MODEL, Embedder, Reranker
from relay.retrieval.registry import CollectionNotReadyError, IndexRegistry
from relay.retrieval.scope import ScopeCues
from relay.store import Store

T = TypeVar("T")


class _Lazy(Generic[T]):
    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._value: T | None = None
        self._lock = threading.Lock()

    def get(self) -> T:
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = self._factory()
        return self._value


class LazyEmbedder:
    def __init__(self, factory: Callable[[], Embedder], *,
                 model_id: str = EMBEDDING_MODEL) -> None:
        self._model = _Lazy(factory)
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id  # known without loading: checked against each index

    def warm(self) -> None:
        self._model.get()

    @property
    def dim(self) -> int:
        return self._model.get().dim

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._model.get().embed_passages(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._model.get().embed_query(text)


class LazyReranker:
    def __init__(self, factory: Callable[[], Reranker], *, model_id: str = RERANK_MODEL) -> None:
        self._model = _Lazy(factory)
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def warm(self) -> None:
        self._model.get()

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return self._model.get().score(query, passages)


class StoreCollectionResolver:
    def __init__(self, store: Store, indexes: IndexRegistry) -> None:
        self._store = store
        self._indexes = indexes
        self._documents = DocumentRegistry(store.documents, store.collections)
        self._cues: dict[str, tuple[int, ScopeCues]] = {}

    @property
    def documents(self) -> DocumentRegistry:
        return self._documents

    async def version(self, collection_id: str) -> int:
        collection = await self._store.collections.get_by_id(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"collection {collection_id!r} does not exist")
        return collection.version

    async def describe(self, collection_id: str) -> tuple[int, str]:
        """(version, retrieval setting name); version 0 means nothing is indexed yet."""
        version = await self.version(collection_id)
        if version == 0:
            raise CollectionNotReadyError(f"collection {collection_id!r} has no indexed "
                                          "documents yet")
        await asyncio.to_thread(self._indexes.index_dir, collection_id)  # files present?
        return version, (await self._documents.retrieval_setting(collection_id)).name

    async def resolve(self, collection_id: str) -> CollectionResources:
        version = await self.version(collection_id)
        setting = await self._documents.retrieval_setting(collection_id)
        loaded = await self._indexes.aload(collection_id, version, setting)
        cached = self._cues.get(collection_id)
        if cached is None or cached[0] != version:
            cached = (version, await self._documents.get_cues(collection_id))
            self._cues[collection_id] = cached
        return CollectionResources(collection_id=collection_id, version=version,
                                   setting_name=setting.name, retriever=loaded.retriever,
                                   top_k=setting.top_k, vocabulary=loaded.vocabulary,
                                   cues=cached[1], paper_ids=loaded.paper_ids)


__all__ = ["LazyEmbedder", "LazyReranker", "StoreCollectionResolver"]
