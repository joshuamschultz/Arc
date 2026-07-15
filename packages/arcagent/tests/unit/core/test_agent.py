"""Tests for agent orchestrator — startup, run, shutdown, ArcRun/ArcLLM bridges."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun import StreamEvent, TokenEvent, TurnEndEvent

from arcagent.core.agent import (
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


async def _fake_stream(*tokens: str) -> AsyncIterator[StreamEvent]:
    for t in tokens:
        yield TokenEvent(text=t)
    yield TurnEndEvent(final_text="".join(tokens))


def _patch_stream(*tokens: str) -> Any:
    async def _factory(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return _fake_stream(*tokens)

    return patch("arcagent.core.agent_dispatch.arcrun_run_stream", side_effect=_factory)


async def _drive(agent: ArcAgent, task: str, key: str = "unit:test") -> None:
    """Consume a streaming run to completion against a keyed session."""
    session = await agent.session(key)
    async for _ in agent.run(task, session=session):
        pass


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
        assert agent._identity is not None
        assert agent._identity.did.startswith("did:arc:testorg:executor/")

    async def test_startup_emits_init_event(self, agent: ArcAgent) -> None:
        events: list[str] = []

        async def on_init(ctx: Any) -> None:
            events.append("init")

        await agent.startup()
        assert agent._bus is not None
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

        assert agent._bus is not None
        agent._bus.subscribe("agent:shutdown", on_shutdown)
        await agent.shutdown()
        assert "shutdown" in events

    async def test_shutdown_without_startup_is_safe(self, agent: ArcAgent) -> None:
        """Shutdown before startup should not raise."""
        await agent.shutdown()  # No-op


class TestRun:
    async def test_run_requires_startup(self, agent: ArcAgent) -> None:
        # session() calls _ensure_started, so opening a session before startup raises.
        with pytest.raises(RuntimeError, match="not started"):
            await agent.session("k")


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
            "event_data": {
                "actor": "operator",
                "changes": {"temperature": {"old": 0.7, "new": 0.3}},
            },
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
    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_pre_respond_emitted_before_run(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        events: list[str] = []

        async def on_pre(ctx: Any) -> None:
            events.append("pre_respond")

        mock_load_model.return_value = MagicMock()

        with _patch_stream("done"):
            await agent.startup()
            assert agent._bus is not None
            agent._bus.subscribe("agent:pre_respond", on_pre)
            await _drive(agent, "test task")
        assert "pre_respond" in events


class TestErrorEvent:
    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_error_event_emitted_on_failure(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_error(ctx: Any) -> None:
            events.append(ctx.data)

        mock_load_model.return_value = MagicMock()

        async def _boom(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            raise RuntimeError("LLM call failed")

        with patch("arcagent.core.agent_dispatch.arcrun_run_stream", side_effect=_boom):
            await agent.startup()
            assert agent._bus is not None
            agent._bus.subscribe("agent:error", on_error)
            with pytest.raises(RuntimeError, match="LLM call failed"):
                await _drive(agent, "failing task")
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
    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_model_loaded_once_across_runs(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Model is loaded once on first run and reused."""
        mock_load_model.return_value = MagicMock()

        with _patch_stream("done"):
            await agent.startup()
            await _drive(agent, "task 1")
            await _drive(agent, "task 2")
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
        with pytest.raises(ValueError, match="Key file not found"):
            await agent.startup()


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

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_maybe_compact_returns_when_context_none(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """maybe_compact returns early when context is None."""
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        session_manager = await agent.session("unit:compact")

        # Save the original context and temporarily set to None during maybe_compact
        original_context = agent._context

        from arcagent.core.agent_dispatch import maybe_compact

        agent._context = None
        await maybe_compact(agent, session_manager)
        # Should not crash

        # Restore for cleanup
        agent._context = original_context

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_maybe_compact_triggers_when_threshold_exceeded(
        self,
        mock_load_model: MagicMock,
        agent_config: ArcAgentConfig,
    ) -> None:
        """compact_threshold check triggers compaction across runs."""
        from arcagent.core.config import ContextConfig

        # Set a very low compact_threshold to force compaction
        config = agent_config.model_copy(update={"context": ContextConfig(compact_threshold=0.01)})
        agent = ArcAgent(config=config)

        mock_load_model.return_value = MagicMock()

        with _patch_stream("result"):
            await agent.startup()
            session = await agent.session("unit:compact-threshold")
            # Drive several turns to accumulate tokens and exceed the threshold.
            for _ in range(5):
                async for _ in agent.run("message " * 100, session=session):
                    pass

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
    """`skills` property returns [] when capability_registry is None."""

    def test_skills_returns_empty_when_registry_none(self, agent: ArcAgent) -> None:
        # Before startup, capability_registry is None
        assert agent._capability_registry is None
        assert agent.skills == []


class TestShutdownEdgeCases:
    """shutdown returns early when bus/tool_registry is None."""

    async def test_shutdown_returns_when_bus_none(self, agent: ArcAgent) -> None:
        await agent.startup()
        agent._bus = None
        await agent.shutdown()


class TestCapabilityPromptInjectionEdgeCases:
    """_setup_capability_prompt_injection returns when bus/registry is None."""

    async def test_capability_prompt_injection_returns_when_bus_none(
        self, agent: ArcAgent
    ) -> None:
        from arcagent.core.agent_lifecycle import setup_capability_prompt_injection

        await agent.startup()
        agent._bus = None
        setup_capability_prompt_injection(agent)  # no-op

    async def test_capability_prompt_injection_returns_when_registry_none(
        self, agent: ArcAgent
    ) -> None:
        from arcagent.core.agent_lifecycle import setup_capability_prompt_injection

        await agent.startup()
        agent._capability_registry = None
        setup_capability_prompt_injection(agent)  # no-op


class TestVaultResolverEdgeCases:
    """Lines 475, 482-483: vault resolver edge cases."""

    def test_create_vault_resolver_returns_none_when_backend_empty(self, agent: ArcAgent) -> None:
        """Lines 475: return None when backend_ref is empty."""
        from arcagent.core.vault_resolver import create_vault_resolver

        # Config has no vault backend
        assert agent._config.vault.backend == ""
        resolver = create_vault_resolver(agent._config)
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

    def test_create_vault_resolver_wraps_backend_in_cache(self) -> None:
        """The instantiated backend is wrapped in a CachedVaultBackend honoring
        config.vault.cache_ttl_seconds (the tested TTL cache is now live)."""
        from arcagent.core.vault.backends.env import EnvBackend
        from arcagent.core.vault.cache import CachedVaultBackend
        from arcagent.core.vault_resolver import create_vault_resolver

        config = ArcAgentConfig(
            agent=AgentConfig(name="test"),
            llm=LLMConfig(model="test/model"),
            vault=VaultConfig(
                backend="arcagent.core.vault.backends.env:EnvBackend",
                cache_ttl_seconds=123,
            ),
        )
        resolver = create_vault_resolver(config)

        assert isinstance(resolver, CachedVaultBackend)
        assert resolver._ttl_seconds == 123
        assert isinstance(resolver.backend, EnvBackend)


class TestLLMBridgeWiring:
    """SPEC-017 R-001: LLM bridge on_event must reach load_eval_model.

    The arcllm bridge was defined but never wired — ArcLLM events
    (llm_call, config_change, circuit_change) never reached the
    ModuleBus and therefore never reached the UI or memory modules.
    """

    @patch("arcagent.core.model_manager.load_eval_model")
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

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_ensure_model_passes_agent_did_to_load_eval_model(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """Task 27 — the agent's own VERIFIED identity DID reaches
        load_eval_model(), so TraceRecords carry a trustworthy agent_did
        rather than only the free-text agent_label."""
        mock_load_model.return_value = MagicMock()

        await agent.startup()
        agent._ensure_model()

        _args, kwargs = mock_load_model.call_args
        assert agent._identity is not None
        assert kwargs.get("agent_did") == agent._identity.did

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_ensure_model_forwards_llm_modules_overrides(
        self,
        mock_load_model: MagicMock,
        agent: ArcAgent,
    ) -> None:
        """LLMConfig.modules per-agent overrides reach load_eval_model.

        Long-context stages (e.g. an approver chewing through 40k+ tokens of
        accumulated handoff JSON) routinely exceed the 180s default
        queue.call_timeout. Agents need to tune arcllm modules from their
        own arcagent.toml without editing global arcllm config.
        """
        mock_load_model.return_value = MagicMock()
        agent._config.llm.modules = {
            "queue": {"call_timeout": 600.0, "max_concurrent": 2},
        }

        await agent.startup()
        agent._ensure_model()

        _args, kwargs = mock_load_model.call_args
        assert kwargs.get("arcllm_modules") == {
            "queue": {"call_timeout": 600.0, "max_concurrent": 2},
        }

    @patch("arcagent.core.model_manager.load_eval_model")
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
        assert agent._bus is not None
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

    @patch("arcagent.core.model_manager.load_eval_model")
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
