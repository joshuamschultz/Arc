"""T-031 — deterministic capture: zero-LLM (spy), writes event+bullet+edges."""

from __future__ import annotations

from pathlib import Path

import arcllm
from arctrust.audit import AuditEvent

from arcmemory.capture import FastCapture
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.types import Scope


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _capture(workspace: Path, db: MemoryDB, scope: Scope, sink=None) -> FastCapture:
    return FastCapture(
        db,
        workspace,
        scope,
        WeightedGraph(db),
        audit_sink=sink,
        seed_vocabulary=["alice", "bob"],
    )


def test_capture_makes_no_arcllm_call(monkeypatch, workspace, db, scope) -> None:
    calls: list[object] = []

    async def spy_embed(*args, **kwargs):  # pragma: no cover - must never run
        calls.append((args, kwargs))
        raise AssertionError("capture must not call arcllm.embed")

    monkeypatch.setattr(arcllm, "embed", spy_embed)

    cap = _capture(workspace, db, scope)
    cap.capture("alice met bob to plan the release")

    assert calls == []  # the hot path issued zero embedding calls


def test_capture_writes_event_bullet_and_edge(workspace, db, scope) -> None:
    graph = WeightedGraph(db)
    cap = FastCapture(db, workspace, scope, graph, seed_vocabulary=["alice", "bob"])
    event = cap.capture("alice and bob shipped the feature")

    assert event is not None
    assert db.connect().execute("SELECT COUNT(*) FROM episodic").fetchone()[0] == 1
    daily = workspace / "memory" / "daily-log" / f"{event.ts[:10]}.md"
    assert daily.exists() and "alice and bob" in daily.read_text()
    assert graph.weight(scope.key, "alice", "bob") > 0.0  # co-active Hebbian edge


def test_capture_dedups_within_window(workspace, db, scope) -> None:
    cap = _capture(workspace, db, scope)
    assert cap.capture("alice and bob shipped") is not None
    assert cap.capture("alice and bob shipped") is None  # duplicate suppressed


def test_capture_emits_memory_captured_audit(workspace, db, scope) -> None:
    sink = RecordingSink()
    cap = _capture(workspace, db, scope, sink=sink)
    cap.capture("alice and bob shipped")

    assert len(sink.events) == 1
    assert sink.events[0].action == "memory.captured"
    assert sink.events[0].actor_did == scope.agent_did
