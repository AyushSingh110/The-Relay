"""User-facing refusal messages, rendered from a corpus's cue set (V2-04).

The v1 corpus renders the exact v1 texts (``tests/test_collection_aware_safety.py`` pins
them); a collection's messages name its own documents. No paper is named in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from relay.retrieval.cues import CueSet

MAX_NAMED_DOCUMENTS: Final = 10  # above this, messages and prompts give only the count
_NUMBER_WORDS: Final = ("zero", "one", "two", "three", "four", "five", "six", "seven",
                        "eight", "nine", "ten", "eleven", "twelve")


@dataclass(frozen=True)
class RefusalMessages:
    out_of_corpus: str  # early refusal: the corpus does not cover the question
    version: str  # early refusal: another version of one of the corpus's arXiv papers
    llm_refusal: str  # the model replied that the passages do not contain the answer
    safe_refusal: str  # the answer could not be verified, so none is given


def count_phrase(n: int, cues: CueSet) -> str:
    """'three papers', 'one document', '14 documents'."""
    number = _NUMBER_WORDS[n] if n < len(_NUMBER_WORDS) else str(n)
    return f"{number} {cues.noun_singular if n == 1 else cues.noun}"


def refusal_messages(cues: CueSet) -> RefusalMessages:
    documents = cues.documents
    n = len(documents)
    items = "; ".join(d.display_name if d.versioned_id is None
                      else f"{d.display_name}, arXiv:{d.versioned_id}" for d in documents)
    they, do = ("it", "does") if n == 1 else ("they", "do")
    versions = cues.arxiv_ids
    named = n <= MAX_NAMED_DOCUMENTS
    # Nothing version-pinned (not reachable from the gate today), or too many to list.
    if not versions or len(versions) > MAX_NAMED_DOCUMENTS:
        version_text = (f"I can only answer from the exact versions of the {cues.noun} in the "
                        "library, not from other versions.")
    elif len(versions) == 1:
        version_text = (f"I can only answer from the exact {cues.noun_singular} version in the "
                        f"library (arXiv:{versions[0]}), not from other versions of this "
                        f"{cues.noun_singular}.")
    else:
        listed = ", ".join(f"arXiv:{v}" for v in versions)
        version_text = (f"I can only answer from the exact {cues.noun_singular} versions in "
                        f"the library ({listed}), not from other versions of these "
                        f"{cues.noun}.")
    corpus = cues.noun_singular if n == 1 else cues.noun
    return RefusalMessages(
        out_of_corpus=(f"I can only answer from {count_phrase(n, cues)} ({items}), and "
                       f"{they} {do} not cover this question." if named else
                       f"I can only answer from {count_phrase(n, cues)} in this collection, "
                       f"and {they} {do} not cover this question."),
        version=version_text,
        llm_refusal=(f"I can't answer this from the {count_phrase(n, cues)} in the library: "
                     "the passages I found do not contain the information needed."),
        safe_refusal=(f"I could not produce an answer that I can verify against the {corpus}, "
                      "so I am not answering rather than risk an unsupported claim."),
    )


__all__ = ["MAX_NAMED_DOCUMENTS", "RefusalMessages", "count_phrase", "refusal_messages"]
