"""Deterministic entity tagging — controlled vocabulary + regex, zero LLM.

The fast path (and the rebuild replay) must tag entities identically and without
any model call. Given a vocabulary of entity slugs, ``tag_entities`` finds which
appear in a text as whole-phrase, case-insensitive matches. The result is sorted
so the same text + vocabulary always yields the same tags in the same order --
which is what makes ``index/rebuild`` able to reproduce the graph byte-identically.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=2048)
def _phrase_regex(slug: str) -> re.Pattern[str]:
    """Whole-phrase, case-insensitive matcher for a slug (``a-b`` -> ``a b``)."""
    phrase = slug.replace("-", " ").strip()
    return re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)


def tag_entities(text: str, vocabulary: Iterable[str]) -> list[str]:
    """Return the vocabulary slugs mentioned in ``text`` (sorted, deduped)."""
    found = {slug for slug in vocabulary if slug and _phrase_regex(slug).search(text)}
    return sorted(found)


__all__ = ["tag_entities"]
