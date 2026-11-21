"""Exact answer cache and SingleFlight (ARCHITECTURE.md section 8)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from relay.orchestrator.verifier import CitedPassage
from relay.schemas import VerificationVerdict

if TYPE_CHECKING:
    from relay.control.state import StateStore

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES: Final = 1024
CACHEABLE_VERDICTS: Final[frozenset[str]] = frozenset({"VERIFIED", "REFUSAL_VERIFIED"})
_WHITESPACE = re.compile(r"\s+")
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")

T = TypeVar("T")
Role = Literal["leader", "follower"]


def normalise_question(question: str) -> str:
    """NFKC form, whitespace collapsed, ends trimmed. Case is preserved."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", question)).strip()


def question_hash(question: str) -> str:
    return hashlib.sha256(normalise_question(question).encode("utf-8")).hexdigest()


def corpus_digest(paper_sha256s: Iterable[str]) -> str:
    """One digest for the whole corpus: SHA-256 over the sorted per-paper SHA-256 values."""
    hashes = sorted(paper_sha256s)
    if not hashes:
        raise ValueError("corpus_digest needs at least one paper hash")
    return hashlib.sha256("\n".join(hashes).encode("ascii")).hexdigest()


# --- Exact cache ---------------------------------------------------------------------


class CacheScope(BaseModel):
    """Everything besides the question that an answer depends on."""

    model_config = _STRICT

    corpus_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    prompt_version: Annotated[str, Field(min_length=1)]
    model_id: Annotated[str, Field(min_length=1)]


class CachedSource(BaseModel):
    """One passage of a cached answer's ``sources`` event, kept so that a hit on
    POST /v1/ask sends exactly what the live answer sent (V2-14)."""

    model_config = _STRICT

    id: str  # "P1"
    chunk_id: str
    paper_id: str
    short_name: str | None
    page_range: str
    section: str | None
    text_excerpt: str | None
    text: str | None


class CachedAnswer(BaseModel):
    """What is replayed on a hit: the delivered text, its verified citations and (for
    answers cached by POST /v1/ask) the retrieved passages it was answered from."""

    model_config = _STRICT

    delivered_text: str
    refused: bool
    citations: tuple[CitedPassage, ...]
    verification_verdict: VerificationVerdict
    model_id: str | None
    source_request_id: str
    sources: tuple[CachedSource, ...] = ()  # empty for entries written by v1's POST /ask


CacheKey = tuple[str, str, str, str, str]


