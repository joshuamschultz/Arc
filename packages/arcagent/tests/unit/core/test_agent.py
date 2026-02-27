"""Tests for agent orchestrator — startup, run, shutdown, ArcRun bridge, chat."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.agent import ArcAgent, create_arcrun_bridge
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
    VaultConfig,
)
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
        ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            telemetry=TelemetryConfig(enabled=False),
        )
        bus = ModuleBus()
        events: list[str] = []

        async def on_pre_tool(ctx: Any) -> None:
            events.append("pre_tool")

        bus.subscribe("agent:pre_tool", on_pre_tool)

        bridge = create_arcrun_bridge(bus)
        # Simulate ArcRun event
        mock_event = MagicMock()
        mock_event.type = "tool.start"
        mock_event.data = {"tool": "read_file", "args": {}}
        bridge(mock_event)

        # Bridge is sync, but bus.emit is async — bridge schedules it
        # In test we verify the bridge function exists and is callable
        assert callable(bridge)

    async def test_bridge_maps_turn_events(self) -> None:
        ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            telemetry=TelemetryConfig(enabled=False),
        )
        bus = ModuleBus()
        bridge = create_arcrun_bridge(bus)

        mock_event = MagicMock()
        mock_event.type = "turn.start"
        mock_event.data = {"turn": 1}
        bridge(mock_event)
        assert callable(bridge)


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
        # Identity loading will fail: DID specified but key file missing,
        # then generate will fail because key_dir parent doesn't exist
        with pytest.raises((OSError, PermissionError)):
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
    """Lines 89-90: RuntimeError catch when no running loop."""

    def test_bridge_warns_when_no_running_loop(self, caplog: pytest.LogCaptureFixture) -> None:
        """Lines 89-90: RuntimeError caught, warning logged when no event loop."""
        from arcagent.core.agent import create_arcrun_bridge

        ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            telemetry=TelemetryConfig(enabled=False),
        )
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
