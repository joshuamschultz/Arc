"""Tests for §6: migrated audit emits use arctrust.audit.emit(AuditEvent).

Verifies that core components emit structured AuditEvents via arctrust,
not ad-hoc log strings.
"""

from __future__ import annotations

from unittest.mock import MagicMock

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


class TestMemoryACLEmitsAuditEvents:
    """The memory_acl veto hook must emit structured audit events."""

    async def test_acl_veto_emits_structured_audit(self) -> None:
        from arcagent.core.module_bus import EventContext
        from arcagent.modules.memory_acl import _runtime
        from arcagent.modules.memory_acl.capabilities import memory_acl_read

        _runtime.reset()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()
        _runtime.configure(config={"tier": "personal"}, telemetry=telemetry)
        try:
            # A cross-user read the ACL denies must veto and audit.
            ctx = EventContext(
                event="memory.read",
                data={
                    "caller_did": "did:arc:testorg:executor/other",
                    "target_user_did": "did:arc:testorg:user/owner",
                },
                agent_did="did:arc:testorg:agent/agent1",
                trace_id="trace-test",
            )

            await memory_acl_read(ctx)

            assert ctx.is_vetoed
            assert telemetry.audit_event.called
            assert telemetry.audit_event.call_args[0][0] == "session.acl.veto"
        finally:
            _runtime.reset()
