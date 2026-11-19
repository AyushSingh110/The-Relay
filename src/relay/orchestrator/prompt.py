"""Grounded-synthesis prompt (pipeline step 4, the only LLM call).

The system prompt names the corpus through its cue set (V2-04): the v1 corpus renders the
exact v1 prompt (``PROMPT_VERSION`` p05-1); a collection's prompt names its documents and is
versioned ``COLLECTION_PROMPT_VERSION``. Passage headers use the corpus's titles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from relay.ingestion.v1_corpus import V1_CUE_SET, is_v1_corpus
from relay.orchestrator.messages import MAX_NAMED_DOCUMENTS, count_phrase
from relay.providers.base import ChatMessage, ChatRequest
from relay.retrieval.cues import CueSet
from relay.retrieval.hybrid_index import RetrievedPassage

PROMPT_VERSION: Final = "p05-1"  # the v1 prompt (v1 corpus)
COLLECTION_PROMPT_VERSION: Final = "p06-1"  # the same rules, naming a collection's documents
REFUSAL_MARKER: Final = "INSUFFICIENT_EVIDENCE"
DEFAULT_MAX_ANSWER_SENTENCES: Final = 6
_V1_TITLES: Final = V1_CUE_SET.titles_by_paper()

SYSTEM_TEMPLATE: Final = """\
You answer questions about {subject} using ONLY the numbered passages in the \
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


def subject(cues: CueSet) -> str:
    """How the system prompt names the corpus."""
    if cues.subject is not None:
        return cues.subject
    phrase = count_phrase(len(cues.documents), cues)
    if not 0 < len(cues.documents) <= MAX_NAMED_DOCUMENTS:
        return phrase
    return f"{phrase} ({'; '.join(d.display_name for d in cues.documents)})"


def system_prompt(cues: CueSet) -> str:
    """The system prompt for ``cues``, with the {marker} and {max_sentences} slots open.
    Braces in document names are escaped, so they survive the later ``str.format``."""
    named = subject(cues).replace("{", "{{").replace("}", "}}")
    return SYSTEM_TEMPLATE.replace("{subject}", named)


def prompt_version(cues: CueSet) -> str:
    return PROMPT_VERSION if is_v1_corpus(cues) else COLLECTION_PROMPT_VERSION


SYSTEM_PROMPT: Final = system_prompt(V1_CUE_SET)  # the v1 prompt, byte for byte


def passage_metadata(passage: RetrievedPassage,
                     titles: Mapping[str, str] | None = None) -> str:
    """The metadata line shown to the model (and treated as citable by the verifier).

    ``titles`` maps paper ids to titles (the corpus's cue set; default the v1 papers).
    arXiv documents are labelled with their arXiv id; other documents with their id."""
    title = (_V1_TITLES if titles is None else titles).get(passage.paper_id,
                                                         passage.short_name)
    label = (f"arXiv:{passage.arxiv_id}v{passage.version}" if passage.arxiv_id
             else f"document {passage.paper_id}")
    pages = (f"p. {passage.page_start}" if passage.page_start == passage.page_end
             else f"pp. {passage.page_start}-{passage.page_end}")
    section = (f"{passage.section_number} {passage.section_title}" if passage.section_number
               else passage.section_title)
    return f'"{title}" ({label}), {pages}, section: {section}'


def render_passages(passages: Sequence[RetrievedPassage],
                    titles: Mapping[str, str] | None = None) -> str:
    return "\n\n".join(
        f"[P{index}] {passage_metadata(passage, titles)}\n{passage.text}"
        for index, passage in enumerate(passages, start=1)
    )


def build_messages(question: str, passages: Sequence[RetrievedPassage], *,
                   max_sentences: int = DEFAULT_MAX_ANSWER_SENTENCES,
                   cues: CueSet | None = None) -> tuple[ChatMessage, ...]:
    """The two prompt messages; ``cues`` is the corpus (default the v1 papers)."""
    if not passages:
        raise ValueError("a grounded prompt needs at least one passage")
    corpus = V1_CUE_SET if cues is None else cues
    system = system_prompt(corpus).format(marker=REFUSAL_MARKER, max_sentences=max_sentences)
    user = (
        f"Passages:\n\n{render_passages(passages, corpus.titles_by_paper())}\n\n"
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
    cues: CueSet | None = None,
) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=build_messages(question, passages, max_sentences=max_sentences, cues=cues),
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )


__all__ = [
    "COLLECTION_PROMPT_VERSION",
    "PROMPT_VERSION",
    "REFUSAL_MARKER",
    "SYSTEM_PROMPT",
    "SYSTEM_TEMPLATE",
    "build_messages",
    "build_request",
    "passage_metadata",
    "prompt_version",
    "render_passages",
    "subject",
    "system_prompt",
]