class ExactCache:
    def __init__(self, scope: CacheScope, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.scope = scope
        self.max_entries = max_entries
        self._entries: OrderedDict[CacheKey, CachedAnswer] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.rejected = 0
        # V2-13: with a shared store, every worker reads and writes the same answers
        self.shared: StateStore | None = None
        self.shared_ttl_s = 0.0

    def share(self, store: StateStore, *, ttl_s: float) -> None:
        """Keep answers in ``store`` (Redis) instead of this process's memory."""
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self.shared, self.shared_ttl_s = store, ttl_s

    @staticmethod
    def key_text(key: CacheKey) -> str:
        return "|".join(key)

    def __len__(self) -> int:
        return len(self._entries)

    def key(self, question: str, retrieval_setting: str) -> CacheKey:
        return (self.scope.corpus_sha256, retrieval_setting, self.scope.prompt_version,
                self.scope.model_id, question_hash(question))

    def get(self, question: str, retrieval_setting: str) -> CachedAnswer | None:
        key = self.key(question, retrieval_setting)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry

    def put(self, question: str, retrieval_setting: str, answer: CachedAnswer, *,
            degraded: bool) -> bool:
        """Store ``answer`` if it is admissible. Returns whether it was stored."""
        if degraded or answer.verification_verdict not in CACHEABLE_VERDICTS:
            self.rejected += 1
            return False
        key = self.key(question, retrieval_setting)
        self._entries[key] = answer
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)  # least recently used
        return True

    async def lookup(self, question: str, retrieval_setting: str) -> CachedAnswer | None:
        """``get``, from the shared store when there is one. A store failure is a miss:
        the answer is generated again, never lost."""
        if self.shared is None:
            return self.get(question, retrieval_setting)
        try:
            entry = await self.shared.get_cached_answer(
                self.key_text(self.key(question, retrieval_setting)))
        except Exception as exc:
            logger.warning("shared cache read failed (%s): treated as a miss",
                           type(exc).__name__)
            entry = None
        if entry is None:
            self.misses += 1
        else:
            self.hits += 1
        return entry

    async def admit(self, question: str, retrieval_setting: str, answer: CachedAnswer, *,
                    degraded: bool) -> bool:
        """``put``, into the shared store when there is one (same admission rule)."""
        if self.shared is None:
            return self.put(question, retrieval_setting, answer, degraded=degraded)
        if degraded or answer.verification_verdict not in CACHEABLE_VERDICTS:
            self.rejected += 1
            return False
        await self.shared.set_cached_answer(
            self.key_text(self.key(question, retrieval_setting)), answer, self.shared_ttl_s)
        return True

    def purge_collection(self, collection_id: str) -> int:
        """Remove every answer cached for one collection (V2-10: a deleted collection).
        Its keys carry ``setting@<collection_id>:v<version>:...``; returns the count."""
        marker = f"@{collection_id}:"
        doomed = [key for key in self._entries if marker in key[1]]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = self.misses = self.rejected = 0


# --- SingleFlight --------------------------------------------------------------------


class Flight(Generic[T]):
    """One in-flight piece of work and the number of callers still waiting for it."""

    def __init__(self) -> None:
        self.waiters = 0
        self.task: asyncio.Task[T] | None = None

    @property
    def has_waiters(self) -> bool:
        return self.waiters > 0


@dataclass(frozen=True)
class FlightResult(Generic[T]):
    value: T
    role: Role


class SingleFlight(Generic[T]):
    def __init__(self) -> None:
        self._flights: dict[Hashable, Flight[T]] = {}
        self.led = 0
        self.followed = 0

    def __len__(self) -> int:
        return len(self._flights)

    def in_flight(self, key: Hashable) -> bool:
        return key in self._flights

    async def _lead(self, key: Hashable, flight: Flight[T],
                    work: Callable[[Flight[T]], Awaitable[T]]) -> T:
        try:
            return await work(flight)
        finally:
            if self._flights.get(key) is flight:
                del self._flights[key]

    async def do(self, key: Hashable, work: Callable[[Flight[T]], Awaitable[T]], *,
                 follower_timeout_s: float | None = None) -> FlightResult[T]:
        """Run ``work`` once per key."""
        flight = self._flights.get(key)
        role: Role = "follower"
        if flight is None:
            role = "leader"
            flight = Flight()
            self._flights[key] = flight
            flight.task = asyncio.create_task(self._lead(key, flight, work))
            self.led += 1
        else:
            self.followed += 1
        task = flight.task
        if task is None:  # set in the same step as the dict entry, so never observed
            raise RuntimeError("flight has no task")
        flight.waiters += 1
        try:
            shielded = asyncio.shield(task)
            if role == "follower" and follower_timeout_s is not None:
                value = await asyncio.wait_for(shielded, max(0.0, follower_timeout_s))
            else:
                value = await shielded
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not task.done():
                # Nobody is waiting any more.
                if self._flights.get(key) is flight:
                    del self._flights[key]
                task.cancel()
        return FlightResult(value, role)


__all__ = [
    "CACHEABLE_VERDICTS",
    "CacheScope",
    "CachedAnswer",
    "CachedSource",
    "ExactCache",
    "Flight",
    "FlightResult",
    "SingleFlight",
    "corpus_digest",
    "normalise_question",
    "question_hash",
]
