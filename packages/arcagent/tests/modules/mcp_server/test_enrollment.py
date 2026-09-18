"""SPEC-082 — the door's fleet-enrollment gate (`require_enrolled`).

Covers the configured-gate rule: a non-empty roster is enforced at ANY tier
(personal included), an empty roster is open only at personal, and
enterprise/federal always require enrollment (empty roster there denies all).
"""

from __future__ import annotations

import pytest
from arctrust import AuditEvent

from arcagent.modules.mcp_server.enrollment import require_enrolled
from arcagent.modules.mcp_server.identity import InboundRejected

_ENROLLED = "did:arc:local:client/aaaa1111"
_OTHER = "did:arc:local:client/bbbb2222"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_personal_with_no_roster_is_open() -> None:
    """Enrollment is optional at personal while the roster is empty — no raise."""
    sink = _RecordingSink()
    require_enrolled(_OTHER, None, tier="personal", audit_sink=sink)
    require_enrolled(_OTHER, [], tier="personal", audit_sink=sink)
    assert sink.events == []


def test_personal_with_roster_admits_an_enrolled_caller() -> None:
    """A configured roster admits a listed DID at personal."""
    sink = _RecordingSink()
    require_enrolled(_ENROLLED, [_ENROLLED], tier="personal", audit_sink=sink)
    assert sink.events == []


def test_personal_with_roster_refuses_an_unlisted_caller() -> None:
    """THE FIX: a non-empty roster is enforced at personal — an unlisted DID is refused."""
    sink = _RecordingSink()
    with pytest.raises(InboundRejected, match="not enrolled"):
        require_enrolled(_OTHER, [_ENROLLED], tier="personal", audit_sink=sink)
    assert len(sink.events) == 1
    assert sink.events[0].outcome == "deny"


def test_federal_empty_roster_denies_all() -> None:
    """Enterprise/federal mandate enrollment: an empty roster refuses every caller."""
    sink = _RecordingSink()
    with pytest.raises(InboundRejected):
        require_enrolled(_ENROLLED, [], tier="federal", audit_sink=sink)
    assert sink.events[0].outcome == "deny"


def test_federal_admits_an_enrolled_caller() -> None:
    sink = _RecordingSink()
    require_enrolled(_ENROLLED, [_ENROLLED], tier="federal", audit_sink=sink)
    assert sink.events == []
