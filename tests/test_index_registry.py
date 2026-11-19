"""V2-03 tests: IndexRegistry (per-collection indexes, version reloads, LRU eviction).

Indexes are tiny and built with a deterministic fake embedder; no model is loaded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from relay.ingestion.chunker import Chunk
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS, HybridIndex
from relay.retrieval.registry import (
    INDEX_REF_FILE,
    CollectionNotReadyError,
    IndexRegistry,
    resolve_index_dir,
)

SETTING = RETRIEVAL_SETTINGS["small_fast"]


class FakeEmbedder:
    """Deterministic bag-of-words hashing into 16 dimensions (no model, no network)."""

    model_id = "fake/hash-embedder"
    dim = 16

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for word in text.lower().split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector + 1.0 / np.sqrt(self.dim)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vector(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vector(text)


def chunk(paper_id: str, n: int, text: str, preset: str = "small_fast") -> Chunk:
    return Chunk(chunk_id=f"{paper_id}_p1_c{n}", preset=preset, paper_id=paper_id, arxiv_id="",
                 version=0, short_name="doc", section_id="s1", section_number="1",
                 section_title="Intro", section_path=("Intro",), section_kind="body",
                 is_reference=False, page_start=1, page_end=1, word_start=0,
                 word_end=len(text.split()), word_count=len(text.split()), text=text)


def write_index(root: Path, collection_id: str, paper_id: str, text: str) -> Path:
    index = HybridIndex.build([chunk(paper_id, 0, text), chunk(paper_id, 1, "unrelated words")],
                              FakeEmbedder())
    index.save(root / collection_id)
    return root / collection_id


def test_index_dirs_resolve_directly_or_through_a_reference(tmp_path: Path) -> None:
    write_index(tmp_path, "c1", "doc-a", "alpha beta")
    assert resolve_index_dir(tmp_path, "c1") == tmp_path / "c1"
    write_index(tmp_path, "shared", "doc-b", "gamma")
    (tmp_path / "c2").mkdir()
    (tmp_path / "c2" / INDEX_REF_FILE).write_text(json.dumps({"index_dir": "shared"}),
                                                  encoding="utf-8")
    assert resolve_index_dir(tmp_path, "c2") == tmp_path / "shared"
    with pytest.raises(CollectionNotReadyError):
        resolve_index_dir(tmp_path, "never-ingested")


def test_load_get_and_reload_only_when_the_version_changes(tmp_path: Path) -> None:
    write_index(tmp_path, "c1", "doc-a", "alpha beta gamma")
    registry = IndexRegistry(tmp_path, embedder=FakeEmbedder())
    first = registry.load("c1", 1, SETTING)
    assert registry.get("c1") is first and registry.load("c1", 1, SETTING) is first
    result = first.retriever.retrieve("alpha")
    assert {p.paper_id for p in result.passages} == {"doc-a"}
    write_index(tmp_path, "c1", "doc-z", "alpha replaced")
    second = registry.load("c1", 2, SETTING)  # version bump: reloaded from disk
    assert second is not first and second.version == 2
    assert {p.paper_id for p in second.retriever.retrieve("alpha").passages} == {"doc-z"}
    with pytest.raises(CollectionNotReadyError):
        registry.get("not-loaded")


def test_the_ninth_collection_evicts_the_least_recently_used(tmp_path: Path) -> None:
    registry = IndexRegistry(tmp_path, embedder=FakeEmbedder(), max_loaded=8)
    for n in range(1, 9):
        write_index(tmp_path, f"c{n}", f"doc-{n}", f"text {n}")
        registry.load(f"c{n}", 1, SETTING)
    registry.get("c1")  # c1 is now the most recently used; c2 the least
    write_index(tmp_path, "c9", "doc-9", "text 9")
    registry.load("c9", 1, SETTING)
    assert len(registry) == 8 and "c2" not in registry.loaded_ids()
    assert "c1" in registry.loaded_ids() and "c9" in registry.loaded_ids()
    with pytest.raises(CollectionNotReadyError):
        registry.get("c2")
    assert registry.evict_lru() == "c3"  # explicit eviction also takes the oldest
