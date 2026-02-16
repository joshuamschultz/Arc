"""Tests for tool registry — 4 transports, policy, wrapping, arcrun conversion."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcrun import Tool as ArcRunTool

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    NativeToolEntry,
    ToolConfig,
    ToolsConfig,
)
from arcagent.core.errors import ToolError, ToolVetoedError
from arcagent.core.module_bus import EventContext, ModuleBus
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport


@pytest.fixture()
def config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
    )


@pytest.fixture()
def mock_telemetry() -> MagicMock:
    tel = MagicMock()
    tel.audit_event = MagicMock()
    tool_span = MagicMock()
    tool_span.__aenter__ = AsyncMock(return_value=MagicMock())
    tool_span.__aexit__ = AsyncMock(return_value=False)
    tel.tool_span = MagicMock(return_value=tool_span)
    return tel


@pytest.fixture()
def bus(config: ArcAgentConfig, mock_telemetry: MagicMock) -> ModuleBus:
    return ModuleBus(config=config, telemetry=mock_telemetry)


@pytest.fixture()
def registry(
    config: ArcAgentConfig, bus: ModuleBus, mock_telemetry: MagicMock
) -> ToolRegistry:
    return ToolRegistry(
        config=config.tools, bus=bus, telemetry=mock_telemetry
    )


def _make_tool(
    name: str = "test_tool",
    transport: ToolTransport = ToolTransport.NATIVE,
    timeout: int = 30,
) -> RegisteredTool:
    async def execute(**kwargs: Any) -> str:
        return f"result:{name}"

    return RegisteredTool(
        name=name,
        description=f"Test tool {name}",
        input_schema={"type": "object", "properties": {}},
        transport=transport,
        execute=execute,
        timeout_seconds=timeout,
    )


class TestRegisteredTool:
    def test_create_tool(self) -> None:
        tool = _make_tool("my_tool")
        assert tool.name == "my_tool"
        assert tool.transport == ToolTransport.NATIVE

    def test_tool_transport_enum(self) -> None:
        assert ToolTransport.NATIVE.value == "native"
        assert ToolTransport.MCP.value == "mcp"
        assert ToolTransport.HTTP.value == "http"
        assert ToolTransport.PROCESS.value == "process"


class TestRegister:
    def test_register_tool(self, registry: ToolRegistry) -> None:
        tool = _make_tool("read_file")
        registry.register(tool)
        assert "read_file" in registry.tools

    def test_register_multiple_tools(self, registry: ToolRegistry) -> None:
        registry.register(_make_tool("read_file"))
        registry.register(_make_tool("write_file"))
        assert len(registry.tools) == 2


class TestPolicyEnforcement:
    def test_allowlist_blocks_unlisted(self) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(
                policy=ToolConfig(allow=["read_file"])
            ),
        )
        registry = ToolRegistry(
            config=config.tools, bus=MagicMock(), telemetry=MagicMock()
        )
        registry.register(_make_tool("read_file"))
        with pytest.raises(ToolError) as exc_info:
            registry.register(_make_tool("shell_exec"))
        assert exc_info.value.code == "TOOL_POLICY_DENIED"

    def test_denylist_blocks_listed(self) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(
                policy=ToolConfig(deny=["shell_exec"])
            ),
        )
        registry = ToolRegistry(
            config=config.tools, bus=MagicMock(), telemetry=MagicMock()
        )
        registry.register(_make_tool("read_file"))  # Allowed
        with pytest.raises(ToolError) as exc_info:
            registry.register(_make_tool("shell_exec"))
        assert exc_info.value.code == "TOOL_POLICY_DENIED"

    def test_empty_allowlist_allows_all(self, registry: ToolRegistry) -> None:
        registry.register(_make_tool("anything"))
        assert "anything" in registry.tools


class TestToolWrapping:
    async def test_wrapped_execute_emits_pre_tool(
        self, registry: ToolRegistry, bus: ModuleBus
    ) -> None:
        events: list[str] = []

        async def track_pre_tool(ctx: EventContext) -> None:
            events.append("pre_tool")

        bus.subscribe("agent:pre_tool", track_pre_tool)
        tool = _make_tool("read_file")
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        await wrapped({})
        assert "pre_tool" in events

    async def test_wrapped_execute_emits_post_tool(
        self, registry: ToolRegistry, bus: ModuleBus
    ) -> None:
        events: list[str] = []

        async def track_post_tool(ctx: EventContext) -> None:
            events.append("post_tool")

        bus.subscribe("agent:post_tool", track_post_tool)
        tool = _make_tool("read_file")
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        await wrapped({})
        assert "post_tool" in events

    async def test_veto_blocks_execution(
        self, registry: ToolRegistry, bus: ModuleBus
    ) -> None:
        async def veto_handler(ctx: EventContext) -> None:
            ctx.veto("blocked by policy")

        bus.subscribe("agent:pre_tool", veto_handler, priority=10)
        tool = _make_tool("shell_exec")
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        with pytest.raises(ToolVetoedError):
            await wrapped({})

    async def test_audit_event_logged(
        self, registry: ToolRegistry, mock_telemetry: MagicMock
    ) -> None:
        tool = _make_tool("read_file")
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        await wrapped({})
        mock_telemetry.audit_event.assert_called()


class TestToArcrunTools:
    def test_returns_list_of_arcrun_tools(self, registry: ToolRegistry) -> None:
        registry.register(_make_tool("read_file"))
        registry.register(_make_tool("write_file"))
        tools = registry.to_arcrun_tools()
        assert len(tools) == 2
        assert all(isinstance(t, ArcRunTool) for t in tools)

    def test_tool_has_required_fields(self, registry: ToolRegistry) -> None:
        registry.register(_make_tool("read_file"))
        tools = registry.to_arcrun_tools()
        tool = tools[0]
        assert tool.name == "read_file"
        assert tool.description
        assert tool.input_schema is not None
        assert callable(tool.execute)
        assert tool.timeout_seconds is None  # Our wrapper handles timeouts


class TestTimeoutEnforcement:
    async def test_tool_exceeds_timeout_raises(
        self, registry: ToolRegistry
    ) -> None:
        async def slow_execute(**kwargs: Any) -> str:
            await asyncio.sleep(10)
            return "slow"

        tool = RegisteredTool(
            name="slow_tool",
            description="Slow",
            input_schema={},
            transport=ToolTransport.NATIVE,
            execute=slow_execute,
            timeout_seconds=1,
        )
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        with pytest.raises(ToolError) as exc_info:
            await wrapped({})
        assert exc_info.value.code == "TOOL_TIMEOUT"


class TestNativeToolRegistration:
    def test_register_native_tools(self, registry: ToolRegistry) -> None:
        """Register native tools from config entries."""
        entries = {
            "echo": NativeToolEntry(
                module="arcagent.core.tool_registry:_echo_tool",
                description="Echo input",
            )
        }
        registry.register_native_tools(entries)
        assert "echo" in registry.tools


class TestModuleAllowlist:
    def test_allowed_module_accepted(self) -> None:
        """Module in allowed prefixes can be imported."""
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(
                allowed_module_prefixes=["arcagent."],
            ),
        )
        registry = ToolRegistry(
            config=config.tools, bus=MagicMock(), telemetry=MagicMock()
        )
        entries = {
            "echo": NativeToolEntry(
                module="arcagent.core.tool_registry:_echo_tool",
                description="Echo",
            )
        }
        registry.register_native_tools(entries)
        assert "echo" in registry.tools

    def test_disallowed_module_rejected(self) -> None:
        """Module not in allowed prefixes is rejected."""
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(
                allowed_module_prefixes=["arcagent."],
            ),
        )
        registry = ToolRegistry(
            config=config.tools, bus=MagicMock(), telemetry=MagicMock()
        )
        entries = {
            "evil": NativeToolEntry(
                module="os:system",
                description="Evil tool",
            )
        }
        with pytest.raises(ToolError) as exc_info:
            registry.register_native_tools(entries)
        assert exc_info.value.code == "TOOL_MODULE_NOT_ALLOWED"

    def test_missing_colon_rejected(self) -> None:
        """Module ref without ':' separator is rejected."""
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
        )
        registry = ToolRegistry(
            config=config.tools, bus=MagicMock(), telemetry=MagicMock()
        )
        entries = {
            "bad": NativeToolEntry(
                module="os.system",
                description="Bad format",
            )
        }
        with pytest.raises(ToolError) as exc_info:
            registry.register_native_tools(entries)
        assert exc_info.value.code == "TOOL_INVALID_MODULE"


class TestArgValidation:
    async def test_missing_required_arg_rejected(
        self, registry: ToolRegistry
    ) -> None:
        """Tool call missing required args is rejected."""
        tool = RegisteredTool(
            name="strict_tool",
            description="Requires 'path'",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
        )
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        with pytest.raises(ToolError) as exc_info:
            await wrapped({})
        assert exc_info.value.code == "TOOL_INVALID_ARGS"

    async def test_valid_args_accepted(
        self, registry: ToolRegistry
    ) -> None:
        """Tool call with all required args passes validation."""
        tool = RegisteredTool(
            name="valid_tool",
            description="Requires 'path'",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
        )
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        result = await wrapped({"path": "/test"})
        assert result == "ok"

    async def test_unknown_args_rejected_when_strict(
        self, registry: ToolRegistry
    ) -> None:
        """Unknown args rejected when additionalProperties is false."""
        tool = RegisteredTool(
            name="strict_schema",
            description="No extra args",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
        )
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        with pytest.raises(ToolError) as exc_info:
            await wrapped({"path": "/test", "evil": "inject"})
        assert exc_info.value.code == "TOOL_INVALID_ARGS"


class TestShutdown:
    async def test_shutdown_clears_tools(self, registry: ToolRegistry) -> None:
        registry.register(_make_tool("read_file"))
        await registry.shutdown()
        assert len(registry.tools) == 0
