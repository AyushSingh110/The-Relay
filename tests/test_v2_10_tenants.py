"""V2-10: hashed API keys, tenant isolation on every /v1 route, collection deletion, and fair
admission (a noisy tenant cannot starve a light one). Scripted provider, :memory: store,
no network, no Groq."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest

from relay.api.auth import DEV_KEY, Authenticator
from relay.app.bootstrap import DEV_TENANT_ID, V1_COLLECTION_ID, ensure_dev_tenant
from relay.app.routes.ingestion import IngestionContext, ingestion_router
from relay.control.admission import AdmissionQueue
from relay.providers.base import ScriptedReply
from relay.store import open_store
from relay.store.repositories import KEY_PREFIX, hash_api_key
from tests.test_ask_v1 import Harness, answer, ask
from tests.test_ingest_worker import Env

TEXT = "Moshi is a speech dialogue model. Moshi has a latency of 200 ms on one GPU."
ABOUT = "Moshi is a speech dialogue model [P1]."


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


class World:
    """POST /v1/ask and the collection routes over one store, with real key checks."""

    def __init__(self, env: Env, replies: list[ScriptedReply], *, delay_s: float = 0.0,
                 queue: AdmissionQueue | None = None, dev_key: str | None = DEV_KEY) -> None:
        self.env = env
        self.h = Harness(env, replies, delay_s=delay_s)
        self.service = self.h.app.state.relay
        if queue is not None:
            self.service.queue = queue
        self.auth = Authenticator(env.store, dev_key=dev_key)
        self.h.context.auth = self.auth
        self.h.app.include_router(ingestion_router(IngestionContext(
            store=env.store, files=env.files, collections_dir=env.index_root,
            max_upload_bytes=5_000_000, max_pages=50, auth=self.auth,
            on_deleted=self.forget)))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app),
                                        base_url="http://test")

    def forget(self, collection_id: str) -> None:  # as compose wires it
        self.h.context.forget(collection_id)
        if self.service.cache is not None:
            self.service.cache.purge_collection(collection_id)
        self.service.collections.forget(collection_id)

    async def tenant(self, tenant_name: str, role: str = "admin",
                     **kw: Any) -> tuple[str, str]:
        """(tenant id, raw key)."""
        tenant = await self.env.store.tenants.create(tenant_name)
        raw, _ = await self.env.store.api_keys.create(tenant.id, role, **kw)
        return tenant.id, raw

    async def collection(self, tenant_id: str, name: str = "speech") -> str:
        cid = (await self.env.store.collections.create(tenant_id, name)).id
        await self.env.upload(cid, [TEXT], title="Moshi")
        await self.env.worker().run_once()
        return cid

    async def ask(self, key: str, cid: str | None, question: str, **extra: Any) -> Any:
        body: dict[str, Any] = {"question": question, **extra}
        if cid is not None:
            body["collection_id"] = cid
        return await ask(self.h.app, body, auth=f"Bearer {key}")

    def get(self, key: str, path: str) -> Any:
        return self.client.get(path, headers={"Authorization": f"Bearer {key}"})


# 1. keys ---------------------------------------------------------------------------------
async def test_a_key_is_shown_once_and_only_its_hash_is_stored(env: Env) -> None:
    w = World(env, [])
    tenant, raw = await w.tenant("acme", name="ci")  # the key's name
    assert raw.startswith(KEY_PREFIX) and len(raw) == len(KEY_PREFIX) + 32
    int(raw[len(KEY_PREFIX):], 16)  # 32 hex characters
    async with env.store.conn.execute("SELECT * FROM api_key") as cursor:
        rows = [tuple(r) for r in await cursor.fetchall()]
    stored = " ".join(str(v) for row in rows for v in row)
    assert raw not in stored and hash_api_key(raw) in stored  # the hash, never the key
    who = (await w.get(raw, "/v1/whoami")).json()
    assert (who["tenant_id"], who["role"], who["dev_key"]) == (tenant, "admin", False)
    assert raw not in str(who)


async def test_revoked_unknown_and_missing_keys_are_401(env: Env) -> None:
    w = World(env, [])
    tenant, raw = await w.tenant("acme")
    (record,) = await env.store.api_keys.list_for_tenant(tenant)
    assert (await w.get(raw, "/v1/collections")).status_code == 200
    assert await env.store.api_keys.revoke(record.id) is True
    for key in (raw, "rel_live_" + "0" * 32, "nonsense"):
        response = await w.get(key, "/v1/collections")
        assert response.status_code == 401 and response.json()["error"] == "unauthorized"
    assert (await w.client.get("/v1/collections")).status_code == 401
    assert (await w.ask(raw, None, "What is Moshi?")).status == 401


async def test_the_dev_key_works_only_when_enabled(env: Env) -> None:
    on, off = World(env, []), World(env, [], dev_key=None)
    assert (await on.get(DEV_KEY, "/v1/whoami")).json()["tenant_id"] == DEV_TENANT_ID
    assert (await off.get(DEV_KEY, "/v1/whoami")).status_code == 401


async def test_a_keys_daily_limit_is_enforced_on_ask(env: Env) -> None:
    w = World(env, [answer(ABOUT), answer(ABOUT)])
    tenant, raw = await w.tenant("acme", daily_limit=1)
    cid = await w.collection(tenant)
    first = await w.ask(raw, cid, "What is Moshi?")
    assert first.status == 200
    second = await w.ask(raw, cid, "How fast is Moshi?")
    assert second.status == 429 and "daily limit" in second.raw


# 2. isolation ----------------------------------------------------------------------------
async def test_another_tenants_collection_is_404_on_every_route(env: Env) -> None:
    w = World(env, [answer(ABOUT)])
    a, key_a = await w.tenant("a")
    _, key_b = await w.tenant("b")
    cid = await w.collection(a)
    (doc,) = await env.store.documents.list_for_collection(cid)
    conversation = (await env.store.conversations.create(cid)).id
    auth_b = {"Authorization": f"Bearer {key_b}"}
    checks = [
        await w.client.get(f"/v1/collections/{cid}", headers=auth_b),
        await w.client.delete(f"/v1/collections/{cid}", headers=auth_b),
        await w.client.get(f"/v1/documents/{doc.id}", headers=auth_b),
        await w.client.post(f"/v1/collections/{cid}/documents", headers=auth_b,
                            json={"arxiv_id": "1706.03762v7"}),
        await w.client.get(f"/v1/conversations/{conversation}", headers=auth_b),
        await w.client.post("/v1/conversations", headers=auth_b, json={"collection_id": cid}),
    ]
    assert [r.status_code for r in checks] == [404] * len(checks)  # existence never leaks
    asked = await w.ask(key_b, cid, "What is Moshi?")
    assert asked.status == 404
    assert (await w.get(key_b, "/v1/collections")).json()["collections"] == []
    mine = (await w.get(key_a, f"/v1/collections/{cid}")).json()
    assert (mine["id"], mine["document_count"]) == (cid, 1)
    assert len(w.h.provider.requests) == 0  # nothing reached the model


async def test_identical_questions_of_two_tenants_never_share_a_cached_answer(env: Env) -> None:
    w = World(env, [answer(ABOUT), answer(ABOUT)])
    a, key_a = await w.tenant("a")
    b, key_b = await w.tenant("b")
    cid_a, cid_b = await w.collection(a), await w.collection(b)
    first = (await w.ask(key_a, cid_a, "What is Moshi?")).of("final")[0]
    second = (await w.ask(key_b, cid_b, "What is Moshi?")).of("final")[0]
    again = (await w.ask(key_a, cid_a, "What is Moshi?")).of("final")[0]
    assert (first["cache_status"], second["cache_status"], again["cache_status"]) == (
        "miss", "miss", "exact_hit")
    assert len(w.h.provider.requests) == 2  # b paid for its own answer
    assert (first["collection_id"], second["collection_id"]) == (cid_a, cid_b)


async def test_the_request_log_records_the_tenant(env: Env) -> None:
    from relay.app.request_log import log_fields

    record = {"record": "request", "request_id": "r", "collection_id": "c",
              "tenant_id": "acme", "final": {"status": "ok"}}
    assert log_fields(record)["tenant_id"] == "acme"


# 3. deletion -----------------------------------------------------------------------------
async def test_deleting_a_collection_purges_rows_files_and_cache(env: Env) -> None:
    w = World(env, [answer(ABOUT), answer(ABOUT)])
    a, key = await w.tenant("a")
    cid = await w.collection(a)
    (doc,) = await env.store.documents.list_for_collection(cid)
    keep = (await env.store.collections.create(a, "other")).id  # the same PDF, added again
    await env.store.documents.create(keep, source="upload", sha256=doc.sha256, status="ready")
    conversation = (await env.store.conversations.create(cid)).id
    await w.ask(key, cid, "What is Moshi?", conversation_id=conversation)
    await env.store.request_log.insert(status="ok", collection_id=cid)
    assert len(w.service.cache) == 1
    folder = env.index_root / cid
    assert folder.is_dir()
    upload = env.files.upload_path(doc.sha256)
    assert upload.is_file()

    deleted = await w.client.delete(f"/v1/collections/{cid}",
                                    headers={"Authorization": f"Bearer {key}"})
    assert deleted.status_code == 200 and deleted.json()["files_removed"] is True
    assert await env.store.collections.get_by_id(cid) is None
    assert await env.store.documents.list_for_collection(cid) == []
    assert await env.store.conversations.get_by_id(conversation) is None
    async with env.store.conn.execute(
            "SELECT (SELECT COUNT(*) FROM chunk c JOIN document d ON c.document_id = d.id "
            "WHERE d.collection_id = ?), (SELECT COUNT(*) FROM message), "
            "(SELECT COUNT(*) FROM request_log WHERE collection_id = ?)", (cid, cid)) as cur:
        assert tuple(await cur.fetchone()) == (0, 0, 0)  # type: ignore[arg-type]
    assert not folder.exists()  # index builds and extraction reports
    assert len(w.service.cache) == 0  # its cached answer
    # the other collection uses the same PDF: the shared file stays ...
    assert upload.is_file() and await env.store.collections.get_by_id(keep) is not None
    assert (await w.ask(key, cid, "What is Moshi?")).status == 404
    gone = await w.client.delete(f"/v1/collections/{keep}",
                                 headers={"Authorization": f"Bearer {key}"})
    assert gone.status_code == 200 and not upload.exists()  # ... until no one uses it


async def test_a_member_cannot_delete_and_v1_papers_is_protected(env: Env) -> None:
    w = World(env, [])
    a, member = await w.tenant("a", role="member")
    cid = await w.collection(a)
    refused = await w.client.delete(f"/v1/collections/{cid}",
                                    headers={"Authorization": f"Bearer {member}"})
    assert refused.status_code == 403
    await env.store.collections.create(DEV_TENANT_ID, "v1-papers",
                                       collection_id=V1_COLLECTION_ID)
    protected = await w.client.delete(f"/v1/collections/{V1_COLLECTION_ID}",
                                      headers={"Authorization": f"Bearer {DEV_KEY}"})
    assert protected.status_code == 409


async def test_a_collection_being_ingested_cannot_be_deleted(env: Env) -> None:
    w = World(env, [])
    a, key = await w.tenant("a")
    cid = (await env.store.collections.create(a, "busy")).id
    _, job = await env.upload(cid, [TEXT])
    await env.store.ingest_jobs.update_state(job, "parsing")
    busy = await w.client.delete(f"/v1/collections/{cid}",
                                 headers={"Authorization": f"Bearer {key}"})
    assert busy.status_code == 409 and busy.json()["error"] == "collection_busy"
    assert await env.store.collections.get_by_id(cid) is not None


async def test_a_pdf_uploaded_while_a_delete_removes_files_is_kept(
        env: Env, monkeypatch: Any) -> None:
    """Review fix: the delete removes its files while still holding the ingestion lock, so
    an upload of the same PDF elsewhere cannot see the file, skip writing it, then lose it."""
    import threading

    import relay.app.routes.ingestion as ingestion

    w = World(env, [])
    a, key = await w.tenant("a")
    cid = await w.collection(a)
    other = (await env.store.collections.create(a, "other")).id
    (doc,) = await env.store.documents.list_for_collection(cid)
    upload = env.files.upload_path(doc.sha256)
    pdf = upload.read_bytes()
    release = threading.Event()
    real = ingestion._remove_collection_files

    def slow_remove(index_dir: Path, uploads: list[Path]) -> None:
        release.wait(5)
        real(index_dir, uploads)

    monkeypatch.setattr(ingestion, "_remove_collection_files", slow_remove)
    headers = {"Authorization": f"Bearer {key}"}
    deleting = asyncio.create_task(w.client.delete(f"/v1/collections/{cid}", headers=headers))
    await asyncio.sleep(0.2)  # the delete is now removing files
    adding = asyncio.create_task(w.client.post(
        f"/v1/collections/{other}/documents", headers=headers,
        files={"file": ("moshi.pdf", pdf, "application/pdf")}))
    await asyncio.sleep(0.2)
    release.set()
    deleted, added = await deleting, await adding
    assert deleted.status_code == 200 and added.status_code == 202
    assert upload.is_file()  # the new document's file is there to ingest


async def test_an_answer_finishing_after_its_collection_was_deleted_leaves_nothing(
        env: Env) -> None:
    """Review fix: a /v1/ask still answering when its collection is deleted neither caches
    its answer nor writes a request-log record for the deleted collection."""
    from tests.test_ask_collections import ListSink

    w = World(env, [answer(ABOUT)], delay_s=0.1)
    sink = ListSink()
    w.service._run = sink
    a, key = await w.tenant("a")
    cid = await w.collection(a)
    asking = asyncio.create_task(w.ask(key, cid, "What is Moshi?"))
    await asyncio.sleep(0.15)  # it is generating now
    gone = await w.client.delete(f"/v1/collections/{cid}",
                                 headers={"Authorization": f"Bearer {key}"})
    assert gone.status_code == 200
    stream = await asking
    assert stream.of("final")  # the client still got an ending
    assert len(w.service.cache) == 0
    assert not [r for r in sink.records
                if r.get("record") == "request" and r.get("collection_id") == cid]


async def test_a_refused_ask_does_not_spend_the_daily_limit(env: Env) -> None:
    w = World(env, [answer(ABOUT)])
    tenant, raw = await w.tenant("acme", daily_limit=1)
    empty = (await env.store.collections.create(tenant, "empty")).id  # nothing indexed: 409
    assert (await w.ask(raw, empty, "What is Moshi?")).status == 409
    cid = await w.collection(tenant)
    assert (await w.ask(raw, cid, "What is Moshi?")).status == 200  # the limit was not spent


# 4. fair admission -----------------------------------------------------------------------
async def noisy_neighbour(env: Env, *, capacity: int) -> tuple[int, dict[str, Any],
                                                               list[dict[str, Any]], float]:
    """Tenant A sends 30 batch questions at once; tenant B then sends 1 interactive one.

    Returns how many requests the queue admitted between B entering it and B itself (the
    fairness invariant: 0, B goes next), B's final, A's finals, and B's wait in s. Measured
    at the queue, not from when B was sent: B's request does work before it is queued (key,
    collection, index checks), and on a loaded machine A's running answers finish then."""
    queue = AdmissionQueue(capacity=capacity, service_rate_per_s=20.0, max_active=1,
                           tenant_rate_per_s=1.0, tenant_burst=2.0)
    w = World(env, [answer(ABOUT) for _ in range(31)], delay_s=0.01, queue=queue)
    a, key_a = await w.tenant("heavy")
    b, key_b = await w.tenant("light")
    cid_a, cid_b = await w.collection(a), await w.collection(b)
    admitted: list[str | None] = []  # tenants, in the order the queue admitted them
    entered: list[int] = []  # len(admitted) when B entered the queue
    full_at_b: list[bool] = []  # whether the queue was full then (B had to evict someone)
    real_admitted, real_admit = queue._admitted, queue.admit

    def track(tenant: str | None) -> None:
        admitted.append(tenant)
        real_admitted(tenant)

    def admit(**kwargs: Any) -> Any:
        if kwargs.get("tenant") == b:
            entered.append(len(admitted))
            full_at_b.append(queue.depth >= queue.capacity)
        return real_admit(**kwargs)

    queue._admitted = track  # type: ignore[method-assign]
    queue.admit = admit  # type: ignore[method-assign]

    heavy = [asyncio.create_task(w.ask(key_a, cid_a, f"What is Moshi, variant {i}?",
                                       priority="batch")) for i in range(30)]
    await asyncio.sleep(0.15)  # A's burst is queued
    started = time.perf_counter()
    light = await w.ask(key_b, cid_b, "What is Moshi?", priority="interactive")
    waited = time.perf_counter() - started
    heavy_streams = await asyncio.gather(*heavy)
    ahead = admitted[entered[0]:].index(b)
    noisy_neighbour.full_at_b = full_at_b[0]  # type: ignore[attr-defined]
    return (ahead, light.of("final")[0], [s.of("final")[0] for s in heavy_streams], waited)


