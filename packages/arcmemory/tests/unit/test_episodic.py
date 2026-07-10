"""T-021 — episodic store: raw row + daily-log bullet, ordering preserved."""

from __future__ import annotations

from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.mdfile import parse_document
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event


def _event(i: int, *, classification: str = "unclassified") -> Event:
    return Event(
        event_id=f"e{i}",
        ts=f"2026-07-07T00:00:0{i}+00:00",
        scope="did:a",
        kind="k",
        text=f"line {i}",
        classification=classification,
    )


def test_append_persists_event_and_bullet(workspace: Path, db: MemoryDB) -> None:
    store = EpisodicStore(db, workspace)
    ev = _event(1)
    store.append(ev)
    path = store.append_bullet(ev)

    assert (
        db.connect().execute("SELECT text FROM episodic WHERE event_id='e1'").fetchone()[0]
        == "line 1"
    )
    assert path == workspace / "memory" / "daily-log" / "2026-07-07.md"
    assert "[k] line 1" in path.read_text(encoding="utf-8")


def test_ordering_preserved(workspace: Path, db: MemoryDB) -> None:
    store = EpisodicStore(db, workspace)
    for i in range(3):
        ev = _event(i)
        store.append(ev)
        store.append_bullet(ev)

    events = store.events("did:a")
    assert [e.event_id for e in events] == ["e0", "e1", "e2"]

    fm, body = parse_document(
        (workspace / "memory" / "daily-log" / "2026-07-07.md").read_text(encoding="utf-8")
    )
    assert body.splitlines() == [
        "- 2026-07-07T00:00:00+00:00 [k] line 0",
        "- 2026-07-07T00:00:01+00:00 [k] line 1",
        "- 2026-07-07T00:00:02+00:00 [k] line 2",
    ]
    assert fm["classification"] == "unclassified"


def test_append_persists_salience_and_entities(workspace: Path, db: MemoryDB) -> None:
    """The row round-trips the full Event (salience + tagged entities), not a subset."""
    store = EpisodicStore(db, workspace)
    ev = Event(
        event_id="e9",
        ts="2026-07-07T00:00:00+00:00",
        scope="did:a",
        kind="k",
        text="alice met bob",
        salience=0.75,
        entities=["alice", "bob"],
    )
    store.append(ev)

    (loaded,) = store.events("did:a")
    assert loaded.salience == 0.75
    assert loaded.entities == ["alice", "bob"]


def test_page_count_get(workspace: Path, db: MemoryDB) -> None:
    store = EpisodicStore(db, workspace)
    for i in range(3):
        store.append(_event(i))

    assert store.count("did:a") == 3
    newest_first = store.page("did:a", limit=2, offset=0)
    assert [e.event_id for e in newest_first] == ["e2", "e1"]  # seq DESC
    assert [e.event_id for e in store.page("did:a", limit=2, offset=2)] == ["e0"]
    assert store.get("did:a", "e1") is not None
    assert store.get("did:a", "missing") is None


def test_update_and_delete_report_affected(workspace: Path, db: MemoryDB) -> None:
    store = EpisodicStore(db, workspace)
    store.append(_event(1))

    assert store.update_text("did:a", "e1", "corrected") is True
    assert store.get("did:a", "e1").text == "corrected"
    assert store.update_salience("did:a", "e1", 0.9) is True
    assert store.get("did:a", "e1").salience == 0.9
    assert store.delete("did:a", "e1") is True
    assert store.get("did:a", "e1") is None

    # A no-op on a missing id reports False (never a silent success).
    assert store.update_text("did:a", "gone", "x") is False
    assert store.update_salience("did:a", "gone", 0.1) is False
    assert store.delete("did:a", "gone") is False


def test_daily_log_stamps_dominating_classification(workspace: Path, db: MemoryDB) -> None:
    """A SECRET bullet raises the whole day-file's label so the file channel is gated."""
    store = EpisodicStore(db, workspace)
    store.append_bullet(_event(0, classification="unclassified"))
    store.append_bullet(_event(1, classification="SECRET"))

    fm, body = parse_document(
        (workspace / "memory" / "daily-log" / "2026-07-07.md").read_text(encoding="utf-8")
    )
    assert fm["classification"] == "SECRET"  # dominating label of the day's bullets
    assert "[k] line 0" in body and "[k] line 1" in body  # both bullets retained
