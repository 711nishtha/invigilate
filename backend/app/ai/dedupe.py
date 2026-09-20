"""Content hashing and duplicate detection.

Two layers, deliberately:

1. **Exact** — a normalized SHA-256 stored on every variant, with
   ``UNIQUE (family_id, content_hash)`` behind it. The database refuses exact
   duplicates even when two generation requests race.
2. **Near** — token-set similarity, which catches a reworded copy that hashes
   differently.

The hash covers only the immutable question content: stem plus options. It
deliberately excludes the explanation, topic, difficulty and any provenance,
because two variants that ask the identical question are duplicates however
their metadata differs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

DEFAULT_SIMILARITY_THRESHOLD = 0.92

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Casefold, strip punctuation and collapse whitespace.

    So that "What is the time complexity of binary search?" and "what is the
    time complexity of binary search" are recognised as the same question.
    NFKC first, so visually identical Unicode spellings do not slip past.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def content_hash(question_text: str, options: list[str] | None) -> str:
    """Deterministic hash of the immutable question content.

    Options are sorted before hashing: the same four options in a different
    order are the same question, and the per-attempt permutation means stored
    order carries no meaning anyway.
    """
    parts = [normalize_text(question_text)]
    if options:
        parts.extend(sorted(normalize_text(option) for option in options))
    digest = hashlib.sha256("␟".join(parts).encode("utf-8"))
    return digest.hexdigest()


def similarity(left: str, right: str) -> float:
    """Token-set similarity of two question stems, between 0 and 1."""
    left_tokens = normalize_text(left).split()
    right_tokens = normalize_text(right).split()
    if not left_tokens or not right_tokens:
        return 0.0
    return SequenceMatcher(None, left_tokens, right_tokens).ratio()


def find_near_duplicate(
    candidate: str,
    existing: list[tuple[str, str]],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[str, float] | None:
    """Return the closest existing text above ``threshold``, if any.

    ``existing`` is a list of ``(identifier, text)``. The identifier is echoed
    back so the caller can tell the teacher which variant it collided with.
    """
    best: tuple[str, float] | None = None
    for identifier, text in existing:
        score = similarity(candidate, text)
        if score >= threshold and (best is None or score > best[1]):
            best = (identifier, score)
    return best


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "content_hash",
    "find_near_duplicate",
    "normalize_text",
    "similarity",
]
