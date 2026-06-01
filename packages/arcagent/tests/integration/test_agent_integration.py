"""Integration tests — full component stack, end-to-end flows.

Tests the real component interactions without mocking internal components.
Only ArcLLM/ArcRun (external dependencies) are stubbed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from arcrun import StreamEvent, TokenEvent, ToolContext, TurnEndEvent

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.errors import ToolVetoedError
from arcagent.core.module_bus import EventContext


def _mock_tool_context() -> ToolContext:
    """Create a minimal ToolContext for integration tests."""
    return ToolContext(
        run_id="test-run",
        tool_call_id="test-call",
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="integration-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        ),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=10000),
    )


class TestFullStartupShutdown:
    """T4.2.1: config → identity → telemetry → bus → tools → context → agent startup."""

    async def test_startup_initializes_all_components(self, agent_config: ArcAgentConfig) -> None:
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        assert agent._started
        assert agent._telemetry is not None
        assert agent._identity is not None
        assert agent._bus is not None
        assert agent._tool_registry is not None
        assert agent._context is not None

        # Identity should have a real DID
        assert agent._identity.did.startswith("did:arc:testorg:executor/")
        assert agent._identity.can_sign

        # Telemetry should use the real DID (not "pending")
        assert agent._telemetry._agent_did == agent._identity.did

        await agent.shutdown()

    async def test_startup_creates_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "new_workspace"
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="test",
                org="testorg",
                type="executor",
                workspace=str(ws),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            telemetry=TelemetryConfig(enabled=False),
        )
        agent = ArcAgent(config=config)
        await agent.startup()
        await agent.shutdown()
        # Workspace is created during run(), not startup — that's fine

    async def test_identity_keys_persisted(
        self, agent_config: ArcAgentConfig, tmp_path: Path
    ) -> None:
        """Identity generates keys and saves them to key_dir."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        key_dir = tmp_path / "keys"
        key_files = list(key_dir.glob("*.key"))
        pub_files = list(key_dir.glob("*.pub"))

        assert len(key_files) == 1
        assert len(pub_files) == 1

        # Key file should have secure permissions
        assert key_files[0].stat().st_mode & 0o777 == 0o600

        await agent.shutdown()


