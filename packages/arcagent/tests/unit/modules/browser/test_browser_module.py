"""Tests for BrowserModule — Module protocol, lifecycle, tool registration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import EventContext, Module, ModuleBus, ModuleContext


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="anthropic/claude-haiku"),
    )


class TestBrowserModuleProtocol:
    """BrowserModule satisfies the Module protocol."""

    def test_satisfies_module_protocol(self) -> None:
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=Path("/tmp/test"))
        assert isinstance(module, Module)

    def test_name_is_browser(self) -> None:
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=Path("/tmp/test"))
        assert module.name == "browser"


class TestBrowserModuleStartup:
    """startup() connects CDP, creates AX manager, and registers tools."""

    async def test_startup_connects_cdp_and_registers_tools(self, tmp_path: Path) -> None:
        """startup() creates CDPClientManager, AccessibilityManager, and registers tools."""
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=tmp_path)
        config = _make_config()
        tool_registry = MagicMock()

        ctx = ModuleContext(
            bus=ModuleBus(config=config, telemetry=MagicMock()),
            tool_registry=tool_registry,
            config=config,
            telemetry=_make_telemetry(),
            workspace=tmp_path,
            llm_config=config.llm,
        )

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/abc"
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

            mock_cdp_cls.assert_called_once()
            mock_cdp.connect.assert_called_once()
            # Tools should be registered (at least 9 base tools)
            assert tool_registry.register.call_count >= 9

    async def test_startup_subscribes_to_shutdown(self, tmp_path: Path) -> None:
        """startup() subscribes to agent:shutdown event."""
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=tmp_path)
        config = _make_config()
        bus = ModuleBus(config=config, telemetry=MagicMock())

        ctx = ModuleContext(
            bus=bus,
            tool_registry=MagicMock(),
            config=config,
            telemetry=_make_telemetry(),
            workspace=tmp_path,
            llm_config=config.llm,
        )

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/abc"
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

            # Verify shutdown handler is subscribed
            assert "agent:shutdown" in bus._handlers
            assert len(bus._handlers["agent:shutdown"]) >= 1


class TestBrowserModuleShutdown:
    """shutdown() disconnects CDP cleanly."""

    async def test_shutdown_disconnects_cdp(self, tmp_path: Path) -> None:
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=tmp_path)
        mock_cdp = AsyncMock()
        mock_cdp.disconnect = AsyncMock()
        module._cdp = mock_cdp

        await module.shutdown()
        mock_cdp.disconnect.assert_called_once()
        assert module._ax is None

    async def test_shutdown_without_cdp_is_safe(self, tmp_path: Path) -> None:
        """shutdown() doesn't raise when CDP was never connected."""
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=tmp_path)
        await module.shutdown()  # Should not raise

    async def test_on_shutdown_emits_disconnected_event(self, tmp_path: Path) -> None:
        """_on_shutdown() emits browser.disconnected before shutting down."""
        from arcagent.modules.browser.browser_module import BrowserModule

        module = BrowserModule(workspace=tmp_path)
        mock_cdp = AsyncMock()
        mock_cdp.disconnect = AsyncMock()
        module._cdp = mock_cdp

        bus = MagicMock()
        bus.emit = AsyncMock()
        module._bus = bus

        ctx = MagicMock(spec=EventContext)

        await module._on_shutdown(ctx)
        bus.emit.assert_called_once_with("browser.disconnected", {})
        mock_cdp.disconnect.assert_called_once()
