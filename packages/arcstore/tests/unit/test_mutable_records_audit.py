"""Audit emission on the mutable plane (SPEC-056 Phase 0A, task 0A4) — RED.

Every mutable write (``mutable_write``, ``mutable_delete``, ``update_if``)
must emit an ``arctrust.audit.AuditEvent`` carrying ``actor_did`` via
``arctrust.audit.emit()`` (NIST AU-2/AU-3). Verified against an in-memory
sink so no real WORM file is needed for these unit tests.

None of the mutable-plane methods exist yet — every test fails with
``AttributeError`` (feature absent), not an import/syntax error.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.audit import AuditEvent

from arcstore.backends.sqlite import SqliteBackend

_ACTOR = "did:arc:test:exec/aabbccdd"


class _RecordingSink:
    """Minimal in-memory AuditSink — satisfies the ``write(event)`` Protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class TestMutableWriteAudit:
    async def test_write_emits_audit_event_with_actor_did(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        sink = _RecordingSink()
        try:
            await be.mutable_write("tasks", "t1", {"title": "x"}, actor_did=_ACTOR, sink=sink)
            assert len(sink.events) == 1
            assert sink.events[0].actor_did == _ACTOR
        finally:
            await be.stop()

    async def test_delete_emits_audit_event_with_actor_did(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        sink = _RecordingSink()
        try:
            await be.mutable_write("tasks", "t1", {"title": "x"}, actor_did=_ACTOR)
            await be.mutable_delete("tasks", "t1", actor_did=_ACTOR, sink=sink)
            assert len(sink.events) == 1
            assert sink.events[0].actor_did == _ACTOR
        finally:
            await be.stop()

    async def test_update_if_emits_audit_event_with_actor_did(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        sink = _RecordingSink()
        try:
            await be.mutable_write("tasks", "t1", {"owner": None}, actor_did=_ACTOR)
            await be.update_if(
                "tasks", "t1", {"owner": _ACTOR}, where={"owner": None},
                actor_did=_ACTOR, sink=sink,
            )
            assert len(sink.events) == 1
            assert sink.events[0].actor_did == _ACTOR
        finally:
            await be.stop()

    async def test_no_sink_means_no_audit_attempt(self, tmp_path: Path) -> None:
        """A None/omitted sink must not raise — audit is opt-in per call site."""
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"title": "x"}, actor_did=_ACTOR)
        finally:
            await be.stop()

    async def test_write_emits_action_naming_the_write(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        sink = _RecordingSink()
        try:
            await be.mutable_write("tasks", "t1", {"title": "x"}, actor_did=_ACTOR, sink=sink)
            assert "mutable" in sink.events[0].action
            assert sink.events[0].target == "tasks/t1"
        finally:
            await be.stop()
