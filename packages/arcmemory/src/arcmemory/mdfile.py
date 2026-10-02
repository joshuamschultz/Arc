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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcokf import Document, OKFValidationError, listable_file, parse, render

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


def card_files(directory: Path) -> list[Path]:
    """The concept cards in one store directory, sorted: never the reserved ``index.md``.

    Every folder carries an OKF ``index.md`` (and may carry ``log.md``); those
    describe the cards, they are not cards. The single place that rule lives, so
    no store lists, counts, tags or re-indexes its own folder index as a card.
    """
    return sorted(path for path in directory.glob("*.md") if listable_file(path.name))


def parse_document(text: str) -> tuple[dict[str, Any], str]:
    """Split a valid OKF document into ``(metadata, body)``."""
    document = parse(text)
    body = _MARKDOWN_LINK_RE.sub(lambda m: f"[[{m.group(1)}]]", document.body)
    return document.metadata, body


def _okf_body(body: str) -> str:
    """Render Arc's historical wiki links as standard OKF Markdown links."""
    return _WIKI_LINK_RE.sub(lambda match: f"[{match.group(1)}]({match.group(1)}.md)", body)


#: The card kind each store writes, recognised by the key only that store emits.
#: Order matters: an entity carries ``entity_type``, an insight a ``trigger``, a
#: procedure ``when_to_use``, an event ``event_type``, a day's notes ``day``.
_CARD_KINDS: tuple[tuple[str, str], ...] = (
    ("entity_type", "Entity"),
    ("trigger", "Insight"),
    ("when_to_use", "Procedure"),
    ("event_type", "Event"),
    ("day", "DailyLog"),
)
_GENERATED_BY = "process:arcmemory"
_DESCRIPTION_KEYS = ("description", "summary", "when_to_use", "trigger")


def _card_kind(metadata: dict[str, Any]) -> str:
    return next((kind for key, kind in _CARD_KINDS if key in metadata), "Note")


def _first_prose(body: str) -> str:
    for line in body.splitlines():
        text = line.strip().lstrip("-* ").strip()
        if text and not text.startswith("#"):
            return text
    return ""


def _card_metadata(frontmatter: dict[str, Any], body: str) -> dict[str, Any]:
    """Fill the OKF fields a card needs to be listed: ``type``, ``title``, ``description``.

    Only fills what the writer left out, so an explicit ``type`` (connected
    documents) or ``title`` always wins. ``generated`` records which process
    wrote the card, per OKF v0.2.
    """
    metadata = dict(frontmatter)
    metadata.setdefault("type", _card_kind(metadata))
    if not metadata.get("title"):
        heading = next(
            (ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("#")), ""
        )
        title = metadata.get("name") or metadata.get("day") or heading
        if title:
            metadata["title"] = str(title)
    if not metadata.get("description"):
        described = next((metadata[k] for k in _DESCRIPTION_KEYS if metadata.get(k)), "")
        description = " ".join(
            str(described or _first_prose(body) or metadata.get("title", "")).split()
        )[:200]
        if description:
            metadata["description"] = description
    metadata.setdefault(
        "generated", {"by": _GENERATED_BY, "at": datetime.now(UTC).date().isoformat()}
    )
    return metadata


def render_document(frontmatter: dict[str, Any], body: str) -> str:
    """Render and validate a typed OKF v0.2 markdown document."""
    metadata = _card_metadata(frontmatter, body)
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


__all__ = [
    "atomic_write_text",
    "card_files",
    "parse_document",
    "read_frontmatter",
    "render_document",
]
