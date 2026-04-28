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
    return ModuleBus()


@pytest.fixture()
def registry(config: ArcAgentConfig, bus: ModuleBus, mock_telemetry: MagicMock) -> ToolRegistry:
    return ToolRegistry(config=config.tools, bus=bus, telemetry=mock_telemetry)


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
    """Tool policy filters registration silently — does NOT raise.

    Behavior change (was: raise ToolError on every denied registration):
    Denied tools are skipped, logged, and audited. The agent starts cleanly
    with the remaining tools, and the LLM never sees the denied tool in its
    catalog. This lets a least-privilege deploy (deny=[`write`,`bash`]) work
    without crashing the agent at startup when built-in tools register.
    """

    def test_allowlist_skips_unlisted_tool(self) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(policy=ToolConfig(allow=["read_file"])),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
        registry.register(_make_tool("read_file"))
        registry.register(_make_tool("shell_exec"))  # silently skipped
        assert "read_file" in registry.tools
        assert "shell_exec" not in registry.tools

    def test_denylist_skips_listed_tool(self) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(policy=ToolConfig(deny=["shell_exec"])),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
        registry.register(_make_tool("read_file"))
        registry.register(_make_tool("shell_exec"))  # silently skipped
        assert "read_file" in registry.tools
        assert "shell_exec" not in registry.tools

    def test_empty_allow_and_deny_allows_all(self, registry: ToolRegistry) -> None:
        registry.register(_make_tool("anything"))
        assert "anything" in registry.tools

    def test_deny_takes_precedence_over_allow(self) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(policy=ToolConfig(allow=["x"], deny=["x"])),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
        registry.register(_make_tool("x"))
        assert "x" not in registry.tools

    def test_skip_emits_audit_event(self) -> None:
        telemetry = MagicMock()
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(policy=ToolConfig(deny=["shell_exec"])),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=telemetry)
        registry.register(_make_tool("shell_exec"))
        # Audit event fired for the policy-denied registration
        calls = [c for c in telemetry.audit_event.call_args_list
                 if c.args and c.args[0] == "tool.policy_denied"]
        assert len(calls) == 1, f"expected one tool.policy_denied audit event; got {telemetry.audit_event.call_args_list}"
        assert calls[0].args[1]["tool"] == "shell_exec"

    def test_skip_logs_warning(self, caplog) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(policy=ToolConfig(deny=["shell_exec"])),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
        with caplog.at_level("WARNING"):
            registry.register(_make_tool("shell_exec"))
        assert any(
            "shell_exec" in rec.message and "polic" in rec.message.lower()
            for rec in caplog.records
        ), f"expected policy-skip warning; got {[r.message for r in caplog.records]}"


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

    async def test_veto_blocks_execution(self, registry: ToolRegistry, bus: ModuleBus) -> None:
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
    async def test_tool_exceeds_timeout_raises(self, registry: ToolRegistry) -> None:
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
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
        entries = {
            "echo": NativeToolEntry(
                module="arcagent.core.tool_registry:_echo_tool",
                description="Echo",
            )
        }
        registry.register_native_tools(entries)
        assert "echo" in registry.tools

    def test_getattr_nonexistent_function(self) -> None:
        """Line 172: getattr on module for nonexistent function."""
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
        entries = {
            "nonexistent": NativeToolEntry(
                module="arcagent.core.tool_registry:_nonexistent_func",
                description="Nonexistent",
            )
        }
        with pytest.raises(AttributeError):
            registry.register_native_tools(entries)

    def test_disallowed_module_rejected(self) -> None:
        """Module not in allowed prefixes is rejected."""
        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            tools=ToolsConfig(
                allowed_module_prefixes=["arcagent."],
            ),
        )
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
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
        registry = ToolRegistry(config=config.tools, bus=MagicMock(), telemetry=MagicMock())
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
    async def test_missing_required_arg_rejected(self, registry: ToolRegistry) -> None:
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

    async def test_valid_args_accepted(self, registry: ToolRegistry) -> None:
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

    async def test_unknown_args_rejected_when_strict(self, registry: ToolRegistry) -> None:
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


class TestEchoTool:
    """Line 53: Built-in _echo_tool function."""

    def test_echo_tool_returns_echoed_text(self) -> None:
        from arcagent.core.tool_registry import _echo_tool

        assert _echo_tool("hello") == "echo: hello"

    def test_echo_tool_default_empty(self) -> None:
        from arcagent.core.tool_registry import _echo_tool

        assert _echo_tool() == "echo: "


