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
from pathlib import Path


@lru_cache(maxsize=2048)
def _phrase_regex(slug: str) -> re.Pattern[str]:
    """Whole-phrase, case-insensitive matcher for a slug (``a-b`` -> ``a b``)."""
    phrase = slug.replace("-", " ").strip()
    return re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)


def tag_entities(text: str, vocabulary: Iterable[str]) -> list[str]:
    """Return the vocabulary slugs mentioned in ``text`` (sorted, deduped)."""
    found = {slug for slug in vocabulary if slug and _phrase_regex(slug).search(text)}
    return sorted(found)


def entity_vocabulary(mem_dir: Path, seed_vocab: Iterable[str] = ()) -> set[str]:
    """Seed terms unioned with the slugs of the entity files under ``mem_dir``.

    The one builder the fast-capture path, both retrieval channels, and the rebuild
    replay share, so the controlled tagging vocabulary can never silently diverge
    between the site that *writes* an edge and the site that *reads* it.
    """
    vocab = set(seed_vocab)
    entities_dir = mem_dir / "entities"
    if entities_dir.exists():
        vocab.update(p.stem for p in entities_dir.glob("*.md"))
    return vocab


__all__ = ["entity_vocabulary", "tag_entities"]
