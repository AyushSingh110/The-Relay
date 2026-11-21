"""V2-08: follow-up questions on POST /v1/ask: rewritten before the cache, SingleFlight and
retrieval; first questions never pay for a rewrite; the conversation is recorded. Scripted
provider, rule rewriter (or a scripted LLM rewriter), :memory: store; no network."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

import pytest

from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.orchestrator.rewriter import LLMQueryRewriter, Rewrite, RuleQueryRewriter
from relay.providers.base import ScriptedReply
from relay.providers.errors import ProviderConnectionError
from relay.store import open_store
from relay.store.repositories import Turn
from tests.test_ask_v1 import Harness, answer, ask
from tests.test_ingest_worker import Env
from tests.test_v2_08_rewriter import Scripted

ABOUT = "Moshi is a speech dialogue model [P1]."
LATENCY = "Moshi has a latency of 200 ms [P1]."


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env]:
    async with open_store(":memory:") as store:
        await ensure_dev_tenant(store)
        yield Env(store, tmp_path)


async def speech(env: Env) -> str:
    cid = (await env.store.collections.create(DEV_TENANT_ID, "speech")).id
    await env.upload(cid, ["Moshi is a speech dialogue model. Moshi has a latency of 200 ms "
                           "on one GPU."], title="Moshi")
    await env.worker().run_once()
    return cid


class Spy:
    """The rule rewriter, recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.inner = RuleQueryRewriter()

    async def resolve(self, question: str, history: Sequence[Turn]) -> Rewrite:
        self.calls.append((question, len(history)))
        return await self.inner.resolve(question, history)


def harness(env: Env, replies: list[ScriptedReply], rewriter: Any = None) -> Harness:
    h = Harness(env, replies)
    h.app.state.relay.rewriter = rewriter or Spy()
    return h


async def new_conversation(env: Env, cid: str) -> str:
    return (await env.store.conversations.create(cid)).id


def body(cid: str, question: str, conversation: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"question": question, "collection_id": cid}
    if conversation is not None:
        out["conversation_id"] = conversation
    return out


def prompts(h: Harness) -> list[str]:
    return ["\n".join(m.content for m in r.messages) for r in h.provider.requests]


# 1 --------------------------------------------------------------------------------------
async def test_first_questions_never_call_the_rewriter(env: Env) -> None:
    cid = await speech(env)
    spy = Spy()
    h = harness(env, [answer(ABOUT), answer(ABOUT)], spy)
    no_conversation = await ask(h.app, body(cid, "How does Moshi work?"))
    conversation = await new_conversation(env, cid)
    first_turn = await ask(h.app, body(cid, "How does Moshi answer?", conversation))
    assert spy.calls == []  # no rewrite, so no model call and no added latency
    for stream in (no_conversation, first_turn):
        assert all(s.get("stage") != "rewriting" for s in stream.of("status"))
        assert stream.names[0] == "accepted"
    assert first_turn.of("accepted")[0]["conversation_id"] == conversation


# 2 --------------------------------------------------------------------------------------
async def test_a_follow_up_is_rewritten_before_retrieval_and_generation(env: Env) -> None:
    cid = await speech(env)
    h = harness(env, [answer(ABOUT), answer(LATENCY)])
    conversation = await new_conversation(env, cid)
    await ask(h.app, body(cid, "How does Moshi work?", conversation))
    follow = await ask(h.app, body(cid, "And what is its inference latency?", conversation))
    assert follow.names[:3] == ["accepted", "status", "status"]  # accepted first, always
    done = [s for s in follow.of("status") if "rewritten_question" in s][0]
    assert (done["stage"], done["original_question"]) == (
        "rewriting", "And what is its inference latency?")
    assert done["rewritten_question"] == "What is Moshi's inference latency?"
    assert "What is Moshi's inference latency?" in prompts(h)[1]  # the model got it
    final = follow.of("final")[0]
    assert final["rewritten_question"] == "What is Moshi's inference latency?"
    assert final["status"] == "ok"


# 3 --------------------------------------------------------------------------------------
async def test_a_self_contained_follow_up_is_unchanged(env: Env) -> None:
    cid = await speech(env)
    h = harness(env, [answer(ABOUT), answer(LATENCY)])
    conversation = await new_conversation(env, cid)
    await ask(h.app, body(cid, "How does Moshi work?", conversation))
    follow = await ask(h.app, body(cid, "What is the hidden dimension of BERT?", conversation))
    done = [s for s in follow.of("status") if "rewritten_question" in s][0]
    assert done["rewritten_question"] == "What is the hidden dimension of BERT?"
    assert done["rewritten"] is False


