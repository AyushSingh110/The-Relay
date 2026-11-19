"""Document registry: what a collection contains, derived from the store (plan V2-03).

v1 identified its three papers by constants in the code. A v2 collection's identity comes
from its ``document`` rows: ``DocumentRegistry`` turns them into

- **scope cues** for the scope analyser (``relay.retrieval.scope.ScopeCues``): each ready
  document contributes its title, the short name before a colon in its title (for example
  "Moshi"), and its arXiv identifier if it came from arXiv; arXiv sources also pin their
  version;
- the collection's **retrieval setting**: the preset named in ``collection.retrieval_preset``
  with reranking switched on when ``collection.rerank`` is set (migration 002).

``document_paper_id`` is the id the chunks of a document carry: the exact arXiv id
(``2410.00037v1``) for arXiv sources, otherwise the document's own id. The ingest worker
uses the same function, so cues and passages always agree.

Only ``ready`` documents count: a document still being ingested has no passages yet.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from relay.errors import RelayError
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS, RetrievalSetting
from relay.retrieval.scope import ScopeCues
from relay.store.repositories import CollectionRepo, Document, DocumentRepo

ARXIV_SOURCE: Final = re.compile(r"^arxiv:(\d{4}\.\d{4,5})v(\d+)$")
SHORT_NAME_MAX_WORDS: Final = 4
# A title is a cue only if it is specific enough not to match ordinary questions: an
# upload titled "paper" or "a" would otherwise mark every question as about that document.
TITLE_MIN_WORDS: Final = 2
TITLE_MIN_CHARS: Final = 8
SHORT_NAME_MIN_CHARS: Final = 4


class CollectionNotFoundError(RelayError):
    """No collection with that id."""


class UnknownPresetError(RelayError):
    """The collection names a retrieval preset this code does not have."""


def arxiv_source(arxiv_id: str, version: int) -> str:
    return f"arxiv:{arxiv_id}v{version}"


def parse_arxiv_source(source: str) -> tuple[str, int] | None:
    match = ARXIV_SOURCE.match(source)
    return None if match is None else (match.group(1), int(match.group(2)))


def document_paper_id(document: Document) -> str:
    arxiv = parse_arxiv_source(document.source)
    return document.id if arxiv is None else f"{arxiv[0]}v{arxiv[1]}"


def _normalise(text: str) -> str:
    """Same normalisation the scope analyser applies to questions."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def document_cues(document: Document) -> tuple[str, ...]:
    """Regex patterns that name this document in a question."""
    patterns: list[str] = []
    title = _normalise(document.title or "")
    if len(title.split()) >= TITLE_MIN_WORDS and len(title) >= TITLE_MIN_CHARS:
        patterns.append(re.escape(title))
    head = title.split(":", 1)[0].strip()
    if (head != title and len(head) >= SHORT_NAME_MIN_CHARS
            and len(head.split()) <= SHORT_NAME_MAX_WORDS):
        patterns.append(re.escape(head))
    arxiv = parse_arxiv_source(document.source)
    if arxiv is not None:
        patterns.append(re.escape(arxiv[0]))
    return tuple(dict.fromkeys(patterns))


class DocumentRegistry:
    def __init__(self, documents: DocumentRepo, collections: CollectionRepo) -> None:
        self._documents = documents
        self._collections = collections

    async def _ready_documents(self, collection_id: str) -> list[Document]:
        if await self._collections.get_by_id(collection_id) is None:
            raise CollectionNotFoundError(f"collection {collection_id!r} does not exist")
        documents = await self._documents.list_for_collection(collection_id)
        return [d for d in documents if d.status == "ready"]

    async def get_cues(self, collection_id: str) -> ScopeCues:
        strong: dict[str, tuple[str, ...]] = {}
        pinned: dict[str, int] = {}
        for document in await self._ready_documents(collection_id):
            patterns = document_cues(document)
            if patterns:
                strong[document_paper_id(document)] = patterns
            arxiv = parse_arxiv_source(document.source)
            if arxiv is not None:
                pinned[arxiv[0]] = arxiv[1]
        return ScopeCues(strong=strong, weak={}, pinned_versions=pinned)

    async def paper_ids(self, collection_id: str) -> tuple[str, ...]:
        return tuple(sorted(document_paper_id(d)
                            for d in await self._ready_documents(collection_id)))

    async def retrieval_setting(self, collection_id: str) -> RetrievalSetting:
        collection = await self._collections.get_by_id(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"collection {collection_id!r} does not exist")
        return retrieval_setting_for(collection.retrieval_preset, collection.rerank)


def retrieval_setting_for(preset: str, rerank: bool) -> RetrievalSetting:
    """The preset, with reranking added when the collection asks for it."""
    base = RETRIEVAL_SETTINGS.get(preset)
    if base is None:
        raise UnknownPresetError(f"unknown retrieval preset {preset!r}; known: "
                                 f"{sorted(RETRIEVAL_SETTINGS)}")
    if not rerank or base.rerank:
        return base
    return base.model_copy(update={"name": f"{base.name}_rerank", "rerank": True})


__all__ = ["CollectionNotFoundError", "DocumentRegistry", "UnknownPresetError", "arxiv_source",
           "document_cues", "document_paper_id", "parse_arxiv_source", "retrieval_setting_for"]
