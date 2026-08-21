"""RED (T-984, SPEC-071) — ``_emit_recall_attribution`` must carry the trigger kind.

COMP-009 / REQ-347: proactive recall (``on_moment``) attributes WHICH detector fired
a recall, not just that one happened. The explicit ``recall``/``retrieve`` path must
stay unchanged — no ``trigger`` on that path. Today's signature is
``_emit_recall_attribution(self, recalls: list[Recall]) -> None`` with no ``trigger``
parameter at all, so passing one is a ``TypeError`` — the right RED reason (the
feature is absent, not a typo or import error).
"""

from __future__ import annotations

from pathlib import Path

from arctrust.audit import AuditEvent

from arcmemory.brain import ArcMemoryBrain
from arcmemory.types import Recall

_DID = "did:arc:trigger-test-agent"

#: A source shape ``attributed_cards`` credits: ``file:memory/<slug>.md``.
_CREDITED_SOURCE = "file:memory/entities/alice.md"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _credited_recall() -> Recall:
    """A ``Recall`` whose ``source`` yields a non-empty ``attributed_cards`` result."""
    return Recall(
        source=_CREDITED_SOURCE,
        content="Alice is the engineering lead.",
        score=1.0,
        kind="surface",
    )


def test_emit_recall_attribution_with_trigger_includes_trigger_in_extra(
    workspace: Path,
) -> None:
    """A proactive recall passes ``trigger=<kind>``; the emitted event must carry it."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    brain._emit_recall_attribution([_credited_recall()], trigger="entity_seen")

    attributed = [e for e in sink.events if e.action == "memory.recall_attributed"]
    assert len(attributed) == 1
    event = attributed[0]
    assert event.extra["trigger"] == "entity_seen"
    assert event.extra["cards"]


def test_emit_recall_attribution_without_trigger_omits_trigger(workspace: Path) -> None:
    """The explicit recall/retrieve path (no trigger arg) stays unchanged."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    brain._emit_recall_attribution([_credited_recall()])

    attributed = [e for e in sink.events if e.action == "memory.recall_attributed"]
    assert len(attributed) == 1
    event = attributed[0]
    assert event.extra.get("trigger") is None


def test_emit_recall_attribution_empty_recalls_emits_nothing(workspace: Path) -> None:
    """No credited card -> no event, trigger or not (current behavior preserved)."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    brain._emit_recall_attribution([], trigger="task_start")

    attributed = [e for e in sink.events if e.action == "memory.recall_attributed"]
    assert attributed == []
