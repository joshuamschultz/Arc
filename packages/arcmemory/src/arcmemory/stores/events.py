"""Events store — what HAPPENED in the user's life, one glass-box card per occurrence.

``memory/events/<slug>.md`` records that a thing OCCURRED: a meeting, a sale, a call, a
shipment. Frontmatter carries the structured handles a timeline query needs — the date
it happened (distinct from ``recorded``, the day it was written down), its type, and the
``[[participants]]`` that make it hoppable to the people/projects involved — and the body
carries the prose (what happened, how it came out).

This is the *user's* timeline, deliberately separate from the daily notes (what the
*agent* did) and from procedures (how *we* do things). Without it "a meeting happened"
survived only as loose text in the raw stream, so "what happened with this client this
quarter" had no card to answer from.

Re-extracting an event REFRESHES it in place: an occurrence happens once, so ``upsert``
overwrites the prose with a better telling, unions the participants (non-lossy), keeps
the original ``recorded`` date, and can only ever RAISE the classification.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.security import dominating_classification
from arcmemory.slug import canonical_slug
from arcmemory.types import LifeEvent, utc_today

# Body section heading <-> LifeEvent field, in render order. The heading is the on-disk
# contract the parser reads back, so keep the two directions in lockstep.
_SECTIONS: tuple[tuple[str, str], ...] = (("Summary", "summary"), ("Outcome", "outcome"))
_HEADING_TO_FIELD = {heading: field for heading, field in _SECTIONS}


def participant_slug(raw: str) -> str:
    """Canonical entity slug for a participant, accepting ``[[slug]]`` or a bare slug."""
    return canonical_slug(raw.strip().removeprefix("[[").removesuffix("]]"))


class EventStore:
    """Read/write life-event cards for one scope."""

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace) / "memory" / "events"

    def path_for(self, slug: str) -> Path:
        """Absolute path to an event card (slug canonicalized)."""
        return self._dir / f"{canonical_slug(slug)}.md"

    def write(self, event: LifeEvent) -> Path:
        """Render an event to markdown and atomically write it."""
        frontmatter = {
            "slug": event.slug,
            "title": event.title,
            "date": event.date,
            "recorded": event.recorded,
            "event_type": event.event_type,
            "participants": [f"[[{p}]]" for p in event.participants],
            "classification": event.classification,
        }
        path = self.path_for(event.slug)
        atomic_write_text(path, render_document(frontmatter, _render_body(event)))
        return path

    def upsert(
        self,
        slug: str,
        title: str,
        *,
        date: str = "",
        event_type: str = "unknown",
        participants: list[str] | None = None,
        summary: str = "",
        outcome: str = "",
        classification: str = "unclassified",
    ) -> LifeEvent:
        """Create or refresh an event card; participants union, classification only rises."""
        slug = canonical_slug(slug)
        existing = self.read(slug)
        event = LifeEvent(
            slug=slug,
            title=title or (existing.title if existing else slug),
            date=date or (existing.date if existing else ""),
            recorded=existing.recorded if existing else utc_today(),
            event_type=(
                event_type
                if event_type != "unknown"
                else (existing.event_type if existing else "unknown")
            ),
            participants=_merge_participants(
                existing.participants if existing else [], participants or []
            ),
            summary=summary or (existing.summary if existing else ""),
            outcome=outcome or (existing.outcome if existing else ""),
            classification=dominating_classification(
                ([existing.classification] if existing else []) + [classification]
            ),
        )
        self.write(event)
        return event

    def read(self, slug: str) -> LifeEvent | None:
        """Load an event card (None if absent)."""
        slug = canonical_slug(slug)
        path = self.path_for(slug)
        if not path.exists():
            return None
        fm, body = parse_document(path.read_text(encoding="utf-8"))
        sections = _parse_sections(body)
        return LifeEvent(
            slug=str(fm.get("slug", slug)),
            title=str(fm.get("title", slug.replace("-", " ").title())),
            date=str(fm.get("date", "")),
            recorded=str(fm.get("recorded", "")),
            event_type=str(fm.get("event_type", "unknown")),
            participants=[participant_slug(str(p)) for p in fm.get("participants", [])],
            summary=sections["summary"],
            outcome=sections["outcome"],
            classification=str(fm.get("classification", "unclassified")),
        )

    def slugs(self) -> list[str]:
        """Every event slug currently on disk (sorted)."""
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.md"))


def _merge_participants(existing: list[str], new: list[str]) -> list[str]:
    """Union two participant lists as canonical slugs, preserving first-seen order."""
    merged: list[str] = []
    for raw in [*existing, *new]:
        slug = participant_slug(raw)
        if slug and slug not in merged:
            merged.append(slug)
    return merged


def _render_body(event: LifeEvent) -> str:
    """Render ``# title`` + one prose section per non-empty field (fixed order)."""
    parts = [f"# {event.title}"]
    for heading, field in _SECTIONS:
        text = getattr(event, field)
        if text:
            parts.append(f"## {heading}\n{text}")
    return "\n\n".join(parts)


def _parse_sections(body: str) -> dict[str, str]:
    """Read the ``## Heading`` prose blocks back into the LifeEvent fields."""
    fields = {field: "" for _, field in _SECTIONS}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = _HEADING_TO_FIELD.get(line[3:].strip())
        elif line and current is not None:
            fields[current] = f"{fields[current]}\n{line}".strip()
    return fields


__all__ = ["EventStore", "participant_slug"]
