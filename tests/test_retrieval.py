"""Offline retrieval tests: tokenisation, BM25, dense windows, RRF fusion, reference
down-weighting, paper filter, rerank, citations, persistence and determinism.

A deterministic hashed bag-of-words embedder and a token-overlap reranker stand in for
the ONNX models; nothing is downloaded. The last test exercises the real indexes and is
skipped when they have not been built.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from relay.errors import ConfigurationError, ContractViolationError
from relay.ingestion.chunker import Chunk
from relay.retrieval.embedder import l2_normalise
from relay.retrieval.hybrid_index import (
    DENSE_WINDOW_STEP,
    DENSE_WINDOW_WORDS,
    RETRIEVAL_SETTINGS,
    HybridIndex,
    HybridRetriever,
    RetrievalSetting,
    dense_windows,
    tokenize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DIM = 128


class FakeEmbedder:
    model_id = "fake-hash-embedder"
    dim = DIM

    def __init__(self) -> None:
        self.passage_calls = 0

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(DIM, dtype=np.float32)
        for token in tokenize(text):
            vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM] += 1.0
        return vector

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        self.passage_calls += 1
        return l2_normalise(np.vstack([self._vector(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return l2_normalise(self._vector(text))


class FakeReranker:
    model_id = "fake-overlap-reranker"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls.append((query, len(passages)))
        wanted = set(tokenize(query))
        return [float(len(wanted & set(tokenize(p)))) for p in passages]


def chunk(chunk_id: str, text: str, *, paper: str = "1706.03762v7", page: int = 1,
          is_reference: bool = False, preset: str = "unit", section: str = "1") -> Chunk:
    return Chunk(
        chunk_id=chunk_id, preset=preset, paper_id=paper, arxiv_id=paper[:-2],
        version=int(paper[-1]), short_name="x", section_id=f"{paper}_s001",
        section_number=None if is_reference else section,
        section_title="References" if is_reference else "Model Architecture",
        section_path=("References",) if is_reference else (f"{section} Model Architecture",),
        section_kind="references" if is_reference else "body", is_reference=is_reference,
        page_start=page, page_end=page + 1 if page == 4 else page, word_start=0,
        word_end=len(text.split()), word_count=len(text.split()), text=text,
    )


CORPUS = [
    chunk("A_p1_c0", "the encoder uses d_model = 512 and six identical layers"),
    chunk("A_p2_c0", "multi-head attention projects queries keys and values h times", page=2),
    chunk("M_p3_c0", "mimi codec uses eight codebooks at 12.5 Hz frame rate",
          paper="2410.00037v1", page=3),
    chunk("H_p4_c0", "dynamic chunking routing module predicts boundary probabilities",
          paper="2507.07955v2", page=4),
    chunk("R_p9_c0", "[12] layer normalization d_model attention encoder codebooks references",
          page=9, is_reference=True),
]
UNIT = RetrievalSetting(name="unit", chunk_preset="unit", top_k=3, candidate_depth=10)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def index(embedder: FakeEmbedder) -> HybridIndex:
    return HybridIndex.build(CORPUS, embedder)


def retriever(index: HybridIndex, embedder: FakeEmbedder, **overrides: Any) -> HybridRetriever:
    setting = UNIT.model_copy(update=overrides)
    RetrievalSetting.model_validate(setting.model_dump())
    reranker = FakeReranker() if setting.rerank else None
    return HybridRetriever(index, setting, embedder, reranker)


# --- Tokenisation and windows --------------------------------------------------------


def test_tokenizer_keeps_identifiers_whole_and_split() -> None:
    tokens = tokenize("The d_model is 512; cost 1.0 · 10^20 FLOPs in H-Net (28.4 BLEU).")
    for expected in ("d_model", "d", "model", "512", "10^20", "10", "20", "h-net", "h", "net",
                     "28.4", "flops", "bleu"):
        assert expected in tokens
    assert "the" not in tokens and "is" not in tokens


def test_dense_windows_cover_long_chunks_without_truncation() -> None:
    short = " ".join(f"w{i}" for i in range(DENSE_WINDOW_WORDS))
    assert dense_windows(short) == [short]
    long_text = " ".join(f"w{i}" for i in range(500))
    windows = dense_windows(long_text)
    assert [len(w.split()) for w in windows] == [220, 220, 140]
    assert windows[1].split()[0] == f"w{DENSE_WINDOW_STEP}"
    assert windows[-1].split()[-1] == "w499"  # nothing is cut off


# --- Index ---------------------------------------------------------------------------


def test_index_rejects_mixed_presets_and_empty_sets(embedder: FakeEmbedder) -> None:
    with pytest.raises(ContractViolationError):
        HybridIndex.build([], embedder)
    with pytest.raises(ContractViolationError, match="one preset"):
        HybridIndex.build([CORPUS[0], CORPUS[1].model_copy(update={"preset": "other"})],
                          embedder)


def test_dense_score_is_best_window(embedder: FakeEmbedder) -> None:
    filler = " ".join(f"filler{i}" for i in range(300))
    long_chunk = chunk("L_p1_c0", filler + " needle haystack token")
    built = HybridIndex.build([long_chunk, CORPUS[0]], embedder)
    assert len(built.vectors) == 3 and built.owners.tolist() == [0, 0, 1]
    scores = built.dense_scores(embedder.embed_query("needle haystack token"))
    assert scores[0] > scores[1]  # the matching tail window counts in full


def test_bm25_ranks_exact_identifier_first(index: HybridIndex) -> None:
    scores = index.bm25_scores("codebooks frame rate")
    assert int(np.argmax(scores)) == 2


# --- Fusion, references, filters, rerank --------------------------------------------


def test_rrf_scores_are_reciprocal_rank_sums(index: HybridIndex, embedder: FakeEmbedder) -> None:
    result = retriever(index, embedder).retrieve("mimi codebooks")
    top = result.passages[0]
    assert top.chunk_id == "M_p3_c0"
    expected = sum(1.0 / (60 + r) for r in (top.bm25_rank, top.dense_rank) if r is not None)
    assert top.rrf_score == pytest.approx(expected)
    assert [p.rank for p in result.passages] == [1, 2, 3]
    finals = [p.final_score for p in result.passages]
    assert finals == sorted(finals, reverse=True)


def test_single_ranker_settings(index: HybridIndex, embedder: FakeEmbedder) -> None:
    bm25_only = retriever(index, embedder, use_dense=False).retrieve("routing module")
    assert all(p.dense_rank is None for p in bm25_only.passages)
    assert bm25_only.passages[0].chunk_id == "H_p4_c0"
    dense_only = HybridRetriever(index, UNIT.model_copy(update={"use_bm25": False}), embedder)
    assert all(p.bm25_rank is None for p in dense_only.retrieve("routing module").passages)


def test_references_are_downweighted_unless_requested(index: HybridIndex,
                                                      embedder: FakeEmbedder) -> None:
    query = "layer normalization d_model attention encoder"
    default = retriever(index, embedder).retrieve(query)
    with_refs = retriever(index, embedder).retrieve(query, include_references=True)
    ref_default = next(p for p in default.passages if p.chunk_id == "R_p9_c0")
    ref_wanted = next(p for p in with_refs.passages if p.chunk_id == "R_p9_c0")
    assert ref_default.reference_downweighted and not ref_wanted.reference_downweighted
    assert ref_default.final_score == pytest.approx(ref_default.rrf_score * 0.5)
    assert ref_wanted.rank < ref_default.rank
    assert with_refs.passages[0].chunk_id == "R_p9_c0"


def test_paper_filter_restricts_results(index: HybridIndex, embedder: FakeEmbedder) -> None:
    result = retriever(index, embedder).retrieve("d_model codebooks routing",
                                                 paper_filter=["2410.00037v1"])
    assert {p.paper_id for p in result.passages} == {"2410.00037v1"}
    assert result.paper_filter == ("2410.00037v1",)
    with pytest.raises(ContractViolationError):
        retriever(index, embedder).retrieve("x", paper_filter=["9999.99999v1"])
    with pytest.raises(ContractViolationError):
        retriever(index, embedder).retrieve("x", paper_filter=[])


def test_rerank_reorders_candidates_and_penalises_references(
    index: HybridIndex, embedder: FakeEmbedder
) -> None:
    setting = UNIT.model_copy(update={"rerank": True, "rerank_candidates": 5, "top_k": 3})
    reranker = FakeReranker()
    result = HybridRetriever(index, setting, embedder, reranker).retrieve(
        "layer normalization d_model attention encoder codebooks")
    assert reranker.calls and reranker.calls[0][1] == 5
    assert all(p.rerank_score is not None for p in result.passages)
    ref = next((p for p in result.passages if p.is_reference), None)
    if ref is not None:
        assert ref.final_score == pytest.approx(ref.rerank_score - 2.0)


def test_citations_carry_source_version_section_and_pages(
    index: HybridIndex, embedder: FakeEmbedder
) -> None:
    result = retriever(index, embedder).retrieve("routing module boundary probabilities")
    top = result.passages[0]
    assert (top.paper_id, top.arxiv_id, top.version) == ("2507.07955v2", "2507.07955", 2)
    assert (top.page_start, top.page_end) == (4, 5)
    assert top.citation == "arXiv:2507.07955v2, pp. 4-5, 1 Model Architecture"
    ref = retriever(index, embedder).retrieve("references layer normalization",
                                              include_references=True).passages[0]
    assert ref.citation == "arXiv:1706.03762v7, p. 9, References"


def test_empty_query_and_setting_mismatches(index: HybridIndex, embedder: FakeEmbedder) -> None:
    with pytest.raises(ContractViolationError):
        retriever(index, embedder).retrieve("   ")
    with pytest.raises(ConfigurationError, match="needs preset"):
        HybridRetriever(index, RETRIEVAL_SETTINGS["small_fast"], embedder)
    with pytest.raises(ConfigurationError, match="reranker"):
        HybridRetriever(index, UNIT.model_copy(update={"rerank": True}), embedder, None)
    with pytest.raises(ConfigurationError, match="embedder"):
        HybridRetriever(index, UNIT, None)


@pytest.mark.parametrize(
    "overrides",
    [{"use_bm25": False, "use_dense": False}, {"rerank": True, "rerank_candidates": 2},
     {"candidate_depth": 2}],
)
def test_invalid_settings_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetrievalSetting.model_validate({**UNIT.model_dump(), **overrides})


def test_three_independent_settings_are_defined() -> None:
    assert set(RETRIEVAL_SETTINGS) == {"small_fast", "balanced_rerank", "large_wide"}
    assert len({(s.chunk_preset, s.top_k, s.rerank) for s in RETRIEVAL_SETTINGS.values()}) == 3
    assert RETRIEVAL_SETTINGS["balanced_rerank"].rerank
    assert not RETRIEVAL_SETTINGS["small_fast"].rerank


# --- Persistence and determinism ----------------------------------------------------


def test_save_load_parity(tmp_path: Path, index: HybridIndex, embedder: FakeEmbedder) -> None:
    index.save(tmp_path / "unit")
    loaded = HybridIndex.load(tmp_path / "unit", expected_embedder_id=embedder.model_id)
    for query in ("mimi codebooks", "d_model encoder", "routing module", "references"):
        before = retriever(index, embedder).retrieve(query)
        after = retriever(loaded, embedder).retrieve(query)
        assert before == after
    assert loaded.digest() == index.digest()


def test_load_detects_tampering_and_staleness(tmp_path: Path, index: HybridIndex) -> None:
    directory = tmp_path / "unit"
    index.save(directory)
    with pytest.raises(ConfigurationError, match="stale"):
        HybridIndex.load(directory, expected_chunks_sha256="0" * 64)
    with pytest.raises(ConfigurationError, match="different embedding model"):
        HybridIndex.load(directory, expected_embedder_id="other-model")
    vectors = np.load(directory / "dense_windows.npy")
    vectors[0, 0] += 1.0
    np.save(directory / "dense_windows.npy", vectors)
    with pytest.raises(ConfigurationError, match="integrity"):
        HybridIndex.load(directory)
    with pytest.raises(ConfigurationError, match="no index"):
        HybridIndex.load(tmp_path / "missing")


def test_build_and_retrieval_are_deterministic(embedder: FakeEmbedder) -> None:
    first, second = HybridIndex.build(CORPUS, embedder), HybridIndex.build(CORPUS, embedder)
    assert first.digest() == second.digest()
    a = retriever(first, embedder).retrieve("attention encoder d_model")
    b = retriever(second, embedder).retrieve("attention encoder d_model")
    assert a.model_dump_json() == b.model_dump_json()


def test_ties_break_by_corpus_order(embedder: FakeEmbedder) -> None:
    twins = [chunk("T_p1_c0", "identical text here"), chunk("T_p1_c1", "identical text here")]
    built = HybridIndex.build(twins, embedder)
    result = retriever(built, embedder, top_k=2).retrieve("identical text")
    assert [p.chunk_id for p in result.passages] == ["T_p1_c0", "T_p1_c1"]


# --- Real indexes (skipped when not built) ------------------------------------------

REAL_MANIFEST = REPO_ROOT / "data" / "retrieval_manifest.json"
REAL_INDEXES = REPO_ROOT / "data" / "indexes"


@pytest.mark.skipif(not (REAL_MANIFEST.is_file() and REAL_INDEXES.is_dir()),
                    reason="real indexes not built (run scripts/build_indexes.py)")
def test_real_indexes_load_and_match_manifest() -> None:
    manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
    corpus = json.loads((REPO_ROOT / "data" / "papers" / "manifest.json").read_text("utf-8"))
    for name, summary in manifest["indexes"].items():
        loaded = HybridIndex.load(
            REAL_INDEXES / name,
            expected_chunks_sha256=corpus["extraction"]["chunk_sets"][name]["sha256"],
            expected_embedder_id=manifest["embedding_model"]["model_id"],
        )
        assert len(loaded.chunks) == summary["chunks"]
        assert loaded.digest()["vectors_sha256"] == summary["vectors_sha256"]
        assert sum(c.is_reference for c in loaded.chunks) == summary["reference_chunks"]
        # BM25-only retrieval needs no model: a fact from Attention p.8 is found there.
        bm25 = HybridRetriever(loaded, RetrievalSetting(name="t", chunk_preset=name, top_k=3,
                                                        use_dense=False), None)
        top = bm25.retrieve("BLEU English-to-French big transformer 41.8").passages[0]
        assert top.paper_id == "1706.03762v7" and top.page_start <= 8 <= top.page_end