async def test_a_light_tenant_is_not_stuck_behind_a_noisy_ones_burst(env: Env) -> None:
    ahead, light, heavy, waited = await noisy_neighbour(env, capacity=40)
    assert light["status"] == "ok" and light["verdict"] == "VERIFIED"
    assert ahead == 0  # once queued, B is admitted next, ahead of A's whole waiting burst
    assert all(f["status"] == "ok" for f in heavy)  # the queue held A's burst: nothing lost
    print(f"\nnoisy neighbour (queue 40): {ahead} of A's waiting requests admitted before B, "
          f"waited {waited:.2f} s")


async def test_when_the_queue_is_full_a_batch_request_is_shed_for_an_interactive_one(
        env: Env) -> None:
    ahead, light, heavy, waited = await noisy_neighbour(env, capacity=8)
    assert light["status"] == "ok"
    assert ahead == 0
    shed = [f for f in heavy if f["error_code"] == "queue_full"]
    assert shed  # A's excess burst was shed ...
    # ... and when the queue was still full as B arrived, one was shed to make room for B
    # (on a loaded machine A's answers may have drained it first; eviction itself is the
    # deterministic test_a_full_queue_sheds_its_worst_waiter_for_a_better_arrival)
    if noisy_neighbour.full_at_b:  # type: ignore[attr-defined]
        assert any(f.get("shed_reason") == "shed_for_priority" for f in shed)
    print(f"\nnoisy neighbour (queue 8): {ahead} of A's waiting requests admitted before "
          f"B; {len(shed)} of A's 30 shed; B waited {waited:.2f} s")


