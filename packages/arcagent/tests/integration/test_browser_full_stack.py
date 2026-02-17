"""Full-stack integration tests — Browser Module → ToolRegistry → ArcRun bridge.

Verifies the complete tool lifecycle without launching a real browser:
1. Module discovered via MODULE.yaml convention
2. BrowserModule.startup() registers tools with ToolRegistry
3. ToolRegistry.to_arcrun_tools() bridges to ArcRun Tool format
4. ArcRun tools are callable with proper arg validation and audit events
5. Agent-level startup discovers and loads the browser module
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
)
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import ToolRegistry


@asynccontextmanager
async def _noop_span(*_args: Any, **_kwargs: Any) -> AsyncIterator[MagicMock]:
    """Async context manager stub for telemetry spans."""
    yield MagicMock()


def _make_telemetry() -> MagicMock:
    t = MagicMock(spec=AgentTelemetry)
    t.audit_event = MagicMock()
    t.tool_span = _noop_span
    t.session_span = _noop_span
    return t


def _make_config(workspace: str = "./test-workspace") -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test-browser-agent", workspace=workspace),
        llm=LLMConfig(model="test/model"),
        modules={"browser": ModuleEntry(enabled=True)},
    )


def _make_bus(config: ArcAgentConfig | None = None) -> ModuleBus:
    if config is None:
        config = _make_config()
    return ModuleBus(config=config, telemetry=_make_telemetry())


class TestBrowserModuleDiscovery:
    """Module loader discovers browser via MODULE.yaml."""

    def test_module_yaml_exists(self) -> None:
        """MODULE.yaml is present and parseable."""
        import yaml

        yaml_path = (
            Path(__file__).resolve().parents[2]
            / "arcagent"
            / "modules"
            / "browser"
            / "MODULE.yaml"
        )
        assert yaml_path.exists(), f"MODULE.yaml not found at {yaml_path}"

        with yaml_path.open() as f:
            manifest = yaml.safe_load(f)

        assert manifest["name"] == "browser"
        assert "entry_point" in manifest
        assert manifest["entry_point"] == "arcagent.modules.browser:BrowserModule"

    def test_module_loader_discovers_browser(self) -> None:
        """ModuleLoader.discover() finds browser when enabled in config."""
        from arcagent.core.module_loader import ModuleLoader

        config = _make_config()
        modules_dir = (
            Path(__file__).resolve().parents[2] / "arcagent" / "modules"
        )

        loader = ModuleLoader()
        manifests = loader.discover(modules_dir, config)
        names = [m.name for m in manifests]

        assert "browser" in names

    def test_module_loader_skips_disabled_browser(self) -> None:
        """ModuleLoader.discover() skips browser when disabled."""
        from arcagent.core.module_loader import ModuleLoader

        config = ArcAgentConfig(
            agent=AgentConfig(name="test", workspace="./ws"),
            llm=LLMConfig(model="test/model"),
            modules={"browser": ModuleEntry(enabled=False)},
        )
        modules_dir = (
            Path(__file__).resolve().parents[2] / "arcagent" / "modules"
        )

        loader = ModuleLoader()
        manifests = loader.discover(modules_dir, config)
        names = [m.name for m in manifests]

        assert "browser" not in names


class TestBrowserToolRegistration:
    """BrowserModule.startup() registers tools with ToolRegistry."""

    async def test_startup_registers_expected_tools(self, tmp_path: Path) -> None:
        """startup() registers all 9+ browser tools in the ToolRegistry."""
        from arcagent.modules.browser.browser_module import BrowserModule

        config = _make_config(workspace=str(tmp_path))
        telemetry = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        module = BrowserModule(workspace=tmp_path)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

        # Verify all expected browser tools are registered
        tool_names = list(tool_registry.tools.keys())
        expected_tools = [
            "browser_navigate",
            "browser_go_back",
            "browser_go_forward",
            "browser_reload",
            "browser_read_page",
            "browser_click",
            "browser_type",
            "browser_select",
            "browser_hover",
            "browser_fill_form",
            "browser_screenshot",
            "browser_handle_dialog",
            "browser_get_cookies",
            "browser_set_cookies",
            "browser_execute_js",
            "browser_download_file",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Missing tool: {tool_name}"

        assert len(tool_names) >= 16

        await module.shutdown()


class TestToolRegistryToArcRunBridge:
    """ToolRegistry.to_arcrun_tools() creates valid ArcRun tools."""

    async def test_browser_tools_bridge_to_arcrun(self, tmp_path: Path) -> None:
        """Browser tools convert to arcrun.Tool with correct schemas."""
        from arcrun import Tool as ArcRunTool

        from arcagent.modules.browser.browser_module import BrowserModule

        config = _make_config(workspace=str(tmp_path))
        telemetry = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        module = BrowserModule(workspace=tmp_path)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

        # Bridge to ArcRun
        arcrun_tools = tool_registry.to_arcrun_tools()
        assert len(arcrun_tools) >= 16

        # Each tool must be a valid ArcRunTool with required fields
        for tool in arcrun_tools:
            assert isinstance(tool, ArcRunTool)
            assert tool.name
            assert tool.description
            assert tool.input_schema
            assert callable(tool.execute)
            # Timeout is None because arcagent manages timeouts
            assert tool.timeout_seconds is None

        # Verify specific tool schemas are correct
        nav_tool = next(t for t in arcrun_tools if t.name == "browser_navigate")
        assert "url" in nav_tool.input_schema.get("properties", {})
        assert "url" in nav_tool.input_schema.get("required", [])

        click_tool = next(t for t in arcrun_tools if t.name == "browser_click")
        assert "ref" in click_tool.input_schema.get("properties", {})
        assert "ref" in click_tool.input_schema.get("required", [])

        type_tool = next(t for t in arcrun_tools if t.name == "browser_type")
        assert "ref" in type_tool.input_schema.get("properties", {})
        assert "text" in type_tool.input_schema.get("properties", {})

        fill_tool = next(t for t in arcrun_tools if t.name == "browser_fill_form")
        assert "fields" in fill_tool.input_schema.get("properties", {})

        await module.shutdown()

    async def test_arcrun_tool_execution_fires_bus_events(
        self, tmp_path: Path
    ) -> None:
        """Executing an arcrun-bridged tool fires pre/post events and audit."""
        from arcrun import ToolContext

        from arcagent.modules.browser.browser_module import BrowserModule

        config = _make_config(workspace=str(tmp_path))
        telemetry = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        module = BrowserModule(workspace=tmp_path)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp.send = AsyncMock()
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

        # Bridge to ArcRun
        arcrun_tools = tool_registry.to_arcrun_tools()
        reload_tool = next(t for t in arcrun_tools if t.name == "browser_reload")

        # Execute via ArcRun bridge (reload has no args)
        tool_ctx = ToolContext(
            run_id="test-run",
            tool_call_id="tc-1",
            turn_number=1,
            event_bus=None,
            cancelled=MagicMock(),
        )

        result = await reload_tool.execute(args={}, ctx=tool_ctx)
        assert isinstance(result, str)
        assert "reload" in result.lower()

        # Verify audit trail was fired
        telemetry.audit_event.assert_called()
        audit_calls = [
            call for call in telemetry.audit_event.call_args_list
            if call[0][0] == "tool.executed"
        ]
        assert len(audit_calls) >= 1
        assert audit_calls[0][0][1]["tool"] == "browser_reload"

        await module.shutdown()

    async def test_arcrun_navigate_tool_with_url_policy(
        self, tmp_path: Path
    ) -> None:
        """Navigate tool enforces URL policy through the ArcRun bridge."""
        from arcrun import ToolContext

        from arcagent.modules.browser.browser_module import BrowserModule

        config = _make_config(workspace=str(tmp_path))
        telemetry = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        module = BrowserModule(workspace=tmp_path)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp.send = AsyncMock()
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

        arcrun_tools = tool_registry.to_arcrun_tools()
        nav_tool = next(t for t in arcrun_tools if t.name == "browser_navigate")

        tool_ctx = ToolContext(
            run_id="test-run",
            tool_call_id="tc-2",
            turn_number=1,
            event_bus=None,
            cancelled=MagicMock(),
        )

        # Blocked scheme should raise through the full stack
        with pytest.raises(Exception, match=r"file.*blocked|Scheme"):
            await nav_tool.execute(
                args={"url": "file:///etc/passwd"}, ctx=tool_ctx
            )

        await module.shutdown()


class TestAgentLevelBrowserLoading:
    """Full ArcAgent startup discovers and loads browser module."""

    async def test_agent_startup_loads_browser_module(
        self, tmp_path: Path
    ) -> None:
        """ArcAgent.startup() discovers and registers browser module."""
        from arcagent.core.agent import ArcAgent

        config = ArcAgentConfig(
            agent=AgentConfig(name="test-agent", workspace=str(tmp_path)),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            modules={"browser": ModuleEntry(enabled=True)},
        )

        agent = ArcAgent(config=config)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp_cls.return_value = mock_cdp

            await agent.startup()

        # Browser module should be registered on the bus
        browser_mod = agent._bus.get_module("browser")
        assert browser_mod is not None
        assert browser_mod.name == "browser"

        # Browser tools should be in the tool registry
        tool_names = list(agent._tool_registry.tools.keys())
        assert "browser_navigate" in tool_names
        assert "browser_click" in tool_names
        assert "browser_type" in tool_names
        assert "browser_screenshot" in tool_names
        assert "browser_read_page" in tool_names
        assert "browser_fill_form" in tool_names

        # Tools should bridge to ArcRun
        arcrun_tools = agent._tool_registry.to_arcrun_tools()
        arcrun_tool_names = [t.name for t in arcrun_tools]
        assert "browser_navigate" in arcrun_tool_names
        assert "browser_click" in arcrun_tool_names

        await agent.shutdown()

    async def test_agent_without_browser_module(self, tmp_path: Path) -> None:
        """ArcAgent works fine without browser module enabled."""
        from arcagent.core.agent import ArcAgent

        config = ArcAgentConfig(
            agent=AgentConfig(name="test-agent", workspace=str(tmp_path)),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            modules={},  # No modules enabled
        )

        agent = ArcAgent(config=config)
        await agent.startup()

        # Browser tools should NOT be registered
        tool_names = list(agent._tool_registry.tools.keys())
        assert "browser_navigate" not in tool_names
        assert "browser_click" not in tool_names

        await agent.shutdown()


class TestBrowserEventWiring:
    """Bus events fire correctly through the full stack."""

    async def test_browser_connected_event_on_startup(
        self, tmp_path: Path
    ) -> None:
        """browser.connected event fires during startup with cdp_url and tool_count."""
        from arcagent.modules.browser.browser_module import BrowserModule

        config = _make_config(workspace=str(tmp_path))
        telemetry = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=telemetry,
        )

        # Track browser.connected event
        connected_events: list[dict] = []

        async def on_connected(ctx: object) -> None:
            from arcagent.core.module_bus import EventContext

            assert isinstance(ctx, EventContext)
            connected_events.append(dict(ctx.data))

        bus.subscribe("browser.connected", on_connected)

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        module = BrowserModule(workspace=tmp_path)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

        assert len(connected_events) == 1
        assert connected_events[0]["cdp_url"] == "ws://localhost:9222/devtools/browser/test"
        assert connected_events[0]["tool_count"] >= 16

        await module.shutdown()

    async def test_browser_disconnected_event_on_shutdown(
        self, tmp_path: Path
    ) -> None:
        """browser.disconnected event fires during agent:shutdown."""
        from arcagent.modules.browser.browser_module import BrowserModule

        config = _make_config(workspace=str(tmp_path))
        telemetry = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=telemetry,
        )

        disconnected_events: list[dict] = []

        async def on_disconnected(ctx: object) -> None:
            from arcagent.core.module_bus import EventContext

            assert isinstance(ctx, EventContext)
            disconnected_events.append(dict(ctx.data))

        bus.subscribe("browser.disconnected", on_disconnected)

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        module = BrowserModule(workspace=tmp_path)

        with patch(
            "arcagent.modules.browser.browser_module.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.disconnect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/test"
            mock_cdp_cls.return_value = mock_cdp

            await module.startup(ctx)

        # Simulate agent:shutdown event
        await bus.emit("agent:shutdown", {})

        assert len(disconnected_events) == 1
        assert not module._cdp  # CDP disconnected

        # Module already shut down via _on_shutdown handler
