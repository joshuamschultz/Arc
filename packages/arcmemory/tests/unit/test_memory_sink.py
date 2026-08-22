"""SPEC-073 COMP-004 (T-1026/T-1027) — MemorySink routes a SourceRecord through
the existing FastCapture path so it becomes recallable via the normal
Retriever/SurfaceIndex channel.

RED: ``arcmemory.sinks`` does not exist yet — every test fails on import
(ModuleNotFoundError), a feature-absent reason, not a typo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.capture import FastCapture
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.surface import SurfaceIndex
from arcmemory.sinks import MemorySink
from arcmemory.types import Scope, SourceRecord


def _sink(db: MemoryDB, workspace: Path, scope: Scope) -> MemorySink:
    graph = WeightedGraph(db)
    capture = FastCapture(db, workspace, scope, graph)
    return MemorySink(capture)


async def test_routed_record_is_recallable_through_a_fresh_surface_index(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    sink = _sink(db, workspace, scope)
    record = SourceRecord(
        external_id="msg-1", text="the quarterly report is due Friday", kind="observation"
    )

    await sink("source-a", [record])

    surface = SurfaceIndex(db, workspace, scope, embedder=None)
    await surface.index_if_needed()
    result = await surface.search("quarterly report", top_k=3)
    assert any("quarterly report" in r.content for r in result.recalls)


async def test_routes_multiple_records_in_one_call_as_separate_events(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    sink = _sink(db, workspace, scope)
    records = [
        SourceRecord(external_id="a", text="first distinct record text"),
        SourceRecord(external_id="b", text="second distinct record text"),
    ]

    await sink("source-a", records)

    conn = db.connect()
    count = conn.execute("SELECT COUNT(*) FROM episodic WHERE scope=?", (scope.key,)).fetchone()[0]
    assert count == 2


async def test_threads_kind_and_classification_through_to_capture(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    sink = _sink(db, workspace, scope)
    record = SourceRecord(
        external_id="msg-2",
        text="confidential merger details",
        kind="document",
        classification="secret",
    )

    await sink("source-a", [record])

    conn = db.connect()
    row = conn.execute(
        "SELECT kind, classification FROM episodic WHERE scope=?", (scope.key,)
    ).fetchone()
    assert row == ("document", "secret")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
