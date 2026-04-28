"""Tests for UIReporterModule — event wrapping, control forwarding, config."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.ui_reporter import UIReporterConfig, UIReporterModule


@pytest.fixture
def probe_ok() -> Any:
    """Force `_should_auto_enable` to succeed so startup() reaches transport setup.

    Transport-wiring tests don't care about the probe — they care about what
    happens *after* it passes. This fixture removes the probe as a variable.
    """
    with patch(
        "arcagent.modules.ui_reporter._should_auto_enable",
        return_value=(True, "probe_ok", "test-token"),
    ) as m:
        yield m

# --- Helpers ---


def _make_module(
    tmp_path: Path,
    config: dict[str, Any] | None = None,
    transport: Any | None = None,
) -> UIReporterModule:
    """Create a UIReporterModule with defaults."""
    cfg = {"enabled": True, "url": "ws://localhost:8420/api/agent/connect", "token": "test-token"}
    if config:
        cfg.update(config)
    return UIReporterModule(config=cfg, workspace=tmp_path, transport=transport)


def _make_ctx(bus: Any | None = None) -> MagicMock:
    """Create a minimal ModuleContext mock."""
    ctx = MagicMock()
    ctx.bus = bus or MagicMock()
    ctx.config = MagicMock()
    ctx.config.agent = MagicMock()
    ctx.config.agent.name = "test-agent"
    ctx.config.agent.did = "did:arc:test"
    return ctx


# --- Config Tests ---


class TestUIReporterConfig:
    def test_default_config(self) -> None:
        cfg = UIReporterConfig()
        # SPEC-019 FR-9: default is enabled=True (probe at startup).
        assert cfg.enabled is True
        assert cfg.url == "ws://localhost:8420/api/agent/connect"
        assert cfg.token == ""
        assert cfg.reconnect_max_interval == 60.0
        assert cfg.buffer_size == 1000

    def test_custom_config(self) -> None:
        cfg = UIReporterConfig(
            enabled=True,
            url="ws://ui:9000/api/agent/connect",
            token="my-secret",
            reconnect_max_interval=120.0,
            buffer_size=500,
        )
        assert cfg.enabled is True
        assert cfg.url == "ws://ui:9000/api/agent/connect"
        assert cfg.token == "my-secret"
        assert cfg.reconnect_max_interval == 120.0
        assert cfg.buffer_size == 500


# --- Module Protocol Tests ---


class TestModuleProtocol:
    def test_has_name(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module.name == "ui_reporter"

    def test_has_startup(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert callable(module.startup)

    def test_has_shutdown(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert callable(module.shutdown)

    def test_disabled_module_skips_startup(self, tmp_path: Path) -> None:
        module = UIReporterModule(config={"enabled": False}, workspace=tmp_path)
        assert module._config.enabled is False


# --- Event Wrapping Tests ---


class TestEventWrapping:
    def test_llm_event_wraps_as_llm_layer(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        payload = module._wrap_event(
            event="llm:call_complete",
            data={"model": "gpt-4", "tokens": 100},
        )
        assert payload["layer"] == "llm"
        assert payload["event_type"] == "call_complete"
        assert payload["data"]["model"] == "gpt-4"

    def test_agent_tool_event_wraps_as_run_layer(self, tmp_path: Path) -> None:
        """Tool/plan events from arcrun bridge map to run layer."""
        module = _make_module(tmp_path)
        payload = module._wrap_event(
            event="agent:pre_tool",
            data={"tool": "search"},
        )
        assert payload["layer"] == "run"
        assert payload["event_type"] == "pre_tool"

    def test_agent_plan_event_wraps_as_run_layer(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        payload = module._wrap_event(
            event="agent:post_plan",
            data={},
        )
        assert payload["layer"] == "run"
        assert payload["event_type"] == "post_plan"

    def test_agent_lifecycle_event_wraps_as_agent_layer(self, tmp_path: Path) -> None:
        """Lifecycle events (init, ready, shutdown) stay as agent layer."""
        module = _make_module(tmp_path)
        payload = module._wrap_event(
            event="agent:ready",
            data={},
        )
        assert payload["layer"] == "agent"
        assert payload["event_type"] == "ready"

    def test_agent_respond_event_wraps_as_agent_layer(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        payload = module._wrap_event(
            event="agent:pre_respond",
            data={"task": "hello"},
        )
        assert payload["layer"] == "agent"
        assert payload["event_type"] == "pre_respond"

    def test_agent_error_event_wraps_as_agent_layer(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        payload = module._wrap_event(
            event="agent:error",
            data={"error": "something broke"},
        )
        assert payload["layer"] == "agent"
        assert payload["event_type"] == "error"

    def test_wrap_increments_sequence(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        p1 = module._wrap_event("llm:call_complete", {})
        p2 = module._wrap_event("llm:call_complete", {})
        assert p2["sequence"] == p1["sequence"] + 1

    def test_wrap_includes_agent_identity(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        module._agent_name = "my-agent"
        module._agent_id = "agent-123"
        module._source_id = "did:arc:test"
        payload = module._wrap_event("agent:ready", {})
        assert payload["agent_name"] == "my-agent"
        assert payload["agent_id"] == "agent-123"
        assert payload["source_id"] == "did:arc:test"


# --- Layer Classification Tests ---


class TestLayerClassification:
    """Verify the event → layer mapping logic."""

    @pytest.mark.parametrize(
        "event,expected_layer",
        [
            ("llm:call_complete", "llm"),
            ("llm:config_change", "llm"),
            ("llm:circuit_change", "llm"),
            ("agent:pre_tool", "run"),
            ("agent:post_tool", "run"),
            ("agent:pre_plan", "run"),
            ("agent:post_plan", "run"),
            ("agent:init", "agent"),
            ("agent:ready", "agent"),
            ("agent:shutdown", "agent"),
            ("agent:pre_respond", "agent"),
            ("agent:post_respond", "agent"),
            ("agent:error", "agent"),
            ("agent:extensions_loaded", "agent"),
            ("agent:skills_loaded", "agent"),
        ],
    )
    def test_event_layer_mapping(self, tmp_path: Path, event: str, expected_layer: str) -> None:
        module = _make_module(tmp_path)
        payload = module._wrap_event(event, {})
        assert payload["layer"] == expected_layer, f"{event} should map to {expected_layer}"


# --- Transport Tests ---


class TestTransportWiring:
    @pytest.mark.asyncio
    async def test_startup_sets_agent_id_from_config(
        self, tmp_path: Path, probe_ok: Any
    ) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx()
        await module.startup(ctx)
        assert module._agent_id == "did:arc:test"
        assert module._agent_name == "test-agent"

    @pytest.mark.asyncio
    async def test_startup_creates_transport_when_enabled(
        self, tmp_path: Path, probe_ok: Any
    ) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx()
        await module.startup(ctx)
        # Transport should be created from config
        assert module._transport is not None

    @pytest.mark.asyncio
    async def test_on_event_sends_via_transport(self, tmp_path: Path) -> None:
        mock_transport = AsyncMock()
        mock_transport.send_event = AsyncMock()
        module = _make_module(tmp_path, transport=mock_transport)
        module._agent_id = "agent-123"

        ctx = MagicMock()
        ctx.event = "llm:call_complete"
        ctx.data = {"model": "gpt-4"}
        await module._on_event(ctx)

        mock_transport.send_event.assert_called_once()
        call_args = mock_transport.send_event.call_args
        assert call_args[0][0] == "agent-123"

    @pytest.mark.asyncio
    async def test_shutdown_closes_transport(self, tmp_path: Path) -> None:
        mock_transport = AsyncMock()
        module = _make_module(tmp_path, transport=mock_transport)
        await module.shutdown()
        mock_transport.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_transport_startup(self, tmp_path: Path) -> None:
        """Module with no token should not create transport.

        Probe returns success but with `file_token=None`; config token is
        empty; ARCUI_AGENT_TOKEN env is cleared. The resolved token is
        therefore "" → no transport. Exercises the "post-probe, but no
        token from any source" branch directly without patching the
        (now-inlined) token resolver.
        """
        module = UIReporterModule(
            config={"enabled": True, "token": ""},
            workspace=tmp_path,
        )
        ctx = _make_ctx()
        with patch(
            "arcagent.modules.ui_reporter._should_auto_enable",
            return_value=(True, "probe_ok", None),
        ), patch.dict("os.environ", {"ARCUI_AGENT_TOKEN": ""}, clear=False):
            await module.startup(ctx)
        assert module._transport is None