async def test_two_interactive_tenants_a_light_one_is_not_stuck_behind_a_burst(
        env: Env) -> None:
    """Both tenants interactive: A's requests past its burst are over rate and wait behind
    B's (v1's FIFO order, with no tenants, is in test_v2_10_admission). Measured at the
    queue: once B is queued, only A's in-burst requests (in rate, arrived first) may go
    before it, never A's over-rate ones."""
    queue = AdmissionQueue(capacity=40, service_rate_per_s=20.0, max_active=1,
                           tenant_rate_per_s=1.0, tenant_burst=2.0)
    w = World(env, [answer(ABOUT) for _ in range(13)], delay_s=0.01, queue=queue)
    a, key_a = await w.tenant("heavy")
    b, key_b = await w.tenant("light")
    cid_a, cid_b = await w.collection(a), await w.collection(b)
    admitted: list[str | None] = []
    entered: list[int] = []
    real_admitted, real_admit = queue._admitted, queue.admit

    def track(tenant: str | None) -> None:
        admitted.append(tenant)
        real_admitted(tenant)

    def admit(**kwargs: Any) -> Any:
        if kwargs.get("tenant") == b:
            entered.append(len(admitted))
        return real_admit(**kwargs)

    queue._admitted = track  # type: ignore[method-assign]
    queue.admit = admit  # type: ignore[method-assign]
    heavy = [asyncio.create_task(w.ask(key_a, cid_a, f"What is Moshi, take {i}?"))
             for i in range(12)]
    await asyncio.sleep(0.1)
    await w.ask(key_b, cid_b, "What is Moshi?")
    await asyncio.gather(*heavy)
    ahead = admitted[entered[0]:].index(b)
    in_burst_left = max(0, 2 - sum(1 for t in admitted[:entered[0]] if t == a))
    print(f"\ntwo interactive tenants: {ahead} of A's requests admitted before B "
          f"(A's in-burst requests still waiting: {in_burst_left})")
    assert ahead <= in_burst_left  # at most A's in-burst ones; none of its over-rate ones
