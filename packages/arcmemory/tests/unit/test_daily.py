"""DailyNotesStore — the curated daily rollup: render, round-trip, additive merge, gating."""

from __future__ import annotations

from pathlib import Path

from arcmemory.mdfile import parse_document
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.types import DaySummary, Event

_DAY = "2026-07-07"


def _event(i: int, *, classification: str = "unclassified") -> Event:
    return Event(
        event_id=f"e{i}",
        ts=f"{_DAY}T00:00:0{i}+00:00",
        scope="did:a",
        kind="respond",
        text=f"line {i}",
        classification=classification,
    )


def test_write_renders_categorized_bullets(workspace: Path) -> None:
    store = DailyNotesStore(workspace)
    path = store.write(
        DaySummary(
            day=_DAY,
            summary=["discussed the release"],
            people=["Alice", "Grand Junction"],
            decisions=["ship Friday"],
            tasks=["email Bob"],
        )
    )
    body = path.read_text(encoding="utf-8")
    assert path == workspace / "memory" / "daily-log" / f"{_DAY}.md"
    assert "## Summary\n- discussed the release" in body
    assert "## People & Places\n- Alice\n- Grand Junction" in body
    assert "## Decisions\n- ship Friday" in body
    assert "## Tasks\n- email Bob" in body


def test_round_trip_read_matches_written(workspace: Path) -> None:
    store = DailyNotesStore(workspace)
    written = DaySummary(
        day=_DAY, summary=["a", "b"], people=["Alice"], decisions=["x"], tasks=["y"]
    )
    store.write(written)
    loaded = store.read(_DAY)
    assert loaded is not None
    assert loaded.summary == ["a", "b"]
    assert loaded.people == ["Alice"]
    assert loaded.decisions == ["x"]
    assert loaded.tasks == ["y"]


def test_merge_is_additive_and_deduped(workspace: Path) -> None:
    """A second run of the same day grows the notes (union) rather than clobbering."""
    store = DailyNotesStore(workspace)
    store.merge(DaySummary(day=_DAY, summary=["first"], people=["Alice"]), [_event(0)])
    merged = store.merge(
        DaySummary(day=_DAY, summary=["first", "second"], people=["Bob"]), [_event(1)]
    )
    assert merged is not None
    assert merged.summary == ["first", "second"]  # "first" not duplicated
    assert merged.people == ["Alice", "Bob"]


def test_merge_empty_writes_nothing(workspace: Path) -> None:
    store = DailyNotesStore(workspace)
    assert store.merge(DaySummary(day=_DAY), [_event(0)]) is None
    assert not store.path_for(_DAY).exists()


def test_merge_raises_classification_to_dominating(workspace: Path) -> None:
    """A SECRET event in the day raises the file's label so the channel is gated (SDD §8)."""
    store = DailyNotesStore(workspace)
    store.merge(DaySummary(day=_DAY, summary=["public note"]), [_event(0)])
    store.merge(
        DaySummary(day=_DAY, decisions=["classified call"]),
        [_event(1, classification="SECRET")],
    )
    fm, _ = parse_document(store.path_for(_DAY).read_text(encoding="utf-8"))
    assert fm["classification"] == "SECRET"