class TestNativeToolAsyncWrapper:
    """Line 172: _async_wrapper wraps sync functions."""

    async def test_native_tool_wrapper_invokes_function(self, registry: ToolRegistry) -> None:
        native_tools = {
            "echo": NativeToolEntry(
                module="arcagent.core.tool_registry:_echo_tool",
                description="Echo tool",
            )
        }
        registry.register_native_tools(native_tools)
        tool = registry._tools["echo"]
        result = await tool.execute(text="world")
        assert result == "echo: world"


class TestFormatForPrompt:
    """Tests for ToolRegistry.format_for_prompt() — R1, R5 requirements."""

    def test_empty_registry_returns_empty(self, registry: ToolRegistry) -> None:
        """R1.6: Empty catalog produces no section."""
        assert registry.format_for_prompt() == ""

    def test_single_tool_renders_xml(self, registry: ToolRegistry) -> None:
        """R1.3: Catalog uses XML format."""
        registry.register(_make_tool("read_file"))
        result = registry.format_for_prompt()
        assert "<available-tools>" in result
        assert "</available-tools>" in result
        assert 'name="read_file"' in result
        assert "<description>" in result

    def test_tool_with_when_to_use(self, registry: ToolRegistry) -> None:
        """R1: when_to_use renders as child element."""
        tool = _make_tool("send")
        tool.when_to_use = "When communicating with teammates"
        registry.register(tool)
        result = registry.format_for_prompt()
        assert "<when-to-use>When communicating with teammates</when-to-use>" in result

    def test_tool_with_example(self, registry: ToolRegistry) -> None:
        """R1: example renders as child element."""
        tool = _make_tool("read")
        tool.example = "read(path='/etc/hosts')"
        registry.register(tool)
        result = registry.format_for_prompt()
        assert "<example>" in result

    def test_tool_with_category(self, registry: ToolRegistry) -> None:
        """R1: category renders as attribute."""
        tool = _make_tool("send")
        tool.category = "messaging"
        registry.register(tool)
        result = registry.format_for_prompt()
        assert 'category="messaging"' in result

    def test_xml_escaping(self, registry: ToolRegistry) -> None:
        """R1.4: All string values are XML-escaped."""
        tool = RegisteredTool(
            name='tool<"evil">',
            description="desc with <tags> & ampersands",
            input_schema={"type": "object", "properties": {}},
            transport=ToolTransport.NATIVE,
            execute=lambda: None,
            when_to_use="use when <needed>",
            example='example("a&b")',
            category='cat"quote',
        )
        registry.register(tool)
        result = registry.format_for_prompt()
        # XML-escaped characters should appear
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&quot;" in result

    def test_tools_sorted_alphabetically(self, registry: ToolRegistry) -> None:
        """Tools sorted by name in catalog."""
        registry.register(_make_tool("zebra"))
        registry.register(_make_tool("alpha"))
        registry.register(_make_tool("middle"))
        result = registry.format_for_prompt()
        alpha_pos = result.index("alpha")
        middle_pos = result.index("middle")
        zebra_pos = result.index("zebra")
        assert alpha_pos < middle_pos < zebra_pos

    def test_cache_hit_returns_same_object(self, registry: ToolRegistry) -> None:
        """R1.5: Second call returns cached result."""
        registry.register(_make_tool("test"))
        first = registry.format_for_prompt()
        second = registry.format_for_prompt()
        assert first is second  # Same object — cache hit

    def test_cache_invalidated_on_register(self, registry: ToolRegistry) -> None:
        """R1.5: register() clears cache."""
        registry.register(_make_tool("first"))
        first = registry.format_for_prompt()
        registry.register(_make_tool("second"))
        second = registry.format_for_prompt()
        assert first is not second  # Cache was invalidated
        assert "second" in second

    def test_optional_fields_omitted_when_empty(self, registry: ToolRegistry) -> None:
        """Optional fields not rendered when empty."""
        registry.register(_make_tool("basic"))
        result = registry.format_for_prompt()
        assert "<when-to-use>" not in result
        assert "<example>" not in result
        assert "category=" not in result

    def test_preamble_included(self, registry: ToolRegistry) -> None:
        """R5.1: Default preamble present in output."""
        registry.register(_make_tool("test"))
        result = registry.format_for_prompt()
        assert "<preamble>" in result

    def test_custom_preamble(self) -> None:
        """R5.2: Preamble overridable via config."""
        custom_config = ToolsConfig(preamble="Custom preamble text")
        reg = ToolRegistry(config=custom_config, bus=MagicMock(), telemetry=MagicMock())
        reg.register(_make_tool("test"))
        result = reg.format_for_prompt()
        assert "Custom preamble text" in result

    def test_preamble_xml_escaped(self) -> None:
        """R5.3: Preamble text is XML-escaped."""
        config = ToolsConfig(preamble="Use tools <carefully> & wisely")
        reg = ToolRegistry(config=config, bus=MagicMock(), telemetry=MagicMock())
        reg.register(_make_tool("test"))
        result = reg.format_for_prompt()
        assert "&lt;carefully&gt;" in result
        assert "&amp;" in result

    @pytest.mark.asyncio
    async def test_shutdown_clears_prompt_cache(self) -> None:
        """R1.2: Tools removed via shutdown() disappear from next prompt assembly."""
        reg = ToolRegistry(config=ToolsConfig(), bus=MagicMock(), telemetry=MagicMock())
        reg.register(_make_tool("test"))
        assert reg.format_for_prompt() != ""
        await reg.shutdown()
        assert reg.format_for_prompt() == ""

    def test_is_prompt_cached_property(self, registry: ToolRegistry) -> None:
        """Public property exposes cache state without accessing private attrs."""
        assert not registry.is_prompt_cached
        registry.register(_make_tool("test"))
        registry.format_for_prompt()
        assert registry.is_prompt_cached
        registry.register(_make_tool("another"))
        assert not registry.is_prompt_cached


