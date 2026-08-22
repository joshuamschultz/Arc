"""Glass-box markdown helpers — atomic writes + YAML frontmatter.

The curated stores (semantic/procedural/insight) are human-editable markdown with
a YAML frontmatter block. This module is the single place that reads, renders, and
atomically writes them, so every store treats the on-disk truth identically.

Absorbed from ``arcagent/utils/{io,sanitizer}`` (frontmatter read + atomic write) --
re-homed here because arcmemory must not import arcagent.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from arcokf import Document, OKFValidationError, parse, render

_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\.md\)")


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_document(text: str) -> tuple[dict[str, Any], str]:
    """Split a valid OKF document into ``(metadata, body)``."""
    document = parse(text)
    body = _MARKDOWN_LINK_RE.sub(lambda m: f"[[{m.group(1)}]]", document.body)
    return document.metadata, body


def _okf_body(body: str) -> str:
    """Render Arc's historical wiki links as standard OKF Markdown links."""
    return _WIKI_LINK_RE.sub(lambda match: f"[{match.group(1)}]({match.group(1)}.md)", body)


def render_document(frontmatter: dict[str, Any], body: str) -> str:
    """Render and validate a typed OKF v0.2 markdown document."""
    metadata = dict(frontmatter)
    metadata.setdefault("type", "ArcMemory")
    try:
        encoded = render(Document(metadata, _okf_body(body))) + "\n"
        parse(encoded)
        return encoded
    except OKFValidationError as error:
        raise ValueError("invalid ArcMemory OKF document") from error


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Read just the frontmatter dict from ``path`` (None if unreadable/absent)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, _ = parse_document(text)
    return fm or None


__all__ = ["atomic_write_text", "parse_document", "read_frontmatter", "render_document"]