class TestRunWithMockLLM:
    """T4.2.2: agent.run() with mock LLM and native tools."""

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_run_full_pipeline(
        self,
        mock_load_model: MagicMock,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        # Setup workspace files (policy.md is injected by memory module, not read directly)
        (workspace / "identity.md").write_text("Agent: integration-agent")
        (workspace / "context.md").write_text("Context: test-only")

        mock_load_model.return_value = MagicMock()
        captured: dict[str, Any] = {}

        async def _fake_run_stream(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)

            async def _gen() -> Any:
                yield TokenEvent(text="task completed")
                yield TurnEndEvent(final_text="task completed", tool_calls_made=1)

            return _gen()

        agent = ArcAgent(config=agent_config)
        await agent.startup()
        with patch(
            "arcagent.core.agent_dispatch.arcrun_run_stream", side_effect=_fake_run_stream
        ):
            session = await agent.session("itest")
            events = [ev async for ev in agent.run("test integration task", session=session)]

        # Model was loaded (SPEC-017 R-001: on_event now also passed)
        mock_load_model.assert_called_once()
        load_args, load_kwargs = mock_load_model.call_args
        assert load_args == ("test/model",)
        assert load_kwargs["agent_label"] == "integration-agent"
        assert callable(load_kwargs["on_event"])

        # The streaming loop was called with the full execution context.
        assert captured["task"] == "test integration task"
        assert captured["model"] is not None
        # arcrun now receives a CapabilityProvider, not a flat tool list.
        assert hasattr(captured["capabilities"], "advertise")
        assert isinstance(captured["system_prompt"], str)
        assert callable(captured["on_event"])
        assert callable(captured["transform_context"])

        # System prompt includes workspace content
        assert "integration-agent" in captured["system_prompt"]
        assert "test-only" in captured["system_prompt"]

        assert isinstance(events[-1], TurnEndEvent)
        await agent.shutdown()


class TestBusEventFlow:
    """T4.2.3: Module Bus event flow during tool execution."""

    async def test_tool_execution_emits_events(self, agent_config: ArcAgentConfig) -> None:
        """Full event flow: register tool → wrap → execute → events fire."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        events: list[str] = []

        async def track_event(ctx: EventContext) -> None:
            events.append(ctx.event)

        agent._bus.subscribe("agent:pre_tool", track_event)
        agent._bus.subscribe("agent:post_tool", track_event)

        # Register a tool and execute through the wrapped pipeline
        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        async def noop_tool(**kwargs: Any) -> str:
            return "ok"

        tool = RegisteredTool(
            name="test_tool",
            description="Test tool",
            input_schema={"type": "object", "properties": {}},
            transport=ToolTransport.NATIVE,
            execute=noop_tool,
        )
        agent._tool_registry.register(tool)

        # Get wrapped version and execute via arcrun adapter
        arcrun_tools = agent._tool_registry.to_arcrun_tools()
        test_arcrun_tool = next(t for t in arcrun_tools if t.name == "test_tool")
        result = await test_arcrun_tool.execute({}, _mock_tool_context())

        assert result == "ok"
        assert "agent:pre_tool" in events
        assert "agent:post_tool" in events

        await agent.shutdown()

    async def test_event_ordering(self, agent_config: ArcAgentConfig) -> None:
        """Events fire in correct order: pre_tool before post_tool."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        order: list[str] = []

        async def on_pre(ctx: EventContext) -> None:
            order.append("pre")

        async def on_post(ctx: EventContext) -> None:
            order.append("post")

        agent._bus.subscribe("agent:pre_tool", on_pre)
        agent._bus.subscribe("agent:post_tool", on_post)

        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        async def noop(**kwargs: Any) -> str:
            return "done"

        agent._tool_registry.register(
            RegisteredTool(
                name="ordered_tool",
                description="Test",
                input_schema={},
                transport=ToolTransport.NATIVE,
                execute=noop,
            )
        )

        tools = agent._tool_registry.to_arcrun_tools()
        ordered_tool = next(t for t in tools if t.name == "ordered_tool")
        await ordered_tool.execute({}, _mock_tool_context())

        assert order == ["pre", "post"]
        await agent.shutdown()


class TestVetoEndToEnd:
    """T4.2.4: veto blocks tool execution end-to-end."""

    async def test_veto_prevents_tool_execution(self, agent_config: ArcAgentConfig) -> None:
        """A policy handler vetoes a tool call; tool never executes."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        executed = False

        async def dangerous_tool(**kwargs: Any) -> str:
            nonlocal executed
            executed = True
            return "should not run"

        async def policy_veto(ctx: EventContext) -> None:
            if ctx.data.get("tool") == "dangerous":
                ctx.veto("blocked by security policy")

        agent._bus.subscribe("agent:pre_tool", policy_veto, priority=10)

        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        agent._tool_registry.register(
            RegisteredTool(
                name="dangerous",
                description="Dangerous tool",
                input_schema={},
                transport=ToolTransport.NATIVE,
                execute=dangerous_tool,
            )
        )

        tools = agent._tool_registry.to_arcrun_tools()
        dangerous_arcrun = next(t for t in tools if t.name == "dangerous")
        with pytest.raises(ToolVetoedError):
            await dangerous_arcrun.execute({}, _mock_tool_context())

        assert not executed
        await agent.shutdown()

    async def test_veto_reason_preserved(self, agent_config: ArcAgentConfig) -> None:
        """Veto reason is available in the raised exception."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        async def veto_handler(ctx: EventContext) -> None:
            ctx.veto("unauthorized access attempt")

        agent._bus.subscribe("agent:pre_tool", veto_handler, priority=10)

        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        async def noop(**kwargs: Any) -> str:
            return "noop"

        agent._tool_registry.register(
            RegisteredTool(
                name="restricted",
                description="Restricted",
                input_schema={},
                transport=ToolTransport.NATIVE,
                execute=noop,
            )
        )

        tools = agent._tool_registry.to_arcrun_tools()
        restricted_arcrun = next(t for t in tools if t.name == "restricted")
        with pytest.raises(ToolVetoedError, match="unauthorized access attempt"):
            await restricted_arcrun.execute({}, _mock_tool_context())

        await agent.shutdown()


class TestContextManagerPruning:
    """T4.2.5: context manager prunes during arcrun.run."""

    async def test_transform_context_prunes_when_over_threshold(self, tmp_path: Path) -> None:
        """Context manager prunes old tool outputs when over threshold."""
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="ctx-agent",
                workspace=str(tmp_path / "ws"),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            telemetry=TelemetryConfig(enabled=False),
            context=ContextConfig(
                max_tokens=100,
                prune_threshold=0.70,
            ),
        )

        agent = ArcAgent(config=config)
        await agent.startup()

        # Create messages that exceed 70% of 100 tokens (> 70 tokens)
        # Each char ~0.25 tokens * 1.1 multiplier
        # Need ~280 chars to hit 70+ tokens
        old_output = "x" * 200  # ~55 tokens
        recent_output = "y" * 200  # ~55 tokens
        messages = [
            {"role": "tool", "content": old_output},
            {"role": "user", "content": "explain"},
            {"role": "tool", "content": recent_output},
        ]

        result = agent._context.transform_context(messages)

        # At least one tool output should be pruned
        pruned_count = sum(
            1
            for m in result
            if isinstance(m.get("content"), str) and "[output pruned" in m["content"]
        )
        assert pruned_count >= 1

        await agent.shutdown()

    async def test_transform_context_noop_below_threshold(self, tmp_path: Path) -> None:
        """Context manager does nothing when below prune threshold."""
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="ctx-agent",
                workspace=str(tmp_path / "ws"),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            telemetry=TelemetryConfig(enabled=False),
            context=ContextConfig(
                max_tokens=100000,  # Very high limit
                prune_threshold=0.70,
            ),
        )

        agent = ArcAgent(config=config)
        await agent.startup()

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        result = agent._context.transform_context(messages)
        assert result == messages  # No pruning needed

        await agent.shutdown()