# 4 --------------------------------------------------------------------------------------
class Broken:
    async def resolve(self, question: str, history: Sequence[Turn]) -> Rewrite:
        raise RuntimeError("rewriter crashed")


@pytest.mark.parametrize("make", [
    lambda: Broken(),
    lambda: LLMQueryRewriter(Scripted("never", delay_s=2.0), model="m", timeout_s=0.05),
    lambda: LLMQueryRewriter(Scripted(error=ProviderConnectionError("x", reason="t")),
                             model="m"),
], ids=["raises", "timeout", "provider_error"])
async def test_a_failing_rewrite_falls_back_to_the_original_question(env: Env, make: Any) -> None:
    cid = await speech(env)
    h = harness(env, [answer(ABOUT), answer(LATENCY)], make())
    conversation = await new_conversation(env, cid)
    await ask(h.app, body(cid, "How does Moshi work?", conversation))
    follow = await ask(h.app, body(cid, "What is Moshi's latency?", conversation))
    done = [s for s in follow.of("status") if "rewritten_question" in s][0]
    assert (done["rewritten_question"], done["rewritten"]) == ("What is Moshi's latency?", False)
    assert done["fallback"] is not None
    assert follow.of("final")[0]["status"] == "ok"  # the answer still comes


# 5 --------------------------------------------------------------------------------------
async def test_a_rewritten_follow_up_shares_the_cache_entry_of_the_same_question(
        env: Env) -> None:
    cid = await speech(env)
    h = harness(env, [answer(LATENCY), answer(LATENCY)])
    conversation = await new_conversation(env, cid)
    first = await ask(h.app, body(cid, "What is Moshi's latency?", conversation))
    assert first.of("final")[0]["cache_status"] == "miss"
    follow = await ask(h.app, body(cid, "What is its latency?", conversation))
    final = follow.of("final")[0]
    assert final["rewritten_question"] == "What is Moshi's latency?"
    assert final["cache_status"] == "exact_hit"
    assert len(h.provider.requests) == 1  # the follow-up made no model call at all


# 6 --------------------------------------------------------------------------------------
async def test_the_conversation_is_recorded_and_the_rewrite_sees_at_most_two_turns(
        env: Env) -> None:
    cid = await speech(env)
    spy = Spy()
    h = harness(env, [answer(ABOUT) for _ in range(5)], spy)
    conversation = await new_conversation(env, cid)
    questions = ["How does Moshi work?", "What is it?", "How is it built?", "Is it fast?",
                 "Who made it?"]
    for q in questions:
        assert (await ask(h.app, body(cid, q, conversation))).of("final")[0]["status"] == "ok"
    messages = await env.store.conversations.list_messages(conversation)
    assert [m.content for m in messages if m.role == "user"] == questions  # the originals
    assert len(messages) == 10
    assert [n for _, n in spy.calls] == [1, 2, 2, 2]  # bounded to the last 2 turns


async def test_a_failed_answer_is_not_recorded(env: Env) -> None:
    cid = await speech(env)
    broken = ScriptedReply(error=ProviderConnectionError("reset", reason="test"))
    h = harness(env, [broken, broken, broken])
    conversation = await new_conversation(env, cid)
    final = (await ask(h.app, body(cid, "How does Moshi work?", conversation))).of("final")[0]
    assert final["status"] != "ok"
    assert await env.store.conversations.list_messages(conversation) == []


# conversations API -----------------------------------------------------------------------
async def test_conversations_can_be_created_and_read_back(env: Env) -> None:
    import httpx

    cid = await speech(env)
    h = harness(env, [answer(ABOUT)])
    auth = {"Authorization": "Bearer relay-dev-key"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app),
                                 base_url="http://test") as client:
        created = await client.post("/v1/conversations", json={"collection_id": cid},
                                    headers=auth)
        assert created.status_code == 201
        conversation = created.json()["id"]
        await ask(h.app, body(cid, "How does Moshi work?", conversation))
        got = (await client.get(f"/v1/conversations/{conversation}", headers=auth)).json()
        assert got["collection_id"] == cid
        assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
        assert got["messages"][1]["content"].startswith("Moshi is a speech dialogue model")
        assert (await client.get("/v1/conversations/nope", headers=auth)).status_code == 404
        assert (await client.post("/v1/conversations", json={"collection_id": "nope"},
                                  headers=auth)).status_code == 404
        assert (await client.post("/v1/conversations", json={"collection_id": cid})
                ).status_code == 401
