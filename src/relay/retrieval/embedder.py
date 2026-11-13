"""Embedding and reranking models behind small Protocols (fakes are used in tests)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict

from relay.errors import ConfigurationError

EMBEDDING_MODEL: Final = "BAAI/bge-small-en-v1.5"
EMBEDDING_HF_REPO: Final = "Qdrant/bge-small-en-v1.5-onnx-Q"
EMBEDDING_ONNX_FILE: Final = "model_optimized.onnx"
RERANK_MODEL: Final = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_HF_REPO: Final = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_ONNX_FILE: Final = "onnx/model.onnx"
MODEL_MAX_TOKENS: Final = 512
# BGE v1.5 model-card instruction for short query -> passage retrieval.
BGE_QUERY_PREFIX: Final = "Represent this sentence for searching relevant passages: "
DEFAULT_MODEL_DIR: Final = Path("data/models")
DEFAULT_THREADS: Final = 4  # fixed thread count keeps ONNX outputs reproducible
# Small batches: FastEmbed pads each batch to its longest passage (measured ~16x faster).
BATCH_SIZE: Final = 4


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class Reranker(Protocol):
    @property
    def model_id(self) -> str: ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


def l2_normalise(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


class ModelRecord(BaseModel):
    """Provenance of a locally cached model, recorded in the retrieval manifest."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    model_id: str
    hf_repo: str
    revision: str
    onnx_file: str
    onnx_sha256: str
    onnx_bytes: int


def describe_cached_model(
    cache_dir: Path, hf_repo: str, onnx_file: str, model_id: str
) -> ModelRecord:
    repo_dir = cache_dir / f"models--{hf_repo.replace('/', '--')}"
    ref = repo_dir / "refs" / "main"
    if not ref.is_file():
        raise ConfigurationError(
            f"model {model_id} is not cached in {cache_dir}; run "
            "scripts/build_indexes.py --download-models once"
        )
    revision = ref.read_text(encoding="utf-8").strip()
    onnx_path = repo_dir / "snapshots" / revision / onnx_file
    data = onnx_path.read_bytes()
    return ModelRecord(
        model_id=model_id,
        hf_repo=hf_repo,
        revision=revision,
        onnx_file=onnx_file,
        onnx_sha256=hashlib.sha256(data).hexdigest(),
        onnx_bytes=len(data),
    )


class FastEmbedEmbedder:
    """BGE-small (384-d) via FastEmbed. Passages and queries are L2-normalised."""

    def __init__(
        self,
        cache_dir: Path = DEFAULT_MODEL_DIR,
        *,
        local_files_only: bool = True,
        threads: int = DEFAULT_THREADS,
        query_prefix: str = BGE_QUERY_PREFIX,
    ) -> None:
        from fastembed import TextEmbedding  # heavy import kept local

        self._model = TextEmbedding(
            EMBEDDING_MODEL, cache_dir=str(cache_dir), threads=threads,
            local_files_only=local_files_only,
        )
        self._query_prefix = query_prefix
        self._dim = int(self._model.embedding_size)

    @property
    def model_id(self) -> str:
        return EMBEDDING_MODEL

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def query_prefix(self) -> str:
        return self._query_prefix

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        vectors = self._model.passage_embed(list(texts), batch_size=BATCH_SIZE)
        return l2_normalise(np.vstack(list(vectors)))

    def embed_query(self, text: str) -> np.ndarray:
        vector = next(iter(self._model.query_embed(self._query_prefix + text)))
        return l2_normalise(np.asarray(vector))


class FastEmbedReranker:
    """MiniLM-L6 MS-MARCO cross-encoder via FastEmbed (higher score = more relevant)."""

    def __init__(
        self, cache_dir: Path = DEFAULT_MODEL_DIR, *, local_files_only: bool = True,
        threads: int = DEFAULT_THREADS,
    ) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(
            RERANK_MODEL, cache_dir=str(cache_dir), threads=threads,
            local_files_only=local_files_only,
        )

    @property
    def model_id(self) -> str:
        return RERANK_MODEL

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, list(passages), batch_size=BATCH_SIZE)]


__all__ = [
    "BGE_QUERY_PREFIX",
    "EMBEDDING_MODEL",
    "RERANK_MODEL",
    "Embedder",
    "FastEmbedEmbedder",
    "FastEmbedReranker",
    "ModelRecord",
    "Reranker",
    "describe_cached_model",
    "l2_normalise",
]
