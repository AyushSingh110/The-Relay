"""Index registry: one loaded retriever per collection, LRU-bounded (plan V2-03).

A collection's index lives in ``{index_dir}/{collection_id}/`` (the v1 ``HybridIndex`` file
format: ``index_manifest.json``, ``chunks.jsonl``, dense vectors). Instead of its own files,
that directory may hold ``index_ref.json`` = ``{"index_dir": "<path>"}`` pointing at an
existing index; the ``v1-papers`` collection uses this to reuse the v1 index files without
re-embedding. A relative reference resolves against ``index_dir``.

``load(collection_id, version, setting)`` reads the index from disk the first time, and
again whenever the collection's version has changed (a document was added). At most
``max_loaded`` collections stay in memory; loading one more evicts the least recently used.
``get`` returns a loaded collection or raises ``CollectionNotReadyError``.

``load`` is blocking (file I/O and BM25 construction); async callers use ``aload``, which
runs it in a worker thread. The registry's own bookkeeping is guarded by a lock, because
several threads may load at once.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from relay.errors import RelayError
from relay.retrieval.embedder import Embedder, Reranker
from relay.retrieval.hybrid_index import (
    INDEX_MANIFEST,
    HybridIndex,
    HybridRetriever,
    RetrievalSetting,
)

INDEX_REF_FILE: Final = "index_ref.json"
DEFAULT_MAX_LOADED: Final = 8


class CollectionNotReadyError(RelayError):
    """The collection has no index yet (nothing ingested), or it is not loaded."""


def resolve_index_dir(index_root: Path, collection_id: str) -> Path:
    """The directory holding this collection's index files: the ``index_ref.json`` target
    when there is one (the ingest worker swaps it atomically to a complete new build),
    otherwise index files directly in the collection's directory."""
    own = index_root / collection_id
    ref = own / INDEX_REF_FILE
    if ref.is_file():
        try:
            target = Path(json.loads(ref.read_text(encoding="utf-8"))["index_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            raise CollectionNotReadyError(f"collection {collection_id!r} has an unreadable "
                                          f"{INDEX_REF_FILE}") from None
        target = target if target.is_absolute() else index_root / target
        if (target / INDEX_MANIFEST).is_file():
            return target
        raise CollectionNotReadyError(f"collection {collection_id!r} points at {target}, "
                                      "which holds no index")
    if (own / INDEX_MANIFEST).is_file():
        return own
    raise CollectionNotReadyError(f"collection {collection_id!r} has no index yet")


def _warm(model: object | None) -> None:
    """Load a lazily loaded model now (``relay.app.collections.LazyEmbedder``), so the load
    does not happen inside a timed retrieval call."""
    warm = getattr(model, "warm", None)
    if callable(warm):
        warm()


@dataclass(frozen=True)
class LoadedCollection:
    collection_id: str
    version: int
    setting: RetrievalSetting
    retriever: HybridRetriever
    vocabulary: frozenset[str]
    paper_ids: frozenset[str]  # the documents the index holds (valid paper filters)


class IndexRegistry:
    def __init__(self, index_root: Path, *, embedder: Embedder,
                 reranker: Reranker | None = None, max_loaded: int = DEFAULT_MAX_LOADED,
                 loader: Callable[[Path], HybridIndex] = HybridIndex.load) -> None:
        if max_loaded < 1:
            raise ValueError("max_loaded must be at least 1")
        self._root = index_root
        self._embedder = embedder
        self._reranker = reranker
        self._max = max_loaded
        self._loader = loader
        self._loaded: OrderedDict[str, LoadedCollection] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._loaded)

    def index_dir(self, collection_id: str) -> Path:
        """Where the collection's index is; raises ``CollectionNotReadyError``."""
        return resolve_index_dir(self._root, collection_id)

    def loaded_ids(self) -> list[str]:
        with self._lock:
            return list(self._loaded)

    def get(self, collection_id: str) -> LoadedCollection:
        with self._lock:
            loaded = self._loaded.get(collection_id)
            if loaded is None:
                raise CollectionNotReadyError(f"collection {collection_id!r} is not loaded")
            self._loaded.move_to_end(collection_id)
            return loaded

    def load(self, collection_id: str, version: int,
             setting: RetrievalSetting) -> LoadedCollection:
        with self._lock:
            current = self._loaded.get(collection_id)
            if current is not None and current.version == version \
                    and current.setting == setting:
                self._loaded.move_to_end(collection_id)
                return current
        index = self._loader(resolve_index_dir(self._root, collection_id))
        loaded = LoadedCollection(
            collection_id=collection_id, version=version, setting=setting,
            retriever=HybridRetriever(index, setting, self._embedder, self._reranker),
            vocabulary=index.vocabulary,
            paper_ids=frozenset(chunk.paper_id for chunk in index.chunks))
        if setting.use_dense:
            _warm(self._embedder)
        if setting.rerank:
            _warm(self._reranker)
        with self._lock:
            self._loaded[collection_id] = loaded
            self._loaded.move_to_end(collection_id)
            while len(self._loaded) > self._max:
                self._loaded.popitem(last=False)
        return loaded

    async def aload(self, collection_id: str, version: int,
                    setting: RetrievalSetting) -> LoadedCollection:
        return await asyncio.to_thread(self.load, collection_id, version, setting)

    def evict_lru(self) -> str | None:
        """Evict the least recently used collection; returns its id (None if empty)."""
        with self._lock:
            if not self._loaded:
                return None
            collection_id, _ = self._loaded.popitem(last=False)
            return collection_id

    def evict(self, collection_id: str) -> None:
        with self._lock:
            self._loaded.pop(collection_id, None)


__all__ = ["DEFAULT_MAX_LOADED", "INDEX_REF_FILE", "CollectionNotReadyError", "IndexRegistry",
           "LoadedCollection", "resolve_index_dir"]