class TestWrappedExecuteKwargs:
    """Line 228: wrapped_execute passes kwargs when args is None."""

    async def test_kwargs_forwarded(self, registry: ToolRegistry) -> None:
        tool = RegisteredTool(
            name="kw_test",
            description="test",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
        )
        registry.register(tool)
        wrapped = registry._create_wrapped_execute(tool)
        result = await wrapped(x="hello")
        assert result == "ok"


class TestToolClassification:
    """SPEC-017 R-020: every tool is classified as read_only or state_modifying."""

    def test_classification_defaults_to_state_modifying(self) -> None:
        """Fail-closed default — tools without explicit classification
        are treated as state_modifying so they cannot accidentally be
        dispatched in a parallel read-only batch."""
        tool = RegisteredTool(
            name="unannotated",
            description="test",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
        )
        assert tool.classification == "state_modifying"

    def test_explicit_read_only(self) -> None:
        tool = RegisteredTool(
            name="read_x",
            description="read file",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
            classification="read_only",
        )
        assert tool.classification == "read_only"


class TestBuiltinToolClassifications:
    """SPEC-017 Task 3.2: Every built-in tool has a correct classification.

    Read-only: read, grep, find, ls (observe filesystem, no mutation)
    State-modifying: bash, edit, write (mutate filesystem or process state)
    """

    def test_builtin_classifications(self, tmp_path: Any) -> None:
        from arcagent.tools import create_builtin_tools

        tools = {t.name: t for t in create_builtin_tools(tmp_path)}

        expected_read_only = {"read", "grep", "find", "ls"}
        expected_state_mod = {"bash", "edit", "write"}

        for name in expected_read_only:
            assert name in tools, f"missing built-in: {name}"
            assert tools[name].classification == "read_only", (
                f"{name} must be read_only but is {tools[name].classification}"
            )

        for name in expected_state_mod:
            assert name in tools, f"missing built-in: {name}"
            assert tools[name].classification == "state_modifying", (
                f"{name} must be state_modifying but is {tools[name].classification}"
            )


