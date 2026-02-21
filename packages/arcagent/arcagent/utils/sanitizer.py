"""Shared text sanitization for memory modules.

Provides NFKC normalization, zero-width character stripping, control
character removal, wiki-link target validation, and slug generation.
Used by both bio_memory and markdown_memory modules to prevent memory
poisoning (OWASP ASI-06).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

# Zero-width and invisible formatting characters
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]")

# ASCII control characters except tab (0x09), newline (0x0A), CR (0x0D)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Non-alphanumeric characters for slug generation (strips hyphens too)
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Slug generation preserving hyphens (for filenames, episode titles)
_SLUG_KEEP_HYPHENS_RE = re.compile(r"[^a-z0-9-]+")

# Maximum wiki-link slug length
_MAX_LINK_LENGTH = 200

# Patterns that look like prompt injection via link names
_INSTRUCTION_PREFIXES = ("system:", "ignore:", "instruction:", "prompt:", "admin:")


def sanitize_text(text: str, max_length: int = 2000) -> str:
    """Sanitize text for memory storage.

    Defense-in-depth against memory poisoning (ASI-06):
    1. NFKC normalization (collapses confusable characters)
    2. Strip zero-width characters (prevents invisible text injection)
    3. Strip ASCII control characters (preserves tab/newline/CR)
    4. Enforce length limit
    """
    clean = unicodedata.normalize("NFKC", text)
    clean = _ZERO_WIDTH_RE.sub("", clean)
    clean = _CONTROL_CHAR_RE.sub("", clean)
    return clean[:max_length]


def sanitize_wiki_link(link: str) -> str | None:
    """Sanitize a wiki-link target and return normalized slug.

    Rejects:
    - Empty or whitespace-only links
    - Path traversal (../)
    - Absolute paths (/)
    - Links exceeding max length
    - Links that look like instructions (prompt injection via link names)

    Returns normalized slug or None if invalid.
    """
    if not link or not link.strip():
        return None

    # Normalize first (catches homoglyphs, fullwidth chars)
    clean = unicodedata.normalize("NFKC", link)
    clean = _ZERO_WIDTH_RE.sub("", clean)

    # Reject instruction-like links (ASI-06: link-as-instruction defense)
    lower = clean.lower().strip()
    if any(lower.startswith(prefix) for prefix in _INSTRUCTION_PREFIXES):
        return None

    # Reject path traversal
    if ".." in clean or clean.startswith("/"):
        return None

    # Length check before slugification
    if len(clean) > _MAX_LINK_LENGTH:
        return None

    # Slugify: lowercase, replace non-alphanumeric with hyphens
    slug = _SLUG_RE.sub("-", clean.lower().strip()).strip("-")

    if not slug:
        return None

    return slug


def slugify(text: str) -> str:
    """Generate a URL-safe slug from text, preserving hyphens.

    Used for episode filenames and other identifiers where hyphens
    are meaningful separators.
    """
    return _SLUG_KEEP_HYPHENS_RE.sub("-", text.lower().strip()).strip("-") or "untitled"


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a markdown file.

    Returns the frontmatter dict, or None if the file cannot be read,
    has no frontmatter, or the frontmatter is not a dict.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    if not text.startswith("---"):
        return None

    end = text.find("\n---", 3)
    if end == -1:
        return None

    fm_text = text[4:end]
    try:
        result = yaml.safe_load(fm_text)
        return result if isinstance(result, dict) else None
    except yaml.YAMLError:
        return None
