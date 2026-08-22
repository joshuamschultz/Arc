"""Timeline reader — 'what changed over a period' (SPEC-072 COMP-009).

Answers "what happened or changed" by reading the ALREADY-STORED, timestamped stores —
the life-event cards (:class:`~arcmemory.stores.events.EventStore`) and the curated
daily log (:class:`~arcmemory.stores.daily.DailyNotesStore`) — for a time window and
returning the changes in chronological order. It adds **no new store**: the timeline is
just a chronological view over stores memory already keeps (REQ-359).

Safe by construction: every entry is classification-gated with the same no-read-up
predicate recall uses (``readable_within``), so a classified change never reaches a
reader whose clearance does not dominate it (REQ-362). Degrade-don't-crash: absent
stores yield an empty timeline, never an error — deterministic, no LLM/embedder.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from arcmemory.security import readable_within
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.events import EventStore
from arcmemory.types import TimeWindow


class TimelineEntry(BaseModel):
    """One dated change on the timeline: WHEN it happened, its source kind, and a summary."""

    date: str  # YYYY-MM-DD
    kind: str  # "event" | "decision"
    summary: str


def read_timeline(
    workspace: Path | str,
    *,
    window: TimeWindow | None = None,
    clearance: str = "unclassified",
) -> list[TimelineEntry]:
    """Dated changes from the life-event + daily stores, chronological and gated.

    ``window`` (optional) restricts entries to a time slice by their date; ``None`` reads
    the whole history. ``clearance`` gates each entry no-read-up (default unclassified, so
    the reader fails safe). Returns ``[]`` when nothing falls in the window or the stores
    do not exist yet — never raises.
    """
    root = Path(workspace)
    entries: list[TimelineEntry] = []
    entries.extend(_life_events(root, window, clearance))
    entries.extend(_daily_decisions(root, window, clearance))
    entries.sort(key=lambda e: (e.date, e.kind, e.summary))
    return entries


def _life_events(root: Path, window: TimeWindow | None, clearance: str) -> list[TimelineEntry]:
    """Life-event cards that fall in the window and the clearance may read."""
    store = EventStore(root)
    out: list[TimelineEntry] = []
    for slug in store.slugs():
        event = store.read(slug)
        if event is None or not event.date:
            continue
        if window is not None and not window.contains(event.date):
            continue
        if not readable_within(clearance, event.classification):
            continue
        detail = event.outcome or event.summary
        summary = f"{event.title}: {detail}" if detail else event.title
        out.append(TimelineEntry(date=event.date, kind="event", summary=summary))
    return out


def _daily_decisions(root: Path, window: TimeWindow | None, clearance: str) -> list[TimelineEntry]:
    """Curated daily-log decisions that fall in the window and the clearance may read."""
    store = DailyNotesStore(root)
    out: list[TimelineEntry] = []
    for day in store.days():
        if window is not None and not window.contains(day):
            continue
        summary = store.read(day)
        if summary is None or not readable_within(clearance, summary.classification):
            continue
        for decision in summary.decisions:
            out.append(TimelineEntry(date=day, kind="decision", summary=decision))
    return out


__all__ = ["TimelineEntry", "read_timeline"]
