"""Document registry: what a collection contains, derived from the store (plan V2-03).

v1 identified its three papers by constants in the code. A v2 collection's identity comes
from its ``document`` rows: ``DocumentRegistry`` turns them into

- the collection's **cue set** (``relay.retrieval.cues.CueSet``, V2-04) for the scope
  analyser, evidence gate, verifier and messages. Each ready document contributes its cues:
  the curated v1 cues for the three v1 papers (``relay.ingestion.v1_corpus``), otherwise
  cues derived from its title, the short name before a colon in its title, and its arXiv
  identifier (``relay.retrieval.cues.derive_document_cues``). A collection holding exactly
  the three v1 papers gets the v1 cue set itself, so its wording matches v1;
- the collection's **retrieval setting**: the preset named in ``collection.retrieval_preset``
  with reranking switched on when ``collection.rerank`` is set (migration 002).

``document_paper_id`` is the id the chunks of a document carry: the exact arXiv id
(``2410.00037v1``) for arXiv sources, otherwise the document's own id. The ingest worker
uses the same function, so cues and passages always agree.

Only ``ready`` documents count: a document still being ingested has no passages yet.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Final

from pydantic import ValidationError

from relay.errors import RelayError
from relay.ingestion.v1_corpus import V1_CUE_SET, V1_DOCUMENTS, v1_document_cues
from relay.retrieval.cues import CueSet, DocumentCues, derive_document_cues
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS, RetrievalSetting
from relay.store.repositories import CollectionRepo, Document, DocumentRepo

logger = logging.getLogger(__name__)

ARXIV_SOURCE: Final = re.compile(r"^arxiv:(\d{4}\.\d{4,5})v(\d+)$")


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


def _stored_cues(document: Document, paper_id: str) -> DocumentCues | None:
    if document.cues is None:
        return None
    try:
        stored = DocumentCues.model_validate_json(document.cues)
    except ValidationError:
        logger.warning("document %s has unreadable cues; deriving them instead", document.id)
        return None
    if stored.paper_id != paper_id:
        logger.warning("document %s has cues for another paper id; deriving them instead",
                       document.id)
        return None
    return stored


def cues_for_document(document: Document) -> DocumentCues:
    """The cues of one document: the stored cues (migration 003) when readable, else the
    curated cues of a v1 paper, else cues derived from its title and arXiv id."""
    paper_id = document_paper_id(document)
    stored = _stored_cues(document, paper_id)
    if stored is not None:
        return stored
    curated = v1_document_cues(paper_id)
    if curated is not None:
        return curated
    arxiv = parse_arxiv_source(document.source)
    return derive_document_cues(paper_id, title=document.title,
                                arxiv_id=None if arxiv is None else arxiv[0],
                                version=None if arxiv is None else arxiv[1])


def document_cues(document: Document) -> tuple[str, ...]:
    """The regex patterns that name this document in a question."""
    return cues_for_document(document).strong


def cue_set_for(documents: Sequence[DocumentCues]) -> CueSet:
    """The cue set of a collection's documents, ordered by paper id. Exactly the three v1
    papers give the v1 cue set (v1 wording); anything else is worded as "documents"."""
    ordered = tuple(sorted(documents, key=lambda d: d.paper_id))
    if ordered == V1_DOCUMENTS:
        return V1_CUE_SET
    return CueSet(documents=ordered)


class DocumentRegistry:
    def __init__(self, documents: DocumentRepo, collections: CollectionRepo) -> None:
        self._documents = documents
        self._collections = collections

    async def _ready_documents(self, collection_id: str) -> list[Document]:
        if await self._collections.get_by_id(collection_id) is None:
            raise CollectionNotFoundError(f"collection {collection_id!r} does not exist")
        documents = await self._documents.list_for_collection(collection_id)
        return [d for d in documents if d.status == "ready"]

    async def get_cues(self, collection_id: str) -> CueSet:
        """The cue set of the collection's ready documents."""
        documents = await self._ready_documents(collection_id)
        unique = {cues.paper_id: cues for cues in map(cues_for_document, documents)}
        return cue_set_for(list(unique.values()))

    async def foreign_cues(self, collection_id: str) -> tuple[DocumentCues, ...]:
        """Cues of the tenant's ready documents in its OTHER collections, minus documents
        this collection also holds: the names the verifier blocks here (isolation)."""
        collection = await self._collections.get_by_id(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"collection {collection_id!r} does not exist")
        mine = await self._ready_documents(collection_id)
        own = {document_paper_id(d) for d in mine}
        same_content = {d.sha256 for d in mine}  # the same PDF uploaded twice
        foreign: dict[str, DocumentCues] = {}
        for other in await self._collections.list_for_tenant(collection.tenant_id):
            if other.id == collection_id:
                continue
            for document in await self._ready_documents(other.id):
                if (document_paper_id(document) not in own
                        and document.sha256 not in same_content):
                    cues = cues_for_document(document)
                    foreign.setdefault(cues.paper_id, cues)
        return tuple(foreign[paper_id] for paper_id in sorted(foreign))

    async def tenant_fingerprint(self, collection_id: str) -> str:
        """Changes whenever any collection of the collection's tenant changes (each change
        bumps a version): what the foreign names, and so verification, depend on."""
        collection = await self._collections.get_by_id(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"collection {collection_id!r} does not exist")
        state = sorted((c.id, c.version) for c in
                       await self._collections.list_for_tenant(collection.tenant_id))
        return hashlib.sha256(repr(state).encode("utf-8")).hexdigest()[:16]

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
           "cue_set_for", "cues_for_document", "document_cues", "document_paper_id",
           "parse_arxiv_source", "retrieval_setting_for"]
