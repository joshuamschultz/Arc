"""Tests for serve() and attach_llm() wiring."""

from unittest.mock import MagicMock

from arcui.auth import AuthConfig
from arcui.server import attach_llm, create_app


class TestCreateApp:
    def test_creates_starlette_app(self):
        # SPEC-026 FR-5: push pipeline deleted. Verify surviving state attributes.
        app = create_app()
        assert hasattr(app.state, "observe")
        assert hasattr(app.state, "agent_registry")
        assert hasattr(app.state, "circuit_breakers")

    def test_creates_with_auth_config(self):
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        assert app.state.auth_config.viewer_token == "v"

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
    def test_attaches_without_error(self):
        # SPEC-026 FR-5: attach_llm only walks the module stack for circuit breakers,
        # telemetry and queue modules. No event callbacks or push wiring.
        app = create_app()
        instance = MagicMock()
        instance._inner = None  # No module stack

        attach_llm(app, instance, label="test-agent")

        # No crash; circuit_breakers list is still empty (no CB in the stack).
        assert app.state.circuit_breakers == []

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

    def test_attaches_telemetry_module_in_stack(self):
        # SPEC-026 FR-5: attach_llm discovers TelemetryModule in the stack.
        from arcllm.modules.telemetry import TelemetryModule

        app = create_app()

        adapter = MagicMock()
        adapter._inner = None

        tm = MagicMock(spec=TelemetryModule)
        tm._inner = adapter

        attach_llm(app, tm, label="agent-1")

        assert len(app.state.telemetry_modules) == 1
