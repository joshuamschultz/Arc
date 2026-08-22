"""SPEC-073 Phase D1 — the recall-attribution audit event carries the active run id.

A recalled card is only useful to a run-scoped UI (Phase D2/D3) if the event
that names it also names WHICH run it happened in. ``arcstore.spool``'s
``request_context``/``current_request_id`` already exist for exactly this
correlation (arcrun binds it around a dispatch); ``ArcMemoryBrain`` must read
it when it stamps ``memory.recall_attributed``.

RED reason: ``ArcMemoryBrain._emit_recall_attribution`` does not yet read
``current_request_id()``, so the emitted event's ``request_id`` is always
``None`` — including inside an active ``request_context`` — and the first
assertion below (``== "run-abc"``) fails.
"""

from __future__ import annotations

from pathlib import Path

from arcstore.spool import request_context
from arctrust.audit import AuditEvent

from arcmemory.brain import ArcMemoryBrain

_DID = "did:arc:test-agent"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _write_entity(workspace: Path, slug: str, classification: str, body: str) -> None:
    """Same seeding shape as ``test_wired_brain.py`` — a recallable curated card."""
    path = workspace / "memory" / "entities" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    label = f"classification: {classification}\n" if classification else ""
    path.write_text(f"---\ntype: entity\nslug: {slug}\n{label}---\n\n{body}\n", encoding="utf-8")


def _attributed_events(sink: RecordingSink) -> list[AuditEvent]:
    return [e for e in sink.events if e.action == "memory.recall_attributed"]


async def test_recall_attribution_carries_the_active_run_id(workspace: Path) -> None:
    """Inside ``request_context("run-abc")``, the attribution event is stamped."""
    _write_entity(workspace, "public-note", "unclassified", "the widget shipping cadence")
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    with request_context("run-abc"):
        text = await brain.retrieve("widget", clearance="unclassified", top_k=5, budget=10_000)

    assert "shipping cadence" in text, "setup broken: the seeded card never surfaced"
    attributed = _attributed_events(sink)
    assert attributed, "no memory.recall_attributed event was emitted for the surfaced card"
    assert attributed[0].request_id == "run-abc"


async def test_recall_attribution_has_no_request_id_outside_a_run(workspace: Path) -> None:
    """Outside any ``request_context``, the same event carries ``request_id=None``."""
    _write_entity(workspace, "public-note", "unclassified", "the widget shipping cadence")
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    text = await brain.retrieve("widget", clearance="unclassified", top_k=5, budget=10_000)

    assert "shipping cadence" in text, "setup broken: the seeded card never surfaced"
    attributed = _attributed_events(sink)
    assert attributed, "no memory.recall_attributed event was emitted for the surfaced card"
    assert attributed[0].request_id is None
