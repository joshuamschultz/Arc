"""SPEC-083 — the agent's audit sink can write durably into its signed WORM chain.

Classifier promotions and the promotion egress record require ``write_durable``.
The agent's sink keeps ordinary events on the telemetry boundary and sends durable
events to the operator-signed chain FIRST (a failed append raises, so the caller
fails closed), then mirrors them to telemetry for observability.
"""

from __future__ import annotations

from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.telemetry import DurableTelemetryAuditSink


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))


class _Chain:
    def __init__(self, *, fail: bool = False) -> None:
        self.durable: list[AuditEvent] = []
        self._fail = fail

    def write_durable(self, event: AuditEvent) -> None:
        if self._fail:
            raise OSError("chain append failed")
        self.durable.append(event)


def _event() -> AuditEvent:
    return AuditEvent(
        actor_did="did:arc:a", action="knowledge.promotion_decision", target="x", outcome="allow"
    )


def test_durable_write_lands_in_the_chain_then_telemetry() -> None:
    telemetry, chain = _Telemetry(), _Chain()
    sink = DurableTelemetryAuditSink(telemetry, chain)

    sink.write_durable(_event())

    assert [e.action for e in chain.durable] == ["knowledge.promotion_decision"]
    assert [name for name, _ in telemetry.events] == ["knowledge.promotion_decision"]


def test_failed_chain_append_raises_and_is_not_reported_as_written() -> None:
    telemetry = _Telemetry()
    sink = DurableTelemetryAuditSink(telemetry, _Chain(fail=True))

    with pytest.raises(OSError, match="chain append failed"):
        sink.write_durable(_event())

    assert telemetry.events == []


def test_ordinary_write_stays_on_telemetry_only() -> None:
    telemetry, chain = _Telemetry(), _Chain()
    sink = DurableTelemetryAuditSink(telemetry, chain)

    sink.write(_event())

    assert chain.durable == []
    assert [name for name, _ in telemetry.events] == ["knowledge.promotion_decision"]
