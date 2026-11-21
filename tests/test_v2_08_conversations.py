"""V2-08: conversation storage: whole turns appended atomically, the recent turns read back in
order and bounded (so a rewrite prompt never grows with the conversation). :memory: store."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from relay.app.bootstrap import DEV_TENANT_ID, ensure_dev_tenant
from relay.store import Store, open_store
from relay.store.repositories import ForeignKeyViolationError, Turn


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    async with open_store(":memory:") as s:
        await ensure_dev_tenant(s)
        yield s


async def conversation(store: Store) -> str:
    collection = await store.collections.create(DEV_TENANT_ID, "papers")
    return (await store.conversations.create(collection.id)).id


async def test_turns_come_back_in_order_and_only_the_most_recent(store: Store) -> None:
    cid = await conversation(store)
    for n in range(1, 6):
        await store.conversations.append_turn(cid, f"question {n}", f"answer {n}")
    recent = await store.conversations.get_recent_turns(cid, limit=2)
    assert recent == [Turn("question 4", "answer 4"), Turn("question 5", "answer 5")]
    assert len(await store.conversations.get_recent_turns(cid, limit=10)) == 5
    assert await store.conversations.get_recent_turns(cid, limit=0) == []


async def test_a_new_conversation_has_no_turns(store: Store) -> None:
    assert await store.conversations.get_recent_turns(await conversation(store)) == []


async def test_a_turn_is_written_whole_or_not_at_all(store: Store) -> None:
    with pytest.raises(ForeignKeyViolationError):
        await store.conversations.append_turn("no-such-conversation", "q", "a")
    conn = store.conn
    count = (await (await conn.execute("SELECT COUNT(*) FROM message")).fetchone())[0]
    assert count == 0  # neither message was left behind


async def test_lone_messages_are_not_mistaken_for_turns(store: Store) -> None:
    cid = await conversation(store)
    await store.conversations.append_turn(cid, "question 1", "answer 1")
    await store.conversations.append_message(cid, "user", "a question never answered")
    assert await store.conversations.get_recent_turns(cid) == [Turn("question 1", "answer 1")]
