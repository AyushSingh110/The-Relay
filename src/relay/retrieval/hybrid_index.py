"""Hybrid retrieval over one chunk preset: BM25 + exact dense cosine, fused with RRF.

- **BM25** (``rank_bm25.BM25Okapi``) over a deterministic tokenizer that keeps technical
  identifiers whole (``d_model``, ``10^20``, ``28.4``, ``h-net``) *and* indexes their
  parts, so both "d_model" and "d model" match. Source text is never altered.
- **Dense**: every chunk is embedded in windows of at most ``DENSE_WINDOW_WORDS`` words
  (the 512-token model limit; nothing is truncated) and scored by its best window.
  Exact NumPy cosine (vectors are L2-normalised, so cosine = dot product).
- **Fusion**: Reciprocal Rank Fusion, ``sum 1 / (rrf_k + rank)`` over the enabled
  rankers (Cormack et al., 2009; ``rrf_k = 60``).
- **References**: chunks with ``is_reference`` are down-weighted (fused score x
  ``reference_weight``; rerank score - ``reference_rerank_penalty``) unless the caller
  says the question is about references (ARCHITECTURE.md section 6).
- **Optional rerank**: the top ``rerank_candidates`` fused chunks are re-scored by a
  cross-encoder.
- Ties always break by corpus order, so identical inputs give identical rankings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Annotated, Any, Final

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rank_bm25 import BM25Okapi

from relay.errors import ConfigurationError, ContractViolationError
from relay.ingestion.chunker import Chunk, render_jsonl
from relay.retrieval.embedder import Embedder, Reranker

TOKENIZER_VERSION: Final = "bm25-tok-1"
DENSE_WINDOW_WORDS: Final = 220
DENSE_WINDOW_STEP: Final = 180
RRF_K: Final = 60
BM25_K1: Final = 1.5
BM25_B: Final = 0.75

INDEX_MANIFEST: Final = "index_manifest.json"
CHUNKS_FILE: Final = "chunks.jsonl"
VECTORS_FILE: Final = "dense_windows.npy"
OWNERS_FILE: Final = "dense_window_owner.npy"

_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")
_TOKEN = re.compile(r"[a-z0-9]+(?:[._^\-][a-z0-9]+)*")
_PARTS = re.compile(r"[._^\-]")
_STOPWORDS: Final = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how",
        "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were",
        "what", "when", "where", "which", "who", "why", "with", "does", "do", "did", "can",
        "could", "would", "should", "will", "their", "there", "these", "those", "than", "then",
        "into", "about", "over", "under", "between",
    ]
)


# --- Tokenisation and BM25 ------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lower-cased tokens; compound identifiers are kept whole and also split into parts."""
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for token in _TOKEN.findall(text):
        if token in _STOPWORDS:
            continue
        tokens.append(token)
        if _PARTS.search(token):
            tokens.extend(p for p in _PARTS.split(token) if p and p not in _STOPWORDS)
    return tokens


def dense_windows(text: str) -> list[str]:
    words = text.split()
    if len(words) <= DENSE_WINDOW_WORDS:
        return [text]
    windows: list[str] = []
    start = 0
    while True:
        end = min(start + DENSE_WINDOW_WORDS, len(words))
        windows.append(" ".join(words[start:end]))
        if end == len(words):
            return windows
        start += DENSE_WINDOW_STEP


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rank(scores: np.ndarray, allowed: np.ndarray, depth: int) -> list[int]:
    """Indices of the top-``depth`` allowed items by score (desc), ties by index (asc)."""
    candidates = np.flatnonzero(allowed)
    if candidates.size == 0:
        return []
    order = np.lexsort((candidates, -scores[candidates]))
    return [int(candidates[i]) for i in order[:depth]]


# --- Settings and results -------------------------------------------------------------


