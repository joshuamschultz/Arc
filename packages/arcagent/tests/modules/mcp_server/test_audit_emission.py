"""SPEC-082 T-1085 (RED) — every door op is audited, with a payload hash not raw args.

REQ-416 / COMP-001, COMP-003. Every inbound door operation — allow AND deny — emits
exactly one :class:`~arctrust.AuditEvent` carrying the caller DID, the verb
(``action="mcp.<verb>"``), the outcome, the tier, and a *hash* of the payload. Raw
argument values must never appear in the event (LLM02/LLM07). Sink selection is
tiered: a ``WormSink`` at federal, a ``NullSink`` at personal.

This test drives ``arcagent.modules.mcp_server.audit`` (``emit_door_event`` +
``select_sink``), which does not exist yet. The RED is the import: ``No module named
'arcagent.modules.mcp_server.audit'``. It goes GREEN when T-1086 adds the module.
"""

from __future__ import annotations

from pathlib import Path

from arctrust import AuditEvent, InProcessSigner, NullSink, WormSink

from arcagent.modules.mcp_server.audit import emit_door_event, select_sink

_CALLER = "did:arc:acme:exec/deadbeef"
_SECRET_ARG = "SUPER-SECRET-TOKEN-do-not-log"


class _RecordingSink:
    """An ``arctrust`` ``AuditSink`` that keeps every event for assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_allow_and_deny_each_emit_one_event_with_the_required_fields() -> None:
    """Both an allow and a deny emit exactly one event carrying DID/verb/outcome/tier."""
    sink = _RecordingSink()

    emit_door_event(
        sink,
        caller_did=_CALLER,
        verb="tools/call",
        outcome="allow",
        tier="personal",
        arguments={"path": "/etc/data"},
    )
    emit_door_event(
        sink,
        caller_did=_CALLER,
        verb="tools/call",
        outcome="deny",
        tier="personal",
        arguments={"path": "/etc/data"},
    )

    assert len(sink.events) == 2
    allow, deny = sink.events
    for event in (allow, deny):
        assert event.action == "mcp.tools/call"
        assert event.actor_did == _CALLER
        assert event.tier == "personal"
        assert event.payload_hash
    assert allow.outcome == "allow"
    assert deny.outcome == "deny"


def test_raw_argument_values_never_appear_in_the_event() -> None:
    """Only a payload hash is recorded — never the raw argument text (REQ-416)."""
    sink = _RecordingSink()

    emit_door_event(
        sink,
        caller_did=_CALLER,
        verb="tools/call",
        outcome="allow",
        tier="personal",
        arguments={"token": _SECRET_ARG},
    )

    event = sink.events[0]
    assert event.payload_hash
    assert _SECRET_ARG not in event.model_dump_json()


def test_sink_selection_is_worm_at_federal_and_null_at_personal(tmp_path: Path) -> None:
    """Federal routes to a tamper-evident ``WormSink``; personal to a ``NullSink``."""
    federal_sink = select_sink(
        "federal",
        worm_path=tmp_path / "door_audit.worm",
        signer=InProcessSigner(seed=b"\x00" * 32),
    )
    personal_sink = select_sink("personal")

    assert isinstance(federal_sink, WormSink)
    assert isinstance(personal_sink, NullSink)
