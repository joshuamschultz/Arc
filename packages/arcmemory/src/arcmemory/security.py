"""Untrusted-content defenses applied *before* anything becomes memory.

Every capture runs ``sanitize -> privacy_filter -> dedup`` (SDD 4.3, REQ-012).
This is the ASI06 (memory poisoning) / LLM01 (prompt injection) boundary: event
payloads are untrusted, so their text is normalized, injection-pattern-dropped,
secret-stripped, and windowed-deduped before it is ever written or embedded.

Absorbs the sanitizer that used to live in ``arcagent/utils/sanitizer.py`` -- it
moves here because arcmemory must not import arcagent (DC-2, DAG invariant).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import deque

# Zero-width + invisible formatting characters (instruction smuggling vectors).
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]")
# Soft hyphen, Mongolian vowel separator, variation selectors, Unicode Tag block.
_INVISIBLE_RE = re.compile(r"[\u00ad\u180e\ufe00-\ufe0f\U000e0000-\U000e007f]")
# ASCII control chars except tab/newline/CR; DEL too.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Injection-pattern dropping: instruction-hijack phrasings that should never be
# retained as memory. Matched case-insensitively; the offending span is removed.
_INJECTION_RE = re.compile(
    r"(?i)\b("
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|above)"
    r"|forget\s+(?:everything|all\s+previous)"
    r"|you\s+are\s+now\s+"
    r"|new\s+instructions?\s*:"
    r"|system\s+prompt\s*:"
    r"|override\s+(?:your\s+)?(?:instructions?|system)"
    r")\b[^\n]*"
)

# Secret formats to redact before storage (LLM02 sensitive-info; not exhaustive,
# but covers the common high-signal token shapes).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN[ A-Z]+PRIVATE KEY-----"),
)
_REDACTION = "[REDACTED]"


def sanitize(text: str, *, max_length: int = 2000) -> str:
    """Normalize, strip invisibles, drop injection patterns, enforce size cap.

    Order matters: normalize first (collapses confusables that would hide an
    injection phrase), strip invisible/control characters, drop injection spans,
    collapse the blank the drop leaves, then cap length last.
    """
    clean = unicodedata.normalize("NFKC", text)
    clean = _ZERO_WIDTH_RE.sub("", clean)
    clean = _INVISIBLE_RE.sub("", clean)
    clean = _CONTROL_CHAR_RE.sub("", clean)
    clean = _INJECTION_RE.sub("", clean)
    # Collapse runs of spaces the injection-drop may have left mid-line.
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    if len(clean) > max_length:
        return clean[:max_length]
    return clean


def privacy_filter(text: str) -> str:
    """Redact secret-shaped substrings so no key/token becomes memory."""
    filtered = text
    for pattern in _SECRET_PATTERNS:
        filtered = pattern.sub(_REDACTION, filtered)
    return filtered


def content_hash(text: str) -> str:
    """Stable SHA-256 hex of ``text`` -- the dedup + tamper-evidence key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Deduper:
    """Windowed content-hash dedup -- suppresses recently-seen identical text.

    Bounded memory: only the last ``window`` hashes are retained, so this is O(1)
    per check and constant-space regardless of stream length (Scalability).
    """

    def __init__(self, window: int = 128) -> None:
        self._window = window
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def is_duplicate(self, text: str) -> bool:
        """Return True if ``text`` was seen within the window; record it either way."""
        digest = content_hash(text)
        if digest in self._seen:
            return True
        self._order.append(digest)
        self._seen.add(digest)
        if len(self._order) > self._window:
            evicted = self._order.popleft()
            self._seen.discard(evicted)
        return False


__all__ = ["Deduper", "content_hash", "privacy_filter", "sanitize"]
