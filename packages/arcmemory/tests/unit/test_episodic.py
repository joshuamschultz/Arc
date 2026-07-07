"""T-021 — episodic store: raw row + daily-log bullet, ordering preserved."""

from __future__ import annotations

from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event


def _event(i: int) -> Event:
    return Event(
        event_id=f"e{i}",
        ts=f"2026-07-07T00:00:0{i}+00:00",
        scope="did:a",
        kind="k",
        text=f"line {i}",
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

    bullets = (workspace / "memory" / "daily-log" / "2026-07-07.md").read_text().splitlines()
    assert bullets == [
        "- 2026-07-07T00:00:00+00:00 [k] line 0",
        "- 2026-07-07T00:00:01+00:00 [k] line 1",
        "- 2026-07-07T00:00:02+00:00 [k] line 2",
    ]
