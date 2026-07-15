"""Tests for §6: migrated audit emits use arctrust.audit.emit(AuditEvent).

Verifies that core components emit structured AuditEvents via arctrust,
not ad-hoc log strings.
"""

from __future__ import annotations

from arctrust import AuditEvent, NullSink, emit


class TestAuditEventShape:
    """AuditEvent emitted by core components must have correct shape."""

    def test_audit_event_has_required_fields(self) -> None:
        event = AuditEvent(
            actor_did="did:arc:testorg:executor/abc",
            action="tool.executed",
            target="echo_tool",
            outcome="allow",
            tier="personal",
        )
        assert event.actor_did == "did:arc:testorg:executor/abc"
        assert event.action == "tool.executed"
        assert event.target == "echo_tool"
        assert event.outcome == "allow"
        assert event.tier == "personal"
        assert event.ts is not None

    def test_null_sink_accepts_events(self) -> None:
        sink = NullSink()
        event = AuditEvent(
            actor_did="did:arc:testorg:executor/abc",
            action="agent.startup",
            target="agent",
            outcome="allow",
        )
        emit(event, sink)  # Must not raise

    def test_emit_swallows_sink_failure(self) -> None:
        class BadSink:
            def write(self, event: AuditEvent) -> None:
                raise RuntimeError("sink exploded")

        event = AuditEvent(
            actor_did="did:arc:testorg:executor/abc",
            action="agent.startup",
            target="agent",
            outcome="allow",
        )
        # emit() must not propagate the sink error
        emit(event, BadSink())


class TestTelemetryEmitsStructuredEvents:
    """AgentTelemetry.audit_event must produce structured log entries."""

    def test_telemetry_audit_event_logs_structured_json(self) -> None:
        import json
        import logging

        from arcagent.core.config import TelemetryConfig
        from arcagent.core.telemetry import AgentTelemetry

        config = TelemetryConfig(enabled=False, log_level="DEBUG")
        tel = AgentTelemetry(config=config, agent_did="did:arc:test/abc")

        log_records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = CapturingHandler()
        tel._audit_logger.addHandler(handler)

        tel.audit_event("agent.startup", {"component": "agent"})

        assert len(log_records) >= 1
        parsed = json.loads(log_records[0].getMessage())
        assert parsed["event_type"] == "agent.startup"
        assert parsed["agent_did"] == "did:arc:test/abc"
        assert "details" in parsed

    def test_telemetry_redacts_sensitive_values(self) -> None:
        import json
        import logging

        from arcagent.core.config import TelemetryConfig
        from arcagent.core.telemetry import AgentTelemetry

        config = TelemetryConfig(enabled=False)
        tel = AgentTelemetry(config=config, agent_did="did:arc:test/abc")

        log_records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = CapturingHandler()
        tel._audit_logger.addHandler(handler)

        tel.audit_event("auth.check", {"api_key": "sk-secret-1234", "user": "alice"})

        assert len(log_records) >= 1
        parsed = json.loads(log_records[0].getMessage())
        # Sensitive values must be redacted
        assert parsed["details"]["api_key"] == "[REDACTED]"
        # Non-sensitive values must not be redacted
        assert parsed["details"]["user"] == "alice"


# The memory ACL veto/audit moved to arcmemory with the rest of the memory-visibility
# policy (arcagent no longer owns a memory_acl module); its audit behavior is verified in
# arcmemory's own test suite (test_acl.py::TestBrainAuthorize::test_denial_emits_structured_audit).