class RetrievalSetting(BaseModel):
    """One independently configurable retrieval setting (a row group in Table 3)."""

    model_config = _FROZEN

    name: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    chunk_preset: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    top_k: Annotated[int, Field(ge=1, le=20)]
    candidate_depth: Annotated[int, Field(ge=1)] = 50
    rrf_k: Annotated[int, Field(ge=1)] = RRF_K
    use_bm25: bool = True
    use_dense: bool = True
    rerank: bool = False
    rerank_candidates: Annotated[int, Field(ge=1)] = 12
    reference_weight: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    reference_rerank_penalty: Annotated[float, Field(ge=0.0)] = 2.0

    @model_validator(mode="after")
    def _check(self) -> RetrievalSetting:
        if not (self.use_bm25 or self.use_dense):
            raise ValueError("at least one of use_bm25 / use_dense must be enabled")
        if self.rerank and self.rerank_candidates < self.top_k:
            raise ValueError("rerank_candidates must be >= top_k")
        if self.candidate_depth < max(self.top_k, self.rerank_candidates if self.rerank else 0):
            raise ValueError("candidate_depth must cover top_k and rerank_candidates")
        return self


# Finalised in P04 (see the phase report, section 5). Names are the `retrieval_setting`
# values written to results.csv.
RETRIEVAL_SETTINGS: Final[dict[str, RetrievalSetting]] = {
    s.name: s
    for s in (
        RetrievalSetting(name="small_fast", chunk_preset="small_fast", top_k=4),
        RetrievalSetting(name="balanced_rerank", chunk_preset="balanced_rerank", top_k=4,
                         rerank=True, rerank_candidates=12),
        RetrievalSetting(name="large_wide", chunk_preset="large_wide", top_k=5),
    )
}


class RetrievedPassage(BaseModel):
    model_config = _FROZEN

    rank: int
    chunk_id: str
    paper_id: str
    arxiv_id: str
    version: int
    short_name: str
    section_number: str | None
    section_title: str
    section_path: tuple[str, ...]
    section_kind: str
    page_start: int
    page_end: int
    is_reference: bool
    text: str
    bm25_rank: int | None
    dense_rank: int | None
    rrf_score: float
    rerank_score: float | None
    final_score: float
    reference_downweighted: bool

    @property
    def citation(self) -> str:
        pages = (f"p. {self.page_start}" if self.page_start == self.page_end
                 else f"pp. {self.page_start}-{self.page_end}")
        section = f"{self.section_number} {self.section_title}" if self.section_number \
            else self.section_title
        return f"arXiv:{self.paper_id}, {pages}, {section}"


class RetrievalResult(BaseModel):
    model_config = _FROZEN

    query: str
    setting: str
    paper_filter: tuple[str, ...] | None
    include_references: bool
    passages: tuple[RetrievedPassage, ...]


# --- Index ----------------------------------------------------------------------------


