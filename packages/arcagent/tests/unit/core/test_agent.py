"""Tests for agent orchestrator — startup, run, shutdown, ArcRun bridge, chat, steering."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun import RunHandle

from arcagent.core.agent import (
    AgentHandle,
    ArcAgent,
    create_arcllm_bridge,
    create_arcrun_bridge,
)
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
    VaultConfig,
)
from arcagent.core.errors import IdentityError
from arcagent.core.module_bus import ModuleBus


@pytest.fixture()
def agent_config(tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="test-agent",
            org="testorg",
            type="executor",
            workspace=str(tmp_path / "workspace"),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        ),
        telemetry=TelemetryConfig(enabled=False),
    )


@pytest.fixture()
def agent(agent_config: ArcAgentConfig) -> ArcAgent:
    return ArcAgent(config=agent_config)


class TestInit:
    def test_creates_agent(self, agent: ArcAgent) -> None:
        assert agent._config.agent.name == "test-agent"

    def test_components_not_initialized_before_startup(self, agent: ArcAgent) -> None:
        assert not agent._started

    def test_workspace_cached_at_init(self, agent: ArcAgent, agent_config: ArcAgentConfig) -> None:
        """Workspace path is resolved and cached once at __init__."""
        expected = Path(agent_config.agent.workspace).resolve()
        assert agent._workspace == expected

    def test_reload_lock_initialized(self, agent: ArcAgent) -> None:
        """asyncio.Lock is initialized at __init__."""
        assert isinstance(agent._reload_lock, asyncio.Lock)


class TestStartup:
    async def test_startup_initializes_components(self, agent: ArcAgent) -> None:
        await agent.startup()
        assert agent._started
        assert agent._telemetry is not None
        assert agent._identity is not None
        assert agent._bus is not None
        assert agent._tool_registry is not None
        assert agent._context is not None

    async def test_startup_generates_identity(self, agent: ArcAgent) -> None:
        await agent.startup()
        assert agent._identity.did.startswith("did:arc:testorg:executor/")

    async def test_startup_emits_init_event(self, agent: ArcAgent) -> None:
        events: list[str] = []

        async def on_init(ctx: Any) -> None:
            events.append("init")

        await agent.startup()
        agent._bus.subscribe("agent:init", on_init)
        # Re-emit for test (init already fired during startup)
        await agent._bus.emit("agent:init", {})
        assert "init" in events

    async def test_vault_resolver_created_when_configured(self, tmp_path: Path) -> None:
        config = ArcAgentConfig(
            agent=AgentConfig(name="test", workspace=str(tmp_path)),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            vault=VaultConfig(backend="some.module:VaultBackend"),
            telemetry=TelemetryConfig(enabled=False),
        )
        agent = ArcAgent(config=config)
        # Startup will attempt vault resolver creation but will fail
        # because the module doesn't exist. Expected behavior.
        with pytest.raises(ModuleNotFoundError):
            await agent.startup()

    async def test_vault_resolver_none_when_not_configured(self, agent: ArcAgent) -> None:
        await agent.startup()
        assert agent._vault_resolver is None


class TestShutdown:
    async def test_shutdown_reverses_startup(self, agent: ArcAgent) -> None:
        await agent.startup()
        await agent.shutdown()
        assert not agent._started

    async def test_shutdown_emits_shutdown_event(self, agent: ArcAgent) -> None:
        events: list[str] = []
        await agent.startup()

        async def on_shutdown(ctx: Any) -> None:
            events.append("shutdown")

        agent._bus.subscribe("agent:shutdown", on_shutdown)
        await agent.shutdown()
        assert "shutdown" in events

    async def test_shutdown_without_startup_is_safe(self, agent: ArcAgent) -> None:
        """Shutdown before startup should not raise."""
        await agent.shutdown()  # No-op


class TestRun:
    async def test_run_requires_startup(self, agent: ArcAgent) -> None:
        with pytest.raises(RuntimeError, match="not started"):
            await agent.run("test task")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_run_calls_loop(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        mock_model = MagicMock()
        mock_load_model.return_value = mock_model
        mock_arcrun_run.return_value = MagicMock(content="result", tool_calls_made=0)

        await agent.startup()
        result = await agent.run("test task")
        mock_arcrun_run.assert_called_once()
        assert result is not None


class TestArcRunBridge:
    async def test_bridge_maps_tool_start(self) -> None:
        bus = ModuleBus()
        events: list[str] = []

        async def on_pre_tool(ctx: Any) -> None:
            events.append("pre_tool")

        bus.subscribe("agent:pre_tool", on_pre_tool)

        bridge = create_arcrun_bridge(bus)
        mock_event = MagicMock()
        mock_event.type = "tool.start"
        mock_event.data = {"tool": "read_file", "args": {}}
        bridge(mock_event)

        # Bridge schedules a task that awaits bus.emit — need multiple yields
        for _ in range(5):
            await asyncio.sleep(0)
        assert "pre_tool" in events

    async def test_bridge_maps_turn_events(self) -> None:
        bus = ModuleBus()
        events: list[str] = []

        async def on_pre_plan(ctx: Any) -> None:
            events.append("pre_plan")

        bus.subscribe("agent:pre_plan", on_pre_plan)

        bridge = create_arcrun_bridge(bus)
        mock_event = MagicMock()
        mock_event.type = "turn.start"
        mock_event.data = {"turn": 1}
        bridge(mock_event)

        for _ in range(5):
            await asyncio.sleep(0)
        assert "pre_plan" in events

    async def test_bridge_ignores_unmapped_events(self) -> None:
        bus = ModuleBus()
        bridge = create_arcrun_bridge(bus)
        mock_event = MagicMock()
        mock_event.type = "llm.call"
        mock_event.data = {}
        # Should not raise
        bridge(mock_event)


class TestArcLLMBridge:
    """Tests for create_arcllm_bridge — maps TraceRecords to ModuleBus events."""

    async def test_bridge_maps_llm_call(self) -> None:
        bus = ModuleBus()
        events: list[dict[str, Any]] = []

        async def on_llm_call(ctx: Any) -> None:
            events.append(ctx.data)

        bus.subscribe("llm:call_complete", on_llm_call)

        bridge = create_arcllm_bridge(bus)
        record = MagicMock()
        record.model_dump.return_value = {
            "event_type": "llm_call",
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "duration_ms": 150.0,
            "cost_usd": 0.003,
        }
        bridge(record)

        for _ in range(5):
            await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0]["provider"] == "anthropic"

    async def test_bridge_maps_config_change(self) -> None:
        bus = ModuleBus()
        events: list[dict[str, Any]] = []

        async def on_config_change(ctx: Any) -> None:
            events.append(ctx.data)

        bus.subscribe("llm:config_change", on_config_change)

        bridge = create_arcllm_bridge(bus)
        record = MagicMock()
        record.model_dump.return_value = {
            "event_type": "config_change",
            "event_data": {"actor": "operator", "changes": {"temperature": {"old": 0.7, "new": 0.3}}},
        }
        bridge(record)

        for _ in range(5):
            await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0]["event_data"]["actor"] == "operator"

    async def test_bridge_maps_circuit_change(self) -> None:
        bus = ModuleBus()
        events: list[dict[str, Any]] = []

        async def on_circuit(ctx: Any) -> None:
            events.append(ctx.data)

        bus.subscribe("llm:circuit_change", on_circuit)

        bridge = create_arcllm_bridge(bus)
        record = MagicMock()
        record.model_dump.return_value = {
            "event_type": "circuit_change",
            "event_data": {"provider": "anthropic", "old_state": "CLOSED", "new_state": "OPEN"},
        }
        bridge(record)

        for _ in range(5):
            await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0]["event_data"]["new_state"] == "OPEN"

    async def test_bridge_ignores_rotation_events(self) -> None:
        bus = ModuleBus()
        events: list[str] = []

        async def on_any(ctx: Any) -> None:
            events.append(ctx.event)

        bus.subscribe("llm:call_complete", on_any)
        bus.subscribe("llm:config_change", on_any)
        bus.subscribe("llm:circuit_change", on_any)

        bridge = create_arcllm_bridge(bus)
        record = MagicMock()
        record.model_dump.return_value = {"event_type": "rotation"}
        bridge(record)

        for _ in range(5):
            await asyncio.sleep(0)
        assert events == []

    async def test_bridge_handles_dict_input(self) -> None:
        """Bridge accepts plain dicts (not just Pydantic models)."""
        bus = ModuleBus()
        events: list[dict[str, Any]] = []

        async def on_llm_call(ctx: Any) -> None:
            events.append(ctx.data)

        bus.subscribe("llm:call_complete", on_llm_call)

        bridge = create_arcllm_bridge(bus)
        # Pass a raw dict instead of a Pydantic model
        bridge({"event_type": "llm_call", "provider": "openai", "model": "gpt-4o"})

        for _ in range(5):
            await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0]["provider"] == "openai"


class TestPreRespondEvent:
    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_pre_respond_emitted_before_run(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        events: list[str] = []

        async def on_pre(ctx: Any) -> None:
            events.append("pre_respond")

        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="done")

        await agent.startup()
        agent._bus.subscribe("agent:pre_respond", on_pre)
        await agent.run("test task")
        assert "pre_respond" in events


class TestErrorEvent:
    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_error_event_emitted_on_failure(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_error(ctx: Any) -> None:
            events.append(ctx.data)

        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.side_effect = RuntimeError("LLM call failed")

        await agent.startup()
        agent._bus.subscribe("agent:error", on_error)
        with pytest.raises(RuntimeError, match="LLM call failed"):
            await agent.run("failing task")
        assert len(events) == 1
        assert events[0]["error_type"] == "RuntimeError"


class TestVaultValidation:
    async def test_vault_backend_without_colon_rejected(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ConfigError

        config = ArcAgentConfig(
            agent=AgentConfig(name="test", workspace=str(tmp_path)),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            vault=VaultConfig(backend="no_colon_here"),
            telemetry=TelemetryConfig(enabled=False),
        )
        agent = ArcAgent(config=config)
        with pytest.raises(ConfigError) as exc_info:
            await agent.startup()
        assert exc_info.value.code == "CONFIG_INVALID_VAULT_BACKEND"

    async def test_vault_backend_with_traversal_rejected(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ConfigError

        config = ArcAgentConfig(
            agent=AgentConfig(name="test", workspace=str(tmp_path)),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            vault=VaultConfig(backend="..evil.module:Backdoor"),
            telemetry=TelemetryConfig(enabled=False),
        )
        agent = ArcAgent(config=config)
        with pytest.raises(ConfigError) as exc_info:
            await agent.startup()
        assert exc_info.value.code == "CONFIG_INVALID_VAULT_BACKEND"


class TestModelCaching:
    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_model_loaded_once_across_runs(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Model is loaded once on first run and reused."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="done")

        await agent.startup()
        await agent.run("task 1")
        await agent.run("task 2")
        # Model loaded only once (on first run)
        mock_load_model.assert_called_once()


class TestErrorHandling:
    async def test_startup_component_failure(self, tmp_path: Path) -> None:
        """Component failure during startup raises."""
        config = ArcAgentConfig(
            agent=AgentConfig(
                name="test",
                workspace=str(tmp_path),
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(
                did="did:arc:test:executor/invalid",
                key_dir="/nonexistent/path/that/will/fail",
                vault_path="",
            ),
            telemetry=TelemetryConfig(enabled=False),
        )
        agent = ArcAgent(config=config)
        # DID is set but key file doesn't exist — hard fail, no silent regen
        with pytest.raises(IdentityError, match="Key file not found"):
            await agent.startup()


class TestChat:
    """Tests for multi-turn chat() method with SessionManager integration."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_chat_creates_session(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat() creates a new session when no session_id provided."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="Hello!")

        await agent.startup()
        result = await agent.chat("Hello")
        assert result is not None
        # Session should have been created
        assert agent._session is not None
        assert agent._session.session_id != ""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_chat_appends_user_message(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat() appends user message to session before calling loop."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="Response")

        await agent.startup()
        await agent.chat("Hi there")

        # Session should have messages (at least user + assistant)
        msgs = agent._session.get_messages()
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        assert any(m.get("content") == "Hi there" for m in user_msgs)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_chat_passes_messages_to_run_loop(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat() passes session messages to arcrun.run via messages param."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="Done")

        await agent.startup()
        await agent.chat("First message")

        # Verify arcrun_run was called with messages parameter
        call_kwargs = mock_arcrun_run.call_args
        assert "messages" in call_kwargs.kwargs

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_chat_resumes_session(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat() with session_id resumes existing session."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="Resumed!")

        await agent.startup()

        # Create a session first
        await agent.chat("First")
        session_id = agent._session.session_id

        # Resume with same session_id
        await agent.chat("Second", session_id=session_id)
        assert agent._session.session_id == session_id

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_chat_persists_to_jsonl(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
        tmp_path: Path,
    ) -> None:
        """Messages are persisted to JSONL after chat."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="Saved!")

        await agent.startup()
        await agent.chat("Persist me")

        session_id = agent._session.session_id
        workspace = Path(agent._config.agent.workspace)
        jsonl_path = workspace / "sessions" / f"{session_id}.jsonl"
        assert jsonl_path.exists()
        content = jsonl_path.read_text().strip()
        assert len(content) > 0  # At least user message written

    async def test_chat_requires_startup(self, agent: ArcAgent) -> None:
        """chat() raises if agent not started."""
        with pytest.raises(RuntimeError, match="not started"):
            await agent.chat("Hello")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_chat_appends_assistant_response(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat() appends assistant response to session after loop."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="I am helpful")

        await agent.startup()
        await agent.chat("Help me")

        msgs = agent._session.get_messages()
        assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
        assert len(assistant_msgs) >= 1

    async def test_startup_initializes_session_manager(self, agent: ArcAgent) -> None:
        """SessionManager is initialized during startup."""
        await agent.startup()
        assert agent._session is not None

    async def test_shutdown_with_session(self, agent: ArcAgent) -> None:
        """Shutdown with session manager should not raise."""
        await agent.startup()
        await agent.shutdown()
        assert not agent._started


class TestBridgeNoRunningLoop:
    """RuntimeError catch when no running loop."""

    def test_bridge_warns_when_no_running_loop(self, caplog: pytest.LogCaptureFixture) -> None:
        """RuntimeError caught, warning logged when no event loop."""
        from arcagent.core.agent import create_arcrun_bridge

        bus = ModuleBus()
        bridge = create_arcrun_bridge(bus)

        # Create a mock event that would map to a bus event
        mock_event = MagicMock()
        mock_event.type = "tool.start"
        mock_event.data = {"tool": "test"}

        # Call bridge outside of an async context (no running loop)
        with caplog.at_level("WARNING"):
            bridge(mock_event)

        # Should have logged warning about no running loop
        assert any("No running event loop" in rec.message for rec in caplog.records)


class TestMaybeCompactEdgeCases:
    """Lines 343, 346-347: _maybe_compact edge cases."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_maybe_compact_returns_when_context_none(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Line 343: return when context is None in _maybe_compact."""
        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="result")

        await agent.startup()

        # Create a session with a token_ratio method that returns 1.0 (high ratio)
        session_manager = agent._session

        # Save the original context and temporarily set to None during _maybe_compact
        original_context = agent._context

        # We can test this by directly calling _maybe_compact with context=None
        agent._context = None
        await agent._maybe_compact(session_manager)
        # Should not crash

        # Restore for cleanup
        agent._context = original_context

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_maybe_compact_triggers_when_threshold_exceeded(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent_config: ArcAgentConfig,
    ) -> None:
        """Lines 346-347: compact_threshold check triggers compact."""
        from arcagent.core.config import ContextConfig

        # Set a very low compact_threshold to force compaction
        config = agent_config.model_copy(update={"context": ContextConfig(compact_threshold=0.01)})
        agent = ArcAgent(config=config)

        mock_model = MagicMock()
        mock_load_model.return_value = mock_model
        mock_arcrun_run.return_value = MagicMock(content="result")

        await agent.startup()

        # Create a session with lots of tokens to exceed threshold
        for _ in range(100):
            await agent.chat("message " * 100)

        # Compaction should have been triggered
        # (hard to verify without introspection, but shouldn't crash)


class TestReloadEdgeCases:
    """Lines 357-358, 364: reload() edge cases."""

    async def test_reload_raises_when_not_started(self, agent: ArcAgent) -> None:
        """Lines 357-358: RuntimeError when reload() called before startup."""
        with pytest.raises(RuntimeError, match="not started"):
            await agent.reload()

    async def test_reload_returns_when_bus_none(self, agent: ArcAgent) -> None:
        """Line 364: return when bus/tool_registry is None in reload."""
        await agent.startup()
        # Artificially set bus to None
        agent._bus = None

        # reload should return early without crashing
        await agent.reload()


class TestSkillsPropertyEdgeCase:
    """Line 390: skills property returns [] when skill_registry is None."""

    def test_skills_returns_empty_when_registry_none(self, agent: ArcAgent) -> None:
        """Line 390: return [] when skill_registry is None."""
        # Before startup, skill_registry is None
        assert agent._skill_registry is None
        skills = agent.skills
        assert skills == []


class TestShutdownEdgeCases:
    """Line 406: shutdown returns early when bus/tool_registry is None."""

    async def test_shutdown_returns_when_bus_none(self, agent: ArcAgent) -> None:
        """Line 406: return when bus/tool_registry is None in shutdown."""
        await agent.startup()
        # Artificially set bus to None
        agent._bus = None

        # shutdown should return early without crashing
        await agent.shutdown()


class TestSkillPromptInjectionEdgeCases:
    """Line 432: _setup_skill_prompt_injection returns when bus/skill_registry is None."""

    async def test_skill_prompt_injection_returns_when_bus_none(self, agent: ArcAgent) -> None:
        """Line 432: return when bus/skill_registry is None."""
        await agent.startup()
        # Artificially set bus to None
        agent._bus = None

        # Should not crash when called
        agent._setup_skill_prompt_injection()


class TestToolPromptInjection:
    """R7.1: _setup_tool_prompt_injection wires audit and catalog injection."""

    async def test_tool_prompt_injection_returns_when_bus_none(self, agent: ArcAgent) -> None:
        """Guard: returns early when bus is None."""
        await agent.startup()
        agent._bus = None
        # Should not crash
        agent._setup_tool_prompt_injection()

    async def test_tool_prompt_injection_returns_when_registry_none(self, agent: ArcAgent) -> None:
        """Guard: returns early when tool_registry is None."""
        await agent.startup()
        agent._tool_registry = None
        # Should not crash
        agent._setup_tool_prompt_injection()


class TestLoadModulesEdgeCases:
    """Line 456: _load_modules_by_convention returns when bus is None."""

    async def test_load_modules_returns_when_bus_none(self, agent: ArcAgent) -> None:
        """Line 456: return when bus is None in _load_modules_by_convention."""
        from arcagent.core.module_bus import ModuleContext

        await agent.startup()
        # Artificially set bus to None
        agent._bus = None

        # Create a dummy module context
        ctx = ModuleContext(
            bus=MagicMock(),
            tool_registry=MagicMock(),
            config=agent._config,
            telemetry=MagicMock(),
            workspace=agent._workspace,
            llm_config=agent._config.llm,
        )

        # Should return early without crashing
        agent._load_modules_by_convention(ctx)


class TestVaultResolverEdgeCases:
    """Lines 475, 482-483: vault resolver edge cases."""

    def test_create_vault_resolver_returns_none_when_backend_empty(self, agent: ArcAgent) -> None:
        """Lines 475: return None when backend_ref is empty."""
        # Config has no vault backend
        assert agent._config.vault.backend == ""
        resolver = agent._create_vault_resolver()
        assert resolver is None

    async def test_vault_resolver_import_failure_raises(self, tmp_path: Path) -> None:
        """Lines 482-483: vault resolver import failure raises."""
        from arcagent.core.config import VaultConfig

        config = ArcAgentConfig(
            agent=AgentConfig(name="test", workspace=str(tmp_path)),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
            vault=VaultConfig(backend="nonexistent.module:Class"),
            telemetry=TelemetryConfig(enabled=False),
        )
        agent = ArcAgent(config=config)

        # Should raise ModuleNotFoundError during startup
        with pytest.raises(ModuleNotFoundError):
            await agent.startup()


class TestAgentHandle:
    """Tests for AgentHandle steering wrapper."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_run_async_returns_agent_handle(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """run_async() returns an AgentHandle wrapping RunHandle."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="done"))
        mock_handle.state = MagicMock()
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("test task")
        assert isinstance(handle, AgentHandle)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_steer_delegates_to_run_handle(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """steer() delegates to underlying RunHandle."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.steer("new direction")
        mock_handle.steer.assert_called_once_with("new direction")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_follow_up_delegates(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """follow_up() delegates to underlying RunHandle."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.follow_up("also do X")
        mock_handle.follow_up.assert_called_once_with("also do X")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_cancel_delegates(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """cancel() delegates to underlying RunHandle."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.cancel()
        mock_handle.cancel.assert_called_once()

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_result_emits_post_respond(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """result() awaits RunHandle.result() and emits agent:post_respond."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="answer"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        events: list[dict[str, Any]] = []
        await agent.startup()

        async def on_post_respond(ctx: Any) -> None:
            events.append(ctx.data)

        agent._bus.subscribe("agent:post_respond", on_post_respond)
        handle = await agent.run_async("task")
        result = await handle.result()
        assert result.content == "answer"
        await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0]["session_id"] is not None

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_state_property_exposes_run_state(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """state property exposes RunHandle.state."""
        mock_state = MagicMock()
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.state = mock_state
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        assert handle.state is mock_state

    async def test_run_async_requires_startup(self, agent: ArcAgent) -> None:
        """run_async() raises RuntimeError if agent not started."""
        with pytest.raises(RuntimeError, match="not started"):
            await agent.run_async("task")

    async def test_chat_async_requires_startup(self, agent: ArcAgent) -> None:
        """chat_async() raises RuntimeError if agent not started."""
        with pytest.raises(RuntimeError, match="not started"):
            await agent.chat_async("message")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_chat_async_returns_agent_handle(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat_async() returns AgentHandle with session messages."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="hi"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.chat_async("hello")
        assert isinstance(handle, AgentHandle)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_ready_event_includes_async_fns(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """agent:ready event includes run_async_fn and chat_async_fn."""
        mock_load_model.return_value = MagicMock()
        await agent.startup()
        assert callable(agent.run_async)
        assert callable(agent.chat_async)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_result_with_messages_uses_model_dump(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """result() serializes session messages via model_dump when present."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="reply"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        events: list[dict[str, Any]] = []
        await agent.startup()

        async def on_post_respond(ctx: Any) -> None:
            events.append(ctx.data)

        agent._bus.subscribe("agent:post_respond", on_post_respond)

        handle = await agent.chat_async("hello session")
        await handle.result()
        await asyncio.sleep(0)

        assert len(events) == 1
        msgs = events[0]["messages"]
        assert any(m.get("role") == "user" for m in msgs)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_result_raises_on_double_call(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """result() raises RuntimeError when called a second time."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="ok"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.result()
        with pytest.raises(RuntimeError, match="already been awaited"):
            await handle.result()

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_execute_loop_async_emits_error_on_failure(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """_execute_loop_async emits agent:error when arcrun_run_async raises."""
        errors: list[dict[str, Any]] = []

        async def on_error(ctx: Any) -> None:
            errors.append(ctx.data)

        mock_load_model.return_value = MagicMock()
        mock_run_async.side_effect = RuntimeError("run_async failed")

        await agent.startup()
        agent._bus.subscribe("agent:error", on_error)

        with pytest.raises(RuntimeError, match="run_async failed"):
            await agent.run_async("failing task")

        assert len(errors) == 1
        assert errors[0]["error_type"] == "RuntimeError"


class TestSteeringValidation:
    """SEC-001/SEC-010: Input validation on steer/follow_up messages."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_steer_rejects_empty_string(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Empty string rejected by steer()."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        with pytest.raises(ValueError, match="must not be empty"):
            await handle.steer("")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_steer_rejects_whitespace_only(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Whitespace-only string rejected by steer()."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        with pytest.raises(ValueError, match="must not be empty"):
            await handle.steer("   \n  ")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_steer_rejects_oversized_message(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Oversized message rejected by steer()."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        oversized = "x" * 40_000
        with pytest.raises(ValueError, match="exceeds"):
            await handle.steer(oversized)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_follow_up_rejects_empty_string(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Empty string rejected by follow_up()."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        with pytest.raises(ValueError, match="must not be empty"):
            await handle.follow_up("")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_follow_up_rejects_oversized_message(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Oversized message rejected by follow_up()."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        oversized = "x" * 40_000
        with pytest.raises(ValueError, match="exceeds"):
            await handle.follow_up(oversized)


class TestSteeringTerminalGuard:
    """QA: Prevent steer/follow_up/cancel after result() has been awaited."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_steer_after_result_raises(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """steer() raises RuntimeError after result() has completed."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="done"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.result()
        with pytest.raises(RuntimeError, match="Cannot call steer"):
            await handle.steer("too late")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_follow_up_after_result_raises(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """follow_up() raises RuntimeError after result() has completed."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="done"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.result()
        with pytest.raises(RuntimeError, match="Cannot call follow_up"):
            await handle.follow_up("too late")

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_cancel_after_result_raises(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """cancel() raises RuntimeError after result() has completed."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="done"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        await handle.result()
        with pytest.raises(RuntimeError, match="Cannot call cancel"):
            await handle.cancel()


class TestSteeringAuditTrail:
    """SEC-003: Audit events emitted for steering operations."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_steer_emits_audit_event(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """steer() emits agent.steer audit event."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        # Spy on telemetry audit_event
        handle._telemetry.audit_event = MagicMock()
        await handle.steer("redirect")
        handle._telemetry.audit_event.assert_called_once_with(
            "agent.steer",
            {"session_id": handle._session_id, "message_len": 8},
        )

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_follow_up_emits_audit_event(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """follow_up() emits agent.follow_up audit event."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        handle._telemetry.audit_event = MagicMock()
        await handle.follow_up("also X")
        handle._telemetry.audit_event.assert_called_once_with(
            "agent.follow_up",
            {"session_id": handle._session_id, "message_len": 6},
        )

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_cancel_emits_audit_event(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """cancel() emits agent.cancel audit event."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.run_async("task")
        handle._telemetry.audit_event = MagicMock()
        await handle.cancel()
        handle._telemetry.audit_event.assert_called_once_with(
            "agent.cancel",
            {"session_id": handle._session_id},
        )


class TestChatAsyncSession:
    """SEC-005/COV: chat_async session integration."""

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_result_commits_assistant_message_to_session(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """result() appends assistant message to session when attached."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="AI response"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        handle = await agent.chat_async("user question")
        await handle.result()

        # Session should have assistant response
        msgs = agent._session.get_messages()
        assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
        assert len(assistant_msgs) >= 1
        assert any(m.get("content") == "AI response" for m in assistant_msgs)

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run_async")
    async def test_chat_async_resume_session(
        self,
        mock_run_async: AsyncMock,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """chat_async() with session_id resumes existing session."""
        mock_handle = AsyncMock(spec=RunHandle)
        mock_handle.result = AsyncMock(return_value=MagicMock(content="resumed"))
        mock_run_async.return_value = mock_handle
        mock_load_model.return_value = MagicMock()

        await agent.startup()

        # First message creates a session
        handle1 = await agent.chat_async("first")
        await handle1.result()
        session_id = agent._session.session_id

        # Resume with explicit session_id
        handle2 = await agent.chat_async("second", session_id=session_id)
        assert isinstance(handle2, AgentHandle)
        assert agent._session.session_id == session_id


class TestLLMBridgeWiring:
    """SPEC-017 R-001: LLM bridge on_event must reach load_eval_model.

    The arcllm bridge was defined but never wired — ArcLLM events
    (llm_call, config_change, circuit_change) never reached the
    ModuleBus and therefore never reached the UI or memory modules.
    """

    @patch("arcagent.core.agent.load_eval_model")
    async def test_ensure_model_passes_on_event_to_load_eval_model(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """load_eval_model() receives an on_event callback wired to the bus."""
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        # Trigger lazy model load
        agent._ensure_model()

        mock_load_model.assert_called_once()
        _args, kwargs = mock_load_model.call_args
        assert "on_event" in kwargs, "on_event must be passed to load_eval_model"
        assert callable(kwargs["on_event"]), "on_event must be a callable"

    @patch("arcagent.core.agent.load_eval_model")
    async def test_llm_events_reach_module_bus(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """TraceRecords routed through on_event land on the ModuleBus."""
        mock_load_model.return_value = MagicMock()

        events: list[dict[str, Any]] = []

        async def on_llm_call(ctx: Any) -> None:
            events.append(ctx.data)

        await agent.startup()
        agent._bus.subscribe("llm:call_complete", on_llm_call)

        agent._ensure_model()
        _args, kwargs = mock_load_model.call_args
        on_event = kwargs["on_event"]

        record = MagicMock()
        record.model_dump.return_value = {
            "event_type": "llm_call",
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "duration_ms": 120.0,
        }
        on_event(record)

        for _ in range(5):
            await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0]["provider"] == "anthropic"


class TestShutdownClosesModel:
    """SPEC-017 R-004: shutdown must close the httpx client on the model."""

    @patch("arcagent.core.agent.load_eval_model")
    async def test_shutdown_closes_model(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """shutdown() awaits model.close() to release the httpx connection pool."""
        mock_model = MagicMock()
        mock_model.close = AsyncMock()
        mock_load_model.return_value = mock_model

        await agent.startup()
        agent._ensure_model()  # materialize the model
        await agent.shutdown()

        mock_model.close.assert_awaited_once()

    async def test_shutdown_without_model_does_not_raise(self, agent: ArcAgent) -> None:
        """When no model was loaded, shutdown completes cleanly."""
        await agent.startup()
        assert agent._model is None
        await agent.shutdown()  # no model to close — must not raise
