"""Cue sets: how a corpus's documents are named in questions and answers (plan V2-04).

A ``CueSet`` is the per-corpus input of the scope analyser, the evidence gate, the verifier
and the user-facing messages. It holds no paper data itself:

- the v1 corpus is defined once in ``relay.ingestion.v1_corpus`` (curated, so v1
  behaviour is unchanged);
- a collection's cue set comes from its documents in the store
  (``relay.ingestion.registry.DocumentRegistry.get_cues``), each derived at ingestion by
  ``derive_document_cues`` and stored in ``document.cues`` (migration 003).

Per document, ``strong`` patterns name the document (a hit makes the question about it);
``weak`` patterns are its topic vocabulary (shared words that only suggest it). Patterns are
regular expressions matched on the casefolded, whitespace-normalised text, bounded by
non-alphanumerics. ``title``, ``short_names``, ``arxiv_id`` / ``version`` and
``display_name`` are plain text, for messages and prompts.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHORT_NAME_MAX_WORDS: Final = 4
SHORT_NAME_MIN_CHARS: Final = 4
# A title is a cue only if it is specific enough not to match ordinary questions: a
# document titled "paper" or "a" would otherwise mark every question as about it.
TITLE_MIN_WORDS: Final = 2
TITLE_MIN_CHARS: Final = 8
_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


class DocumentCues(BaseModel):
    model_config = _STRICT

    paper_id: Annotated[str, Field(min_length=1, max_length=128)]
    display_name: Annotated[str, Field(min_length=1, max_length=300)]
    title: str | None = None
    short_names: tuple[str, ...] = ()
    arxiv_id: str | None = None  # bare id, e.g. "1706.03762"
    version: int | None = None
    strong: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()

    @field_validator("strong", "weak")
    @classmethod
    def _compiles(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:  # stored cues are data: a bad pattern must not reach a request
            try:
                bounded(pattern)
            except re.error as exc:
                raise ValueError(f"cue pattern {pattern!r} is not a valid regex: {exc}") from None
        return patterns

    def name_patterns(self) -> tuple[str, ...]:
        """The patterns that NAME the document (title and short names, never aliases or
        topics): what blocks it, as a foreign document, in another collection."""
        names = []
        title = " ".join((self.title or "").split())
        if len(title.split()) >= TITLE_MIN_WORDS and len(title) >= TITLE_MIN_CHARS:
            names.append(re.escape(normalise(title)))
        names += [re.escape(normalise(n)) for n in self.short_names
                  if len(n) >= SHORT_NAME_MIN_CHARS]
        return tuple(dict.fromkeys(names))

    @property
    def versioned_id(self) -> str | None:
        if self.arxiv_id is None or self.version is None:
            return None
        return f"{self.arxiv_id}v{self.version}"


class CueSet(BaseModel):
    """The documents of one corpus, in a fixed order (the order cues are evaluated in)."""

    model_config = _STRICT

    documents: tuple[DocumentCues, ...]
    noun: str = "documents"  # how messages refer to the corpus's items
    noun_singular: str = "document"
    subject: str | None = None  # how the system prompt names the corpus; None = derived

    @model_validator(mode="after")
    def _unique(self) -> CueSet:
        ids = [d.paper_id for d in self.documents]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate documents in a cue set: {ids}")
        return self

    # --- the plain-text view (titles, short names, versioned arXiv ids) ---

    @property
    def paper_ids(self) -> tuple[str, ...]:
        return tuple(d.paper_id for d in self.documents)

    @property
    def titles(self) -> list[str]:
        return [d.title for d in self.documents if d.title]

    @property
    def short_names(self) -> list[str]:
        return [name for d in self.documents for name in d.short_names]

    @property
    def arxiv_ids(self) -> list[str]:
        return [v for d in self.documents if (v := d.versioned_id) is not None]

    # --- the pattern view, keyed by paper id ---

    def strong_cues(self) -> dict[str, tuple[str, ...]]:
        return {d.paper_id: d.strong for d in self.documents if d.strong}

    def weak_cues(self) -> dict[str, tuple[str, ...]]:
        return {d.paper_id: d.weak for d in self.documents if d.weak}

    def name_cues(self) -> dict[str, tuple[str, ...]]:
        """Strong patterns that are names, not identifiers (no digits): the verifier's
        paper-name check."""
        names = {d.paper_id: tuple(p for p in d.strong if not re.search(r"\d", p))
                 for d in self.documents}
        return {paper_id: patterns for paper_id, patterns in names.items() if patterns}

    def pinned_versions(self) -> dict[str, int]:
        return {d.arxiv_id: d.version for d in self.documents
                if d.arxiv_id is not None and d.version is not None}

    def pinned_papers(self) -> dict[str, str]:
        return {d.arxiv_id: d.paper_id for d in self.documents if d.arxiv_id is not None}

    def titles_by_paper(self) -> dict[str, str]:
        return {d.paper_id: d.title for d in self.documents if d.title}


def bounded(pattern: str) -> re.Pattern[str]:
    """A cue pattern matched as a whole term (not inside a longer word)."""
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")


def normalise(text: str) -> str:
    """The normalisation questions are matched in (NFKC, casefold, single spaces)."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def derive_document_cues(paper_id: str, *, title: str | None, arxiv_id: str | None = None,
                         version: int | None = None) -> DocumentCues:
    """Cues for a document from what ingestion knows: its title, the short name before a
    colon in the title (for example "Moshi"), and its arXiv id. No topic cues."""
    clean = " ".join((title or "").split())
    head = clean.split(":", 1)[0].strip()
    has_head = (head != clean and len(head) >= SHORT_NAME_MIN_CHARS
                and len(head.split()) <= SHORT_NAME_MAX_WORDS)
    patterns: list[str] = []
    if len(clean.split()) >= TITLE_MIN_WORDS and len(clean) >= TITLE_MIN_CHARS:
        patterns.append(re.escape(normalise(clean)))
    if has_head:
        patterns.append(re.escape(normalise(head)))
    if arxiv_id is not None:
        patterns.append(re.escape(arxiv_id))
    if has_head:
        display = head
    elif patterns and clean:
        display = f'"{clean}"'
    else:
        display = f"document {paper_id[:8]}"
    return DocumentCues(paper_id=paper_id, display_name=display, title=clean or None,
                        short_names=(head,) if has_head else (), arxiv_id=arxiv_id,
                        version=version, strong=tuple(dict.fromkeys(patterns)))


__all__ = ["CueSet", "DocumentCues", "bounded", "derive_document_cues", "normalise"]