class HybridIndex:
    """Chunks + BM25 + dense window vectors for one chunk preset."""

    def __init__(
        self, chunks: Sequence[Chunk], vectors: np.ndarray, owners: np.ndarray, embedder_id: str
    ) -> None:
        if not chunks:
            raise ContractViolationError("cannot index an empty chunk set")
        presets = {c.preset for c in chunks}
        if len(presets) != 1:
            raise ContractViolationError(f"an index holds exactly one preset, got {presets}")
        if vectors.ndim != 2 or len(vectors) != len(owners):
            raise ContractViolationError("dense vectors and window owners do not align")
        if set(owners.tolist()) != set(range(len(chunks))):
            raise ContractViolationError("every chunk needs at least one dense window")
        self.chunks = tuple(chunks)
        self.preset = presets.pop()
        self.vectors = np.asarray(vectors, dtype=np.float32)
        self.owners = np.asarray(owners, dtype=np.int64)
        self.embedder_id = embedder_id
        self._tokens = [tokenize(c.text) for c in self.chunks]
        self._bm25 = BM25Okapi(self._tokens, k1=BM25_K1, b=BM25_B)
        self.vocabulary = frozenset(t for tokens in self._tokens for t in tokens)
        self._paper_ids = np.array([c.paper_id for c in self.chunks])

    @classmethod
    def build(cls, chunks: Sequence[Chunk], embedder: Embedder) -> HybridIndex:
        if not chunks:
            raise ContractViolationError("cannot index an empty chunk set")
        windows: list[str] = []
        owners: list[int] = []
        for index, chunk in enumerate(chunks):
            for window in dense_windows(chunk.text):
                windows.append(window)
                owners.append(index)
        vectors = embedder.embed_passages(windows)
        return cls(chunks, vectors, np.array(owners, dtype=np.int64), embedder.model_id)

    # -- scoring --

    def bm25_scores(self, query: str) -> np.ndarray:
        tokens = tokenize(query)
        if not tokens:
            return np.zeros(len(self.chunks), dtype=np.float64)
        return np.asarray(self._bm25.get_scores(tokens), dtype=np.float64)

    def dense_scores(self, query_vector: np.ndarray) -> np.ndarray:
        window_scores = self.vectors @ np.asarray(query_vector, dtype=np.float32)
        best = np.full(len(self.chunks), -np.inf, dtype=np.float64)
        np.maximum.at(best, self.owners, window_scores.astype(np.float64))
        return best

    def allowed_mask(self, paper_filter: Collection[str] | None) -> np.ndarray:
        if paper_filter is None:
            return np.ones(len(self.chunks), dtype=bool)
        wanted = set(paper_filter)
        unknown = wanted - set(self._paper_ids.tolist())
        if not wanted or unknown:
            raise ContractViolationError(f"invalid paper filter {sorted(wanted)}")
        return np.isin(self._paper_ids, sorted(wanted))

    # -- persistence --

    def digest(self) -> dict[str, str]:
        return {
            "chunks_sha256": _sha256(render_jsonl(self.chunks).encode("utf-8")),
            "vectors_sha256": _sha256(self.vectors.tobytes()),
            "owners_sha256": _sha256(self.owners.tobytes()),
        }

    def save(self, directory: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / CHUNKS_FILE).write_text(render_jsonl(self.chunks), encoding="utf-8",
                                             newline="\n")
        np.save(directory / VECTORS_FILE, self.vectors, allow_pickle=False)
        np.save(directory / OWNERS_FILE, self.owners, allow_pickle=False)
        manifest = {
            "preset": self.preset,
            "chunks": len(self.chunks),
            "dense_windows": int(len(self.vectors)),
            "dim": int(self.vectors.shape[1]),
            "embedder_id": self.embedder_id,
            "tokenizer_version": TOKENIZER_VERSION,
            "dense_window_words": DENSE_WINDOW_WORDS,
            "dense_window_step": DENSE_WINDOW_STEP,
            "bm25": {"k1": BM25_K1, "b": BM25_B},
            **self.digest(),
            **(extra or {}),
        }
        tmp = directory / f".{INDEX_MANIFEST}.tmp"
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                       newline="\n")
        os.replace(tmp, directory / INDEX_MANIFEST)
        return manifest

    @classmethod
    def load(cls, directory: Path, *, expected_chunks_sha256: str | None = None,
             expected_embedder_id: str | None = None) -> HybridIndex:
        manifest_path = directory / INDEX_MANIFEST
        if not manifest_path.is_file():
            raise ConfigurationError(f"no index at {directory}; run scripts/build_indexes.py")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        chunks = [Chunk.model_validate_json(line) for line in
                  (directory / CHUNKS_FILE).read_text(encoding="utf-8").splitlines() if line]
        vectors = np.load(directory / VECTORS_FILE, allow_pickle=False)
        owners = np.load(directory / OWNERS_FILE, allow_pickle=False)
        index = cls(chunks, vectors, owners, manifest["embedder_id"])
        if manifest.get("tokenizer_version") != TOKENIZER_VERSION:
            raise ConfigurationError("index was built with a different BM25 tokenizer")
        for key, value in index.digest().items():
            if manifest.get(key) != value:
                raise ConfigurationError(f"index file integrity check failed ({key})")
        if expected_chunks_sha256 is not None and index.digest()["chunks_sha256"] != \
                expected_chunks_sha256:
            raise ConfigurationError("index is stale: chunk set differs from the corpus manifest")
        if expected_embedder_id is not None and index.embedder_id != expected_embedder_id:
            raise ConfigurationError("index was built with a different embedding model")
        return index


# --- Retriever ------------------------------------------------------------------------


