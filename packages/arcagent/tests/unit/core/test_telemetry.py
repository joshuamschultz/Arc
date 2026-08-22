"""Tests for telemetry — OTel spans, structured logging, audit events."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from arcagent.core.config import TelemetryConfig
from arcagent.core.telemetry import AgentTelemetry, TelemetryAuditSink


@pytest.fixture(autouse=True)
def _otel_setup() -> Iterator[None]:
    """A fresh in-memory OTel provider per test, put back afterwards.

    The tracer provider is process-global and ``set_tracer_provider`` is
    guarded by a set-once latch: after the first call it logs "Overriding of
    current TracerProvider is not allowed" and does nothing. So this fixture
    gave the FIRST test a provider and every later one that same provider,
    never a fresh one — and never gave it back, so every later test in the
    process, in any package, inherited an SDK provider where the library
    default is a proxy.

    Assigning the module global is what makes both halves work: it sidesteps
    the latch, so each test really does get a clean provider, and it is the
    only way to put the previous one back. OTel exposes no public reset.
    """
    previous = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = TracerProvider()
    try:
        yield
    finally:
        trace._TRACER_PROVIDER = previous


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture()
def telemetry() -> AgentTelemetry:
    config = TelemetryConfig(enabled=True, log_level="DEBUG")
    return AgentTelemetry(config=config, agent_did="did:arc:test:executor/abcd1234")


@pytest.fixture()
def disabled_telemetry() -> AgentTelemetry:
    config = TelemetryConfig(enabled=False)
    return AgentTelemetry(config=config, agent_did="did:arc:test:executor/abcd1234")


class TestSessionSpan:
    async def test_creates_session_span(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test task"):
            pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "arcagent.session"
        assert spans[0].attributes is not None
        assert spans[0].attributes["agent.task"] == "test task"

    async def test_session_span_has_agent_did(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test"):
            pass
        spans = exporter.get_finished_spans()
        assert spans[0].attributes is not None
        assert spans[0].attributes["agent.did"] == "did:arc:test:executor/abcd1234"


class TestTurnSpan:
    async def test_creates_turn_span(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test"):
            async with telemetry.turn_span(1):
                pass
        spans = exporter.get_finished_spans()
        turn_spans = [s for s in spans if s.name == "arcagent.turn"]
        assert len(turn_spans) == 1
        assert turn_spans[0].attributes is not None
        assert turn_spans[0].attributes["agent.turn_number"] == 1

    async def test_turn_nests_under_session(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test"):
            async with telemetry.turn_span(1):
                pass
        spans = exporter.get_finished_spans()
        session = next(s for s in spans if s.name == "arcagent.session")
        turn = next(s for s in spans if s.name == "arcagent.turn")
        assert turn.parent is not None
        assert turn.parent.span_id == session.context.span_id


class TestToolSpan:
    async def test_creates_tool_span(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test"):
            async with telemetry.turn_span(1):
                async with telemetry.tool_span("read_file", {"path": "workspace/test"}):
                    pass
        spans = exporter.get_finished_spans()
        tool_spans = [s for s in spans if s.name == "arcagent.tool"]
        assert len(tool_spans) == 1
        assert tool_spans[0].attributes is not None
        assert tool_spans[0].attributes["tool.name"] == "read_file"

    async def test_tool_nests_under_turn(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test"):
            async with telemetry.turn_span(1):
                async with telemetry.tool_span("read_file", {"path": "workspace"}):
                    pass
        spans = exporter.get_finished_spans()
        turn = next(s for s in spans if s.name == "arcagent.turn")
        tool = next(s for s in spans if s.name == "arcagent.tool")
        assert tool.parent is not None
        assert tool.parent.span_id == turn.context.span_id


class TestAuditEvent:
    async def test_audit_event_logs_structured_json(
        self, telemetry: AgentTelemetry, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            async with telemetry.session_span("test"):
                telemetry.audit_event("tool.executed", {"tool": "read_file"})
        assert len(caplog.records) >= 1
        record = caplog.records[0]
        data = json.loads(record.message)
        assert data["event_type"] == "tool.executed"
        assert data["details"]["tool"] == "read_file"
        assert data["agent_did"] == "did:arc:test:executor/abcd1234"

    async def test_audit_event_creates_span_event(
        self, telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with telemetry.session_span("test"):
            telemetry.audit_event("tool.executed", {"tool": "read_file"})
        spans = exporter.get_finished_spans()
        session = spans[0]
        events = session.events
        assert len(events) >= 1
        assert events[0].name == "audit:tool.executed"

    def test_typed_audit_sink_forwards_event_metadata(
        self, telemetry: AgentTelemetry, caplog: pytest.LogCaptureFixture
    ) -> None:
        from arctrust import AuditEvent

        sink = TelemetryAuditSink(telemetry)
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:actor",
                    action="stream.end",
                    target="stream:1234",
                    outcome="success",
                    payload_hash="abcd",
                )
            )

        data = json.loads(caplog.records[-1].message)
        assert data["event_type"] == "stream.end"
        assert data["details"]["target"] == "stream:1234"
        assert data["details"]["payload_hash"] == "abcd"


class TestSetAgentDid:
    def test_set_agent_did_updates_did(self, telemetry: AgentTelemetry) -> None:
        telemetry.set_agent_did("did:arc:new:executor/9999")
        assert telemetry._agent_did == "did:arc:new:executor/9999"

    async def test_audit_event_uses_updated_did(
        self,
        telemetry: AgentTelemetry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        telemetry.set_agent_did("did:arc:updated:executor/abcd")
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            telemetry.audit_event("test.event", {"key": "val"})
        data = json.loads(caplog.records[0].message)
        assert data["agent_did"] == "did:arc:updated:executor/abcd"


class TestAuditRedaction:
    async def test_sensitive_keys_redacted(
        self,
        telemetry: AgentTelemetry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            telemetry.audit_event(
                "vault.access",
                {
                    "vault_path": "secret/keys",
                    "api_key": "sk-live-12345",
                    "password": "hunter2",
                    "username": "admin",
                },
            )
        data = json.loads(caplog.records[0].message)
        assert data["details"]["api_key"] == "[REDACTED]"
        assert data["details"]["password"] == "[REDACTED]"
        assert data["details"]["username"] == "admin"  # Not sensitive

    async def test_nested_sensitive_keys_redacted(
        self,
        telemetry: AgentTelemetry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            telemetry.audit_event(
                "test",
                {
                    "config": {"secret_value": "hidden", "name": "visible"},
                },
            )
        data = json.loads(caplog.records[0].message)
        assert data["details"]["config"]["secret_value"] == "[REDACTED]"
        assert data["details"]["config"]["name"] == "visible"

    async def test_non_sensitive_keys_preserved(
        self,
        telemetry: AgentTelemetry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            telemetry.audit_event(
                "tool.executed",
                {
                    "tool": "read_file",
                    "duration_ms": 42,
                },
            )
        data = json.loads(caplog.records[0].message)
        assert data["details"]["tool"] == "read_file"
        assert data["details"]["duration_ms"] == 42


class TestDisabledTelemetry:
    async def test_disabled_session_span_is_noop(
        self, disabled_telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with disabled_telemetry.session_span("test"):
            pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 0

    async def test_disabled_turn_span_is_noop(
        self, disabled_telemetry: AgentTelemetry, exporter: InMemorySpanExporter
    ) -> None:
        async with disabled_telemetry.session_span("test"):
            async with disabled_telemetry.turn_span(1):
                pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 0

    async def test_disabled_audit_event_still_logs(
        self,
        disabled_telemetry: AgentTelemetry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Audit events always log, even when OTel spans are disabled."""
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            disabled_telemetry.audit_event("test.event", {"key": "val"})
        assert len(caplog.records) >= 1
