"""Grounded-synthesis prompt (pipeline step 4, the only LLM call).

Passages are numbered ``[P1]..[Pk]``, each with a metadata line (paper title, exact arXiv
version, pages, section). The model must cite a tag in every sentence or reply with the
refusal marker and nothing else. ``PROMPT_VERSION`` changes whenever the wording does,
so cached answers and run manifests can tell prompt generations apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from relay.ingestion.arxiv_fetcher import PAPERS
from relay.providers.base import ChatMessage, ChatRequest
from relay.retrieval.hybrid_index import RetrievedPassage

PROMPT_VERSION: Final = "p05-1"
REFUSAL_MARKER: Final = "INSUFFICIENT_EVIDENCE"
DEFAULT_MAX_ANSWER_SENTENCES: Final = 6
_TITLES: Final = {p.paper_id: p.title for p in PAPERS}

SYSTEM_PROMPT: Final = """\
You answer questions about three research papers using ONLY the numbered passages in the \
user message.

Rules:
1. Use only what the passages state. Do not use outside knowledge, even if you know the answer.
2. End every sentence with the tag(s) of the passage(s) that support it, written exactly \
like [P1] or [P1][P3]. Never cite a tag that was not provided.
3. Copy numbers, symbols and technical names exactly as they appear in the passages \
(for example d_model or 12.5 Hz). Do not calculate new numbers.
4. If you name a paper, an arXiv ID or a page, it must match the header of a passage you \
cite in that sentence.
5. If the passages do not contain the information needed to answer, reply with exactly \
{marker} and nothing else. Do not explain and do not guess.
6. If the passages support only part of the question, answer only that part, with tags, \
and do not comment on the rest.
7. Write at most {max_sentences} plain sentences. No headings, tables or preamble."""


def passage_metadata(passage: RetrievedPassage) -> str:
    """The metadata line shown to the model (and treated as citable by the verifier)."""
    title = _TITLES.get(passage.paper_id, passage.short_name)
    pages = (f"p. {passage.page_start}" if passage.page_start == passage.page_end
             else f"pp. {passage.page_start}-{passage.page_end}")
    section = (f"{passage.section_number} {passage.section_title}" if passage.section_number
               else passage.section_title)
    return f'"{title}" (arXiv:{passage.paper_id}), {pages}, section: {section}'


def render_passages(passages: Sequence[RetrievedPassage]) -> str:
    return "\n\n".join(
        f"[P{index}] {passage_metadata(passage)}\n{passage.text}"
        for index, passage in enumerate(passages, start=1)
    )


def build_messages(question: str, passages: Sequence[RetrievedPassage], *,
                   max_sentences: int = DEFAULT_MAX_ANSWER_SENTENCES) -> tuple[ChatMessage, ...]:
    if not passages:
        raise ValueError("a grounded prompt needs at least one passage")
    system = SYSTEM_PROMPT.format(marker=REFUSAL_MARKER, max_sentences=max_sentences)
    user = (
        f"Passages:\n\n{render_passages(passages)}\n\n"
        f"Question: {question.strip()}\n\n"
        f"Answer using only the passages above, with a tag in every sentence, or reply "
        f"{REFUSAL_MARKER}."
    )
    return (ChatMessage(role="system", content=system), ChatMessage(role="user", content=user))


def build_request(
    question: str,
    passages: Sequence[RetrievedPassage],
    *,
    model: str,
    max_completion_tokens: int,
    temperature: float | None = 0.0,
    reasoning_effort: str | None = None,
    max_sentences: int = DEFAULT_MAX_ANSWER_SENTENCES,
) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=build_messages(question, passages, max_sentences=max_sentences),
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )


__all__ = [
    "PROMPT_VERSION",
    "REFUSAL_MARKER",
    "SYSTEM_PROMPT",
    "build_messages",
    "build_request",
    "passage_metadata",
    "render_passages",
]