class TestPipelineEnforcement:
    """SPEC-017 R-010 / R-011: pipeline is consulted on every dispatch.

    Phase 3 integration — when the registry is constructed with a
    ``PolicyPipeline``, every dispatch goes through it. A deny
    raises :class:`PolicyDenied` and aborts the call.
    """

    async def test_registry_consults_pipeline_on_dispatch(
        self,
        config: ArcAgentConfig,
        bus: ModuleBus,
        mock_telemetry: MagicMock,
    ) -> None:
        from arcagent.core.tool_policy import PolicyPipeline
        from arcagent.core.tool_registry import ToolRegistry

        evaluated: list[str] = []

        class RecordingLayer:
            name = "recording"

            async def evaluate(self, call: Any, ctx: Any) -> Any:
                from arcagent.core.tool_policy import Decision

                evaluated.append(call.tool_name)
                return Decision.allow(input_hash="h", evaluated_at_us=1)

        pipeline = PolicyPipeline(layers=[RecordingLayer()])
        reg = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=mock_telemetry,
            policy_pipeline=pipeline,
        )

        tool = RegisteredTool(
            name="probe",
            description="probe",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
            classification="read_only",
        )
        reg.register(tool)
        wrapped = reg._create_wrapped_execute(tool)
        await wrapped()

        assert evaluated == ["probe"]

    async def test_pipeline_deny_raises_policy_denied(
        self,
        config: ArcAgentConfig,
        bus: ModuleBus,
        mock_telemetry: MagicMock,
    ) -> None:
        from arcagent.core.tool_policy import (
            Decision,
            PolicyDenied,
            PolicyPipeline,
        )
        from arcagent.core.tool_registry import ToolRegistry

        class DenyAll:
            name = "deny_all"

            async def evaluate(self, call: Any, ctx: Any) -> Any:
                return Decision.deny(
                    layer="deny_all",
                    rule_id="test.block",
                    reason=f"{call.tool_name} blocked",
                    input_hash="h",
                    evaluated_at_us=1,
                )

        pipeline = PolicyPipeline(layers=[DenyAll()])
        reg = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=mock_telemetry,
            policy_pipeline=pipeline,
        )

        tool = RegisteredTool(
            name="blocked",
            description="blocked",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
            classification="state_modifying",
        )
        reg.register(tool)
        wrapped = reg._create_wrapped_execute(tool)

        with pytest.raises(PolicyDenied) as exc_info:
            await wrapped()

        assert exc_info.value.decision.layer == "deny_all"
        assert "blocked" in exc_info.value.decision.reason

    async def test_pipeline_deny_does_not_invoke_tool(
        self,
        config: ArcAgentConfig,
        bus: ModuleBus,
        mock_telemetry: MagicMock,
    ) -> None:
        """A denied call must never reach ``execute()`` — a single code
        path through ``_create_wrapped_execute`` guarantees this."""
        from arcagent.core.tool_policy import (
            Decision,
            PolicyDenied,
            PolicyPipeline,
        )
        from arcagent.core.tool_registry import ToolRegistry

        class DenyAll:
            name = "deny_all"

            async def evaluate(self, call: Any, ctx: Any) -> Any:
                return Decision.deny(
                    layer="deny_all",
                    rule_id="test.block",
                    reason="blocked",
                    input_hash="h",
                    evaluated_at_us=1,
                )

        called = {"count": 0}

        async def _exec(**kwargs: Any) -> str:
            called["count"] += 1
            return "should_not_happen"

        pipeline = PolicyPipeline(layers=[DenyAll()])
        reg = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=mock_telemetry,
            policy_pipeline=pipeline,
        )
        tool = RegisteredTool(
            name="blocked",
            description="blocked",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=_exec,
            classification="state_modifying",
        )
        reg.register(tool)
        wrapped = reg._create_wrapped_execute(tool)

        with pytest.raises(PolicyDenied):
            await wrapped()

        assert called["count"] == 0

    async def test_no_pipeline_means_no_enforcement(
        self,
        config: ArcAgentConfig,
        bus: ModuleBus,
        mock_telemetry: MagicMock,
    ) -> None:
        """Backward compat — registry without pipeline behaves as before.

        The pipeline is opt-in to preserve the existing test surface.
        When the wider codebase wires a pipeline through, enforcement
        becomes load-bearing. Until then the registry is permissive."""
        from arcagent.core.tool_registry import ToolRegistry

        reg = ToolRegistry(config=config.tools, bus=bus, telemetry=mock_telemetry)
        tool = RegisteredTool(
            name="ok",
            description="ok",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ran"),
            classification="read_only",
        )
        reg.register(tool)
        wrapped = reg._create_wrapped_execute(tool)
        result = await wrapped()
        assert result == "ran"

    async def test_registry_propagates_tier_to_policy_context(
        self,
        config: ArcAgentConfig,
        bus: ModuleBus,
        mock_telemetry: MagicMock,
    ) -> None:
        """SPEC-017 review finding: tier must flow into PolicyContext.

        A prior version hardcoded ``tier="personal"`` in the dispatch
        path, which meant federal and enterprise deployments recorded
        ``tier="personal"`` in their audit trail. Regression test."""
        from arcagent.core.tool_policy import PolicyPipeline
        from arcagent.core.tool_registry import ToolRegistry

        contexts: list[Any] = []

        class _CtxRecorder:
            name = "recorder"

            async def evaluate(self, call: Any, ctx: Any) -> Any:
                from arcagent.core.tool_policy import Decision

                contexts.append(ctx)
                return Decision.allow(input_hash="h", evaluated_at_us=1)

        pipeline = PolicyPipeline(layers=[_CtxRecorder()])
        reg = ToolRegistry(
            config=config.tools,
            bus=bus,
            telemetry=mock_telemetry,
            policy_pipeline=pipeline,
            tier="federal",
            policy_version="v2.7.1",
        )
        tool = RegisteredTool(
            name="probe",
            description="probe",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=AsyncMock(return_value="ok"),
            classification="read_only",
        )
        reg.register(tool)
        wrapped = reg._create_wrapped_execute(tool)
        await wrapped()

        assert len(contexts) == 1
        assert contexts[0].tier == "federal"
        assert contexts[0].policy_version == "v2.7.1"


# Ensure asyncio import isn't flagged as unused
_ = asyncio
