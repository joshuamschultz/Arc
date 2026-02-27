"""Shared I/O utilities.

Centralizes file I/O patterns used across the memory module:
- Token estimation constant
- Atomic write (tmp + rename)
- Message formatting for LLM prompts
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Approximate characters per token for budget estimation.
# Used by ContextGuard, NoteManager, and HybridSearch.
CHARS_PER_TOKEN = 4

# FTS5 special characters that must be stripped before MATCH queries.
_FTS5_SPECIAL_RE = re.compile(r'[*"{}^():\[\]]')


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via tmp + rename.

    Creates the parent directory if it does not exist. The rename is
    atomic on POSIX (same device). Uses ``os.rename`` which is safe
    when source and target share the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(str(tmp), str(path))


def format_messages(
    messages: list[dict[str, object]],
    limit: int = 10,
    *,
    type_filter: str = "",
) -> str:
    """Format recent messages as ``role: content`` lines.

    Args:
        messages: Message dicts with 'role' and 'content' keys.
        limit: Max messages to include (0 = unlimited).
        type_filter: If set, only include messages where ``type`` matches.
    """
    filtered = messages
    if type_filter:
        filtered = [m for m in filtered if m.get("type") == type_filter]
    recent = filtered[-limit:] if limit > 0 else filtered
    return "\n".join(f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in recent)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str | None) -> str:
    """Extract JSON from an LLM response that may be wrapped in markdown fences.

    LLMs often return JSON inside ```json ... ``` blocks. This strips
    the fences so json.loads() can parse the content.
    """
    if not text:
        return ""
    match = _JSON_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def sanitize_fts5_query(query: str) -> str:
    """Escape FTS5 special characters to prevent query syntax errors.

    Strips characters that FTS5 interprets as operators (``*``, ``"``,
    ``{}``, etc.) and individually quotes each term.
    """
    return " ".join(
        f'"{clean}"' for term in query.split() if (clean := _FTS5_SPECIAL_RE.sub("", term))
    )
