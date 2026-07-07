"""Glass-box markdown helpers — atomic writes + YAML frontmatter.

The curated stores (semantic/procedural/insight) are human-editable markdown with
a YAML frontmatter block. This module is the single place that reads, renders, and
atomically writes them, so every store treats the on-disk truth identically.

Absorbed from ``arcagent/utils/{io,sanitizer}`` (frontmatter read + atomic write) --
re-homed here because arcmemory must not import arcagent.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


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
    """Split a markdown doc into (frontmatter dict, body). No frontmatter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), body


def render_document(frontmatter: dict[str, Any], body: str) -> str:
    """Render frontmatter + body back to a markdown string."""
    fm_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{fm_text}\n---\n\n{body.strip()}\n"


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Read just the frontmatter dict from ``path`` (None if unreadable/absent)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, _ = parse_document(text)
    return fm or None


__all__ = ["atomic_write_text", "parse_document", "read_frontmatter", "render_document"]
