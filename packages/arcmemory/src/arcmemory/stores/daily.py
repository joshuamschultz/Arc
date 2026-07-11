"""Daily-notes store — the curated daily rollup (markdown).

``memory/daily-log/YYYY-MM-DD.md`` is the human-readable, searchable **summary** of a
day: what was discussed, the people/places, decisions, and tasks — NOT a transcript.
The raw event stream lives in the episodic SQLite table (+ the audit log) and is never
duplicated here; consolidation's distiller condenses it into these bullets.

Writes are additive across a day: a later consolidation of the same day merges its new
bullets into the existing file (union, dedup) rather than clobbering it, so the notes
grow as the day goes. The frontmatter ``classification`` is the dominating label of the
day's events, so this file channel is gated exactly like the raw stream (SDD §8).
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.security import dominating_classification
from arcmemory.types import DaySummary, Event

# Section heading <-> DaySummary field, in render order. The heading is the on-disk
# contract the parser reads back, so keep the two directions in lockstep.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Timeline", "timeline"),
    ("Discussions", "discussions"),
    ("Decisions", "decisions"),
    ("People & Places", "people"),
    ("Goals", "goals"),
    ("Tasks", "tasks"),
)
_HEADING_TO_FIELD = {heading: field for heading, field in _SECTIONS}


class DailyNotesStore:
    """Read/merge/write the curated daily-notes file for one scope."""

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace) / "memory" / "daily-log"

    def path_for(self, day: str) -> Path:
        """Absolute path to a day's curated notes file."""
        return self._dir / f"{day}.md"

    def read(self, day: str) -> DaySummary | None:
        """Parse the curated notes for ``day`` back into a ``DaySummary`` (None if absent)."""
        path = self.path_for(day)
        if not path.exists():
            return None
        fm, body = parse_document(path.read_text(encoding="utf-8"))
        fields = _parse_sections(body)
        return DaySummary(
            day=day,
            classification=str(fm.get("classification") or "unclassified"),
            **fields,
        )

    def merge(self, additions: DaySummary, events: list[Event]) -> DaySummary | None:
        """Union ``additions`` into the day's existing notes and write; None if nothing to write.

        The classification is recomputed as the dominating label of the prior file and
        this window's events, so appending a classified day can only raise the label.
        """
        existing = self.read(additions.day)
        merged = DaySummary(day=additions.day)
        for _, field in _SECTIONS:
            prior = getattr(existing, field) if existing else []
            setattr(merged, field, _union(prior, getattr(additions, field)))
        if merged.is_empty():
            return None
        labels = [e.classification for e in events]
        if existing is not None:
            labels.append(existing.classification)
        merged.classification = dominating_classification(labels)
        self.write(merged)
        return merged

    def write(self, summary: DaySummary) -> Path:
        """Render a ``DaySummary`` to markdown and atomically write it."""
        path = self.path_for(summary.day)
        frontmatter = {"day": summary.day, "classification": summary.classification}
        atomic_write_text(path, render_document(frontmatter, _render_body(summary)))
        return path


def _union(existing: list[str], new: list[str]) -> list[str]:
    """Union two bullet lists preserving first-seen order (stable, dedup'd)."""
    merged = list(existing)
    for item in new:
        cleaned = item.strip()
        if cleaned and cleaned not in merged:
            merged.append(cleaned)
    return merged


def _render_body(summary: DaySummary) -> str:
    """Render ``# day`` + one bulleted section per non-empty category (fixed order)."""
    parts = [f"# {summary.day}"]
    for heading, field in _SECTIONS:
        bullets = getattr(summary, field)
        if bullets:
            lines = "\n".join(f"- {b}" for b in bullets)
            parts.append(f"## {heading}\n{lines}")
    return "\n\n".join(parts)


def _parse_sections(body: str) -> dict[str, list[str]]:
    """Read ``## Heading`` / ``- bullet`` blocks back into the DaySummary field lists."""
    fields: dict[str, list[str]] = {field: [] for _, field in _SECTIONS}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = _HEADING_TO_FIELD.get(line[3:].strip())
        elif line.startswith("- ") and current is not None:
            fields[current].append(line[2:].strip())
    return fields


__all__ = ["DailyNotesStore"]
