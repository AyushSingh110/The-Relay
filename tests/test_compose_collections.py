"""V2-03 tests: collections wired into ``relay serve`` (tests 9 and 10 of the phase prompt).

``open_runtime`` in sqlite mode with a :memory: store, the embedded stand-in on 127.0.0.1
(no API key, no Groq), and temporary upload/collection directories. Test 10 reads the real
v1 ``small_fast`` index and loads the local embedding model (offline); it is skipped when
those gitignored files are absent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay.app import compose
from relay.app.bootstrap import DEV_TENANT_ID, V1_COLLECTION_ID
from relay.app.collections import LazyEmbedder
from relay.app.settings import LaneSettings, RelaySettings
from relay.config import Settings
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.retrieval.hybrid_index import INDEX_MANIFEST
from tests.test_index_registry import FakeEmbedder
from tests.test_ingest_worker import make_pdf
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever
from tests.test_standin import MODEL, profile

AUTH = {"Authorization": "Bearer relay-dev-key"}
PAPERS_DIR, INDEX_DIR, MODELS_DIR = Path("data/papers"), Path("data/indexes"), Path("data/models")
HAVE_V1_INDEX = (INDEX_DIR / "small_fast" / INDEX_MANIFEST).is_file() and MODELS_DIR.is_dir()


def settings_for(tmp_path: Path, *, index_dir: Path = INDEX_DIR) -> RelaySettings:
    path = tmp_path / "profile.json"
    path.write_text(profile().model_dump_json(), encoding="utf-8")
    return RelaySettings(
        lanes=(LaneSettings(name="s", kind="standin", model=MODEL, embedded=True,
                            profile=path),),
        retrieval={"settings": ("small_fast",)},  # type: ignore[arg-type]
        cache={"mode": "sqlite"},  # type: ignore[arg-type]
        ingestion={"poll_interval_s": 0.05},  # type: ignore[arg-type]
        paths={"store_path": ":memory:", "index_dir": index_dir,  # type: ignore[arg-type]
               "uploads_dir": tmp_path / "uploads",
               "collections_dir": tmp_path / "collections"})


def env() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def final_event(text: str) -> dict[str, Any]:
    return json.loads(text.split("event: final\ndata: ")[1])


async def wait_ready(client: httpx.AsyncClient, document_id: str) -> dict[str, Any]:
    for _ in range(400):
        body = (await client.get(f"/v1/documents/{document_id}", headers=AUTH)).json()
        if body["status"] in ("ready", "failed"):
            return body
        await asyncio.sleep(0.025)
    raise AssertionError("the document was never ingested")


def test_lazy_embedder_loads_its_model_on_first_use_only() -> None:
    made: list[int] = []

    def factory() -> FakeEmbedder:
        made.append(1)
        return FakeEmbedder()

    lazy = LazyEmbedder(factory, model_id="fake/hash-embedder")
    assert lazy.model_id == "fake/hash-embedder" and made == []
    assert lazy.embed_query("x").shape == (16,) and lazy.dim == 16
    lazy.embed_passages(["a", "b"])
    assert made == [1]


async def test_sqlite_mode_serves_v1_ask_and_ingests_a_new_pdf_without_restart(
        tmp_path: Path) -> None:
    index_dir = tmp_path / "no-v1-index"  # v1-papers is skipped, the rest still works
    async with compose.open_runtime(
            settings_for(tmp_path, index_dir=index_dir), env(),
            retrievers={"small_fast": StubRetriever(PASSAGES)},
            collection_embedder=FakeEmbedder()) as runtime:
        assert any("v1-papers not registered" in line for line in runtime.describe())
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # test 9: the v1 path is unchanged
            v1 = await client.post("/ask", json={"request_id": "r1", "question":
                                                 "What is d_model in the encoder?",
                                                 "retrieval_setting": "small_fast"})
            assert v1.status_code == 200 and final_event(v1.text)["status"] == "ok"
            # a new PDF, uploaded to a new collection, becomes queryable while running
            created = await client.post("/v1/collections", headers=AUTH, json={
                "name": "geology", "tenant_id": DEV_TENANT_ID})
            cid = created.json()["id"]
            data = make_pdf(tmp_path / "rocks.pdf", ["Basalt is a volcanic rock from lava"])
            upload = await client.post(f"/v1/collections/{cid}/documents", headers=AUTH,
                                       files={"file": ("rocks.pdf", data, "application/pdf")})
            assert upload.status_code == 202
            document = await wait_ready(client, upload.json()["id"])
            assert document["status"] == "ready" and document["job"]["state"] == "ready"
            answer = await client.post("/ask", json={"request_id": "r2", "collection_id": cid,
                                                     "question": "What is basalt?"})
            assert answer.status_code == 200
            assert final_event(answer.text)["retrieval_setting"] == "small_fast"
            assert runtime.collections is not None
            resources = await runtime.collections.resolve(cid)
            passages = resources.retriever.retrieve("basalt").passages
            assert {p.paper_id for p in passages} == {document["id"]}
    assert runtime.ingest_worker is not None and runtime.ingest_worker.processed == 1


@pytest.mark.skipif(not HAVE_V1_INDEX, reason="the v1 index or embedding model is absent")
async def test_the_v1_papers_are_queryable_through_the_collection_path(tmp_path: Path) -> None:
    manifest = load_corpus_manifest(PAPERS_DIR)
    async with compose.open_runtime(settings_for(tmp_path), env(),
                                    retrievers={"small_fast": StubRetriever(PASSAGES)},
                                    ) as runtime:
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = (await client.get("/v1/collections", headers=AUTH)).json()["collections"]
            assert [(c["id"], c["version"]) for c in listed] == [(V1_COLLECTION_ID, 1)]
            answer = await client.post("/ask", json={
                "request_id": "r1", "collection_id": V1_COLLECTION_ID,
                "question": "What is d_model in the Transformer encoder?"})
            assert answer.status_code == 200
            assert final_event(answer.text)["retrieval_setting"] == "small_fast"
        assert runtime.collections is not None
        resources = await runtime.collections.resolve(V1_COLLECTION_ID)
        # the same chunk set as v1, read from the v1 index files (nothing re-embedded)
        assert resources.retriever.index.digest()["chunks_sha256"] == (
            manifest.extraction or {})["chunk_sets"]["small_fast"]["sha256"]
        passages = resources.retriever.retrieve("d_model of the encoder").passages
        assert {p.paper_id for p in passages} <= {p.paper_id for p in manifest.papers}
        assert set(resources.cues.paper_ids) == {p.paper_id for p in manifest.papers}
        assert resources.cues.noun == "papers"  # exactly the v1 papers: the v1 cue set
    assert not (tmp_path / "collections" / V1_COLLECTION_ID / INDEX_MANIFEST).exists()