class HybridRetriever:
    def __init__(self, index: HybridIndex, setting: RetrievalSetting, embedder: Embedder | None,
                 reranker: Reranker | None = None) -> None:
        if setting.chunk_preset != index.preset:
            raise ConfigurationError(
                f"setting {setting.name} needs preset {setting.chunk_preset}, index is "
                f"{index.preset}"
            )
        if setting.use_dense and embedder is None:
            raise ConfigurationError(f"setting {setting.name} needs an embedder")
        if setting.use_dense and embedder is not None and embedder.model_id != index.embedder_id:
            raise ConfigurationError("query embedder differs from the index embedder")
        if setting.rerank and reranker is None:
            raise ConfigurationError(f"setting {setting.name} needs a reranker")
        self.index = index
        self.setting = setting
        self._embedder = embedder
        self._reranker = reranker

    def retrieve(self, query: str, *, paper_filter: Collection[str] | None = None,
                 include_references: bool = False) -> RetrievalResult:
        setting, index = self.setting, self.index
        if not query.strip():
            raise ContractViolationError("query must not be empty")
        allowed = index.allowed_mask(paper_filter)
        bm25_order: list[int] = []
        dense_order: list[int] = []
        if setting.use_bm25:
            scores = index.bm25_scores(query)
            bm25_order = _rank(scores, allowed & (scores > 0), setting.candidate_depth)
        if setting.use_dense and self._embedder is not None:
            dense_order = _rank(index.dense_scores(self._embedder.embed_query(query)), allowed,
                                setting.candidate_depth)
        bm25_rank = {chunk: r for r, chunk in enumerate(bm25_order, start=1)}
        dense_rank = {chunk: r for r, chunk in enumerate(dense_order, start=1)}
        fused: dict[int, float] = {}
        for ranks in (bm25_rank, dense_rank):
            for chunk, r in ranks.items():
                fused[chunk] = fused.get(chunk, 0.0) + 1.0 / (setting.rrf_k + r)
        downweight = {i for i in fused if index.chunks[i].is_reference and not include_references}
        adjusted = {i: s * (setting.reference_weight if i in downweight else 1.0)
                    for i, s in fused.items()}
        ordered = sorted(adjusted, key=lambda i: (-adjusted[i], i))

        rerank_scores: dict[int, float] = {}
        if setting.rerank and self._reranker is not None:
            pool = ordered[: setting.rerank_candidates]
            raw = self._reranker.score(query, [index.chunks[i].text for i in pool])
            rerank_scores = dict(zip(pool, raw, strict=True))
            final = {i: s - (setting.reference_rerank_penalty if i in downweight else 0.0)
                     for i, s in rerank_scores.items()}
            ordered = sorted(pool, key=lambda i: (-final[i], i))
        else:
            final = adjusted

        passages = []
        for rank, i in enumerate(ordered[: setting.top_k], start=1):
            c = index.chunks[i]
            passages.append(RetrievedPassage(
                rank=rank, chunk_id=c.chunk_id, paper_id=c.paper_id, arxiv_id=c.arxiv_id,
                version=c.version, short_name=c.short_name, section_number=c.section_number,
                section_title=c.section_title, section_path=c.section_path,
                section_kind=c.section_kind, page_start=c.page_start, page_end=c.page_end,
                is_reference=c.is_reference, text=c.text, bm25_rank=bm25_rank.get(i),
                dense_rank=dense_rank.get(i), rrf_score=round(fused[i], 10),
                rerank_score=None if i not in rerank_scores else round(rerank_scores[i], 6),
                final_score=round(final[i], 10), reference_downweighted=i in downweight,
            ))
        return RetrievalResult(
            query=query, setting=setting.name,
            paper_filter=None if paper_filter is None else tuple(sorted(set(paper_filter))),
            include_references=include_references, passages=tuple(passages),
        )


__all__ = [
    "DENSE_WINDOW_WORDS",
    "RETRIEVAL_SETTINGS",
    "RRF_K",
    "HybridIndex",
    "HybridRetriever",
    "RetrievalResult",
    "RetrievalSetting",
    "RetrievedPassage",
    "dense_windows",
    "tokenize",
]
