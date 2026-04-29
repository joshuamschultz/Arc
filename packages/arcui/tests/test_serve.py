"""Tests for serve() and attach_llm() wiring."""

from unittest.mock import MagicMock

from arcui.auth import AuthConfig
from arcui.server import attach_llm, create_app


class TestCreateApp:
    def test_creates_starlette_app(self):
        app = create_app()
        assert hasattr(app.state, "connection_manager")
        assert hasattr(app.state, "aggregator")
        assert hasattr(app.state, "event_buffer")

    def test_creates_with_auth_config(self):
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        assert app.state.auth_config.viewer_token == "v"

    def test_creates_with_trace_store(self):
        store = MagicMock()
        app = create_app(trace_store=store)
        assert app.state.trace_store is store

    def test_creates_with_config_controller(self):
        ctrl = MagicMock()
        app = create_app(config_controller=ctrl)
        assert app.state.config_controller is ctrl

    def test_creates_with_agent_info(self):
        info = {
            "name": "test-agent",
            "did": "did:arc:local:executor/abc123",
            "model": "anthropic/claude-sonnet-4-6",
        }
        app = create_app(agent_info=info)
        assert app.state.agent_info == info

    def test_agent_info_defaults_to_empty_dict(self):
        app = create_app()
        assert app.state.agent_info == {}


class TestAttachLLM:
    def test_attaches_on_event_callback(self):
        app = create_app()
        instance = MagicMock()
        instance._inner = None  # No module stack

        attach_llm(app, instance, label="test-agent")

        assert hasattr(app.state, "on_event_callbacks")
        assert len(app.state.on_event_callbacks) == 1

    def test_discovers_circuit_breakers_in_stack(self):
        from arcllm.modules.circuit_breaker import CircuitBreakerModule
        from arcllm.modules.telemetry import TelemetryModule

        app = create_app()

        # Simulate module stack: outer._inner = cb, cb._inner = adapter
        adapter = MagicMock()
        adapter._inner = None

        cb = MagicMock(spec=CircuitBreakerModule)
        cb._inner = adapter

        outer = MagicMock(spec=TelemetryModule)
        outer._inner = cb

        attach_llm(app, outer)

        assert len(app.state.circuit_breakers) == 1
        assert len(app.state.telemetry_modules) == 1

    def test_on_event_callback_feeds_buffer_and_aggregator(self):
        app = create_app()
        instance = MagicMock()
        instance._inner = None

        attach_llm(app, instance, label="agent-1")

        callback = app.state.on_event_callbacks[0]

        # Simulate a TraceRecord-like object
        record = MagicMock()
        record.model_dump.return_value = {
            "total_tokens": 100,
            "cost_usd": 0.001,
            "duration_ms": 200.0,
            "model": "test",
            "provider": "test",
        }

        callback(record)

        # Buffer should have 1 pending event
        assert app.state.event_buffer.pending_count == 1

        # Aggregator should have 1 request
        stats = app.state.aggregator.stats("1h")
        assert stats["request_count"] == 1
