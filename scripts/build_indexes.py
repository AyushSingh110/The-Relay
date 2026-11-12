"""Build the BM25 + dense indexes for every chunk preset and record model provenance.

  python scripts/build_indexes.py --download-models   # ONE-TIME: fetch models into data/models
  python scripts/build_indexes.py                     # offline build (local model files only)
  python scripts/build_indexes.py --check             # rebuild in memory; exit 1 on any drift

Indexes go to data/indexes/<preset>/ (gitignored). data/retrieval_manifest.json (tracked)
records the models (Hugging Face revision + ONNX SHA-256), library versions, the
retrieval settings and per-preset digests. It contains no timestamps.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from relay.errors import RelayError, sanitize_error
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.ingestion.chunker import CHUNK_PRESETS, Chunk
from relay.retrieval.embedder import (
    BGE_QUERY_PREFIX,
    DEFAULT_THREADS,
    EMBEDDING_HF_REPO,
    EMBEDDING_MODEL,
    EMBEDDING_ONNX_FILE,
    MODEL_MAX_TOKENS,
    RERANK_HF_REPO,
    RERANK_MODEL,
    RERANK_ONNX_FILE,
    FastEmbedEmbedder,
    FastEmbedReranker,
    describe_cached_model,
)
from relay.retrieval.hybrid_index import (
    DENSE_WINDOW_STEP,
    DENSE_WINDOW_WORDS,
    RETRIEVAL_SETTINGS,
    TOKENIZER_VERSION,
    HybridIndex,
    dense_windows,
)

LIBRARIES = ("fastembed", "onnxruntime", "numpy", "rank-bm25", "tokenizers")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--index-dir", type=Path, default=Path("data/indexes"))
    parser.add_argument("--models-dir", type=Path, default=Path("data/models"))
    parser.add_argument("--manifest", type=Path, default=Path("data/retrieval_manifest.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download-models", action="store_true",
                      help="allow the one-time model download from Hugging Face")
    mode.add_argument("--check", action="store_true",
                      help="rebuild in memory and compare with the recorded manifest")
    return parser.parse_args(argv)


def _load_chunks(path: Path) -> tuple[list[Chunk], str]:
    data = path.read_bytes()
    chunks = [Chunk.model_validate_json(line) for line in data.decode("utf-8").splitlines() if line]
    return chunks, hashlib.sha256(data).hexdigest()


def _token_counter(models_dir: Path):  # type: ignore[no-untyped-def]
    from tokenizers import Tokenizer

    record = describe_cached_model(models_dir, EMBEDDING_HF_REPO, EMBEDDING_ONNX_FILE,
                                   EMBEDDING_MODEL)
    repo = models_dir / f"models--{EMBEDDING_HF_REPO.replace('/', '--')}"
    tokenizer = Tokenizer.from_file(str(repo / "snapshots" / record.revision / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return lambda text: len(tokenizer.encode(text).ids)


def build(args: argparse.Namespace) -> dict[str, Any]:
    corpus = load_corpus_manifest(args.papers_dir)
    chunk_sets = (corpus.extraction or {}).get("chunk_sets", {})
    local_only = not args.download_models
    embedder = FastEmbedEmbedder(args.models_dir, local_files_only=local_only)
    FastEmbedReranker(args.models_dir, local_files_only=local_only)  # ensure it is cached
    count_tokens = _token_counter(args.models_dir)

    indexes: dict[str, Any] = {}
    for preset in CHUNK_PRESETS:
        chunks, file_sha = _load_chunks(args.processed_dir / "chunks" / f"{preset.name}.jsonl")
        recorded = chunk_sets.get(preset.name, {}).get("sha256")
        if recorded != file_sha:
            raise RelayError(f"chunk set {preset.name} does not match data/papers/manifest.json;"
                             " re-run scripts/ingest_papers.py --offline")
        index = HybridIndex.build(chunks, embedder)
        windows = [w for c in chunks for w in dense_windows(c.text)]
        summary = {
            "chunks": len(chunks),
            "reference_chunks": sum(c.is_reference for c in chunks),
            "chunk_set_sha256": file_sha,
            "dense_windows": len(windows),
            "dense_windows_over_model_limit": sum(count_tokens(w) > MODEL_MAX_TOKENS
                                                  for w in windows),
            "chunks_over_model_limit": sum(count_tokens(c.text) > MODEL_MAX_TOKENS
                                           for c in chunks),
            "bm25_vocabulary": len(index.vocabulary),
            **index.digest(),
        }
        if not args.check:
            index.save(args.index_dir / preset.name, extra={"chunk_set_sha256": file_sha})
        indexes[preset.name] = summary

    return {
        "schema_version": 1,
        "embedding_model": {
            **describe_cached_model(args.models_dir, EMBEDDING_HF_REPO, EMBEDDING_ONNX_FILE,
                                    EMBEDDING_MODEL).model_dump(),
            "dim": embedder.dim,
            "max_tokens": MODEL_MAX_TOKENS,
            "query_prefix": BGE_QUERY_PREFIX,
            "threads": DEFAULT_THREADS,
        },
        "rerank_model": {
            **describe_cached_model(args.models_dir, RERANK_HF_REPO, RERANK_ONNX_FILE,
                                    RERANK_MODEL).model_dump(),
            "max_tokens": MODEL_MAX_TOKENS,
            "threads": DEFAULT_THREADS,
        },
        "libraries": {name: metadata.version(name) for name in LIBRARIES},
        "bm25_tokenizer_version": TOKENIZER_VERSION,
        "dense_window": {"words": DENSE_WINDOW_WORDS, "step": DENSE_WINDOW_STEP},
        "chunk_presets": [p.model_dump() for p in CHUNK_PRESETS],
        "retrieval_settings": {k: v.model_dump() for k, v in RETRIEVAL_SETTINGS.items()},
        "indexes": indexes,
    }


def render(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = build(args)
    except RelayError as exc:
        print(json.dumps({"outcome": "failed", "error": sanitize_error(exc), "message": str(exc)}))
        return 1
    text = render(manifest)
    if args.check:
        current = args.manifest.read_text(encoding="utf-8") if args.manifest.is_file() else ""
        unchanged = current == text
        print(json.dumps({"outcome": "checked", "manifest_unchanged": unchanged}))
        return 0 if unchanged else 1
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({"outcome": "written", "indexes": {
        k: {"chunks": v["chunks"], "dense_windows": v["dense_windows"]}
        for k, v in manifest["indexes"].items()}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
