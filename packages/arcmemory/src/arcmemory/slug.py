"""Canonical identity key for a memory record's file (the UPSERT match key).

The distiller proposes entity slugs, procedure slugs and insight ids as free LLM
text, so the *same* real-world thing arrives spelled differently across runs
("Custom ERP", "custom_erp", "custom-erp"). Folding those variants to one
deterministic form is what makes distillation UPSERT into the existing card
instead of minting a duplicate file — and, because the key becomes a filename,
stripping separators keeps an LLM-proposed key from escaping the store directory
(LLM05, improper output handling).
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_slug(raw: str) -> str:
    """Fold arbitrary text to a stable, filesystem-safe lowercase-hyphen slug."""
    folded = unicodedata.normalize("NFKC", raw).casefold()
    slug = _NON_ALNUM.sub("-", folded).strip("-")
    return slug or "unknown"


__all__ = ["canonical_slug"]
