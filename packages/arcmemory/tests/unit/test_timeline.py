"""RED — 'what changed' Timeline reader over events + daily log (SPEC-072 COMP-009).

Answers "what happened/changed over a period" by reading the ALREADY-STORED life-event
cards and curated daily log for a window and returning the changes in chronological
order (REQ-359). No new store. Empty (not an error) when nothing falls in the window;
classification-gated so a SECRET change never reaches an unclassified reader (REQ-362).
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.events import EventStore
from arcmemory.timeline import TimelineEntry, read_timeline
from arcmemory.types import DaySummary, TimeWindow


def _seed(workspace: Path) -> None:
    events = EventStore(workspace)
    events.upsert("acme-signed", "Acme signed", date="2026-08-10", outcome="closed won")
    events.upsert("beta-launch", "Beta launch", date="2026-08-18", outcome="shipped")
    events.upsert("ancient", "Ancient event", date="2020-01-01", outcome="old")
    daily = DailyNotesStore(workspace)
    daily.write(DaySummary(day="2026-08-15", decisions=["chose Postgres over SQLite"]))


def test_read_timeline_returns_dated_changes_in_chronological_order(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _seed(workspace)

    window = TimeWindow(start="2026-08-01", end="2026-08-31")
    entries = read_timeline(workspace, window=window)

    assert entries, "expected timeline entries in the window"
    assert all(isinstance(e, TimelineEntry) for e in entries)
    dates = [e.date for e in entries]
    assert dates == sorted(dates), "entries must be chronological"
    # The out-of-window ancient event is excluded.
    assert all(e.date >= "2026-08-01" for e in entries)
    blob = " ".join(f"{e.date} {e.summary}" for e in entries)
    assert "Acme signed" in blob
    assert "Beta launch" in blob
    assert "Postgres" in blob  # a daily decision surfaces as a dated change


def test_read_timeline_is_empty_not_error_when_window_has_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _seed(workspace)
    empty = TimeWindow(start="1999-01-01", end="1999-12-31")
    assert read_timeline(workspace, window=empty) == []


def test_read_timeline_degrades_when_stores_absent(tmp_path: Path) -> None:
    """No events/daily dirs at all → empty list, never an error."""
    assert read_timeline(tmp_path / "empty-ws") == []


def test_read_timeline_gates_classified_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    events = EventStore(workspace)
    events.upsert("op-eclipse", "Op Eclipse", date="2026-08-12", outcome="done", classification="SECRET")

    entries = read_timeline(workspace, clearance="unclassified")
    blob = " ".join(e.summary for e in entries)
    assert "Eclipse" not in blob