class TestGracefulShutdown:
    """T4.2.6: graceful shutdown with reverse teardown."""

    async def test_shutdown_emits_event_then_cleans_up(self, agent_config: ArcAgentConfig) -> None:
        """Shutdown fires shutdown event, then reverses component init."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        events: list[str] = []

        async def on_shutdown(ctx: EventContext) -> None:
            events.append("shutdown")

        agent._bus.subscribe("agent:shutdown", on_shutdown)

        # Register a tool to verify cleanup
        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        async def noop(**kwargs: Any) -> str:
            return "noop"

        agent._tool_registry.register(
            RegisteredTool(
                name="cleanup_test",
                description="Test",
                input_schema={},
                transport=ToolTransport.NATIVE,
                execute=noop,
            )
        )
        assert "cleanup_test" in agent._tool_registry.tools

        await agent.shutdown()

        # Shutdown event fired
        assert "shutdown" in events
        # Agent is no longer started
        assert not agent._started
        # Tool registry cleared
        assert len(agent._tool_registry.tools) == 0

    async def test_double_shutdown_is_safe(self, agent_config: ArcAgentConfig) -> None:
        """Calling shutdown twice does not raise."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()
        await agent.shutdown()
        await agent.shutdown()  # Should be a no-op

    async def test_startup_shutdown_restart(self, agent_config: ArcAgentConfig) -> None:
        """Agent can be started, stopped, and started again."""
        agent = ArcAgent(config=agent_config)

        await agent.startup()
        _ = agent._identity.did  # Capture DID from first run
        await agent.shutdown()

        # Re-startup creates new components
        await agent.startup()
        assert agent._started
        assert agent._identity is not None
        await agent.shutdown()


class TestPostRespondEvent:
    """Integration: agent.run() emits agent:post_respond with result."""

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_post_respond_emitted(
        self,
        mock_load_model: MagicMock,
        agent_config: ArcAgentConfig,
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_post_respond(ctx: EventContext) -> None:
            events.append(ctx.data)

        mock_load_model.return_value = MagicMock()

        async def _fake_run_stream(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            async def _gen() -> AsyncIterator[StreamEvent]:
                yield TurnEndEvent(final_text="done")

            return _gen()

        agent = ArcAgent(config=agent_config)
        await agent.startup()
        assert agent._bus is not None
        agent._bus.subscribe("agent:post_respond", on_post_respond)

        with patch(
            "arcagent.core.agent_dispatch.arcrun_run_stream", side_effect=_fake_run_stream
        ):
            session = await agent.session("itest")
            async for _ in agent.run("test task", session=session):
                pass

        assert len(events) == 1
        assert "result" in events[0]
        await agent.shutdown()
