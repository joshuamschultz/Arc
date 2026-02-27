"""Tests for memory wiring — eval model lazy init, memory_search registration,
date filtering, and semaphore-limited background tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    EvalConfig,
    LLMConfig,
)
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.memory.markdown_memory import MarkdownMemoryModule


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="anthropic/claude-haiku"),
    )


def _make_module(workspace: Path, **kwargs: Any) -> MarkdownMemoryModule:
    return MarkdownMemoryModule(
        config=kwargs.get("config", {}),
        eval_config=kwargs.get("eval_config", EvalConfig()),
        telemetry=kwargs.get("telemetry", _make_telemetry()),
        workspace=workspace,
    )


def _make_ctx(event: str, data: dict[str, Any] | None = None) -> EventContext:
    return EventContext(
        event=event,
        data=data or {},
        agent_did="did:arc:test",
        trace_id="trace-1",
    )


class TestEvalModelLazyInit:
    """T3.1: Eval model lazy init with fallback."""

    def test_get_eval_model_from_eval_config(self, tmp_path: Path) -> None:
        """When EvalConfig has provider+model, use it."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="anthropic", model="claude-haiku"),
        )
        # Store llm_config as if startup() was called
        module._llm_config = LLMConfig(model="openai/gpt-4o")

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_load.return_value = MagicMock()
            model = module._get_eval_model()
            mock_load.assert_called_once_with("anthropic/claude-haiku")
            assert model is not None

    def test_get_eval_model_falls_back_to_llm_config(self, tmp_path: Path) -> None:
        """When EvalConfig is empty, fall back to agent's LLM config."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="", model=""),
        )
        module._llm_config = LLMConfig(model="anthropic/claude-haiku")

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_load.return_value = MagicMock()
            model = module._get_eval_model()
            mock_load.assert_called_once_with("anthropic/claude-haiku")
            assert model is not None

    def test_llm_config_preserved_from_constructor(self, tmp_path: Path) -> None:
        """llm_config passed to constructor is not clobbered (regression)."""
        llm_cfg = LLMConfig(model="anthropic/claude-haiku")
        module = MarkdownMemoryModule(
            config={},
            eval_config=EvalConfig(provider="", model=""),
            telemetry=_make_telemetry(),
            workspace=tmp_path,
            llm_config=llm_cfg,
        )
        # Must survive construction — was previously overwritten to None
        assert module._llm_config is llm_cfg

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_load.return_value = MagicMock()
            model = module._get_eval_model()
            mock_load.assert_called_once_with("anthropic/claude-haiku")
            assert model is not None

    def test_get_eval_model_caches(self, tmp_path: Path) -> None:
        """Model is cached after first call."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="test", model="cached"),
        )
        module._llm_config = LLMConfig(model="fallback/model")

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            sentinel = MagicMock()
            mock_load.return_value = sentinel

            first = module._get_eval_model()
            second = module._get_eval_model()

            assert first is second
            mock_load.assert_called_once()  # Only loaded once

    async def test_on_post_respond_uses_lazy_init(self, tmp_path: Path) -> None:
        """_on_post_respond no longer returns early when eval_model is None."""
        module = _make_module(
            tmp_path,
            config={"entity_extraction_enabled": True},
            eval_config=EvalConfig(provider="test", model="model"),
        )
        module._llm_config = LLMConfig(model="fallback/model")

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_model = MagicMock()
            mock_load.return_value = mock_model

            ctx = _make_ctx(
                "agent:post_respond",
                {"messages": [{"role": "assistant", "content": "hi"}]},
            )
            await module._on_post_respond(ctx)

            # Should have called _get_eval_model (lazy init)
            mock_load.assert_called_once()
            # Entity extraction should have been spawned
            assert len(module._background_tasks) >= 1

        # Cleanup
        await module.shutdown()


class TestMemorySearchRegistration:
    """T3.2: Register memory_search tool."""

    async def test_startup_registers_memory_search(self, tmp_path: Path) -> None:
        """startup() registers memory_search in tool_registry."""
        module = _make_module(tmp_path)
        tool_registry = MagicMock()
        config = _make_config()

        ctx = ModuleContext(
            bus=ModuleBus(),
            tool_registry=tool_registry,
            config=config,
            telemetry=_make_telemetry(),
            workspace=tmp_path,
            llm_config=config.llm,
        )

        await module.startup(ctx)
        tool_registry.register.assert_called_once()

        # Verify the registered tool
        registered_tool = tool_registry.register.call_args[0][0]
        assert registered_tool.name == "memory_search"

    async def test_memory_search_tool_schema(self, tmp_path: Path) -> None:
        """memory_search tool has correct input schema."""
        module = _make_module(tmp_path)
        tool_registry = MagicMock()
        config = _make_config()

        ctx = ModuleContext(
            bus=ModuleBus(),
            tool_registry=tool_registry,
            config=config,
            telemetry=_make_telemetry(),
            workspace=tmp_path,
            llm_config=config.llm,
        )

        await module.startup(ctx)
        registered_tool = tool_registry.register.call_args[0][0]
        props = registered_tool.input_schema["properties"]
        assert "query" in props
        assert "scope" in props
        assert "date_from" in props
        assert "date_to" in props


class TestSemaphoreLimitedBackgroundTasks:
    """T3.4: Semaphore-limited background tasks."""

    async def test_semaphore_limits_concurrency(self, tmp_path: Path) -> None:
        """Background tasks respect semaphore limit (max_concurrent=2)."""
        import logging

        from arcagent.utils.model_helpers import spawn_background

        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(max_concurrent=2),
        )

        active_count = 0
        max_observed = 0

        async def counted_task() -> None:
            nonlocal active_count, max_observed
            active_count += 1
            max_observed = max(max_observed, active_count)
            await asyncio.sleep(0.05)
            active_count -= 1

        logger = logging.getLogger(__name__)

        # Spawn 4 tasks — only 2 should run concurrently
        for _ in range(4):
            spawn_background(
                counted_task(),
                background_tasks=module._background_tasks,
                semaphore=module._semaphore,
                eval_config=module._eval_config,
                logger=logger,
            )

        await asyncio.sleep(0.2)
        assert max_observed <= 2

        await module.shutdown()


class TestGetEvalModelFallback:
    """Test _get_eval_model with different fallback behaviors."""

    def test_get_eval_model_error_on_no_config(self, tmp_path: Path) -> None:
        """When fallback_behavior is 'error' and no config, raises."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="", model="", fallback_behavior="error"),
        )
        module._llm_config = None

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_load.return_value = MagicMock()
            try:
                module._get_eval_model()
                raise AssertionError("Should have raised")
            except RuntimeError as e:
                assert "No eval model config" in str(e)

    def test_get_eval_model_error_on_load_failure(self, tmp_path: Path) -> None:
        """When fallback_behavior is 'error' and load fails, raises."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="test", model="model", fallback_behavior="error"),
        )

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_load.side_effect = RuntimeError("Load failed")
            try:
                module._get_eval_model()
                raise AssertionError("Should have raised")
            except RuntimeError as e:
                assert "Load failed" in str(e)

    def test_get_eval_model_skip_on_load_failure(self, tmp_path: Path) -> None:
        """When fallback_behavior is 'skip' and load fails, returns None."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="test", model="model", fallback_behavior="skip"),
        )

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_load.side_effect = RuntimeError("Load failed")
            result = module._get_eval_model()
            assert result is None


class TestMemorySearchNoResults:
    """Test memory_search when no results found."""

    async def test_memory_search_returns_no_results_message(self, tmp_path: Path) -> None:
        """When search has no results, return empty message."""
        module = _make_module(tmp_path)

        # Mock hybrid_search to return empty
        module._hybrid_search.search = AsyncMock(return_value=[])

        result = await module._handle_memory_search(query="nonexistent")
        assert result == "No memory results found."


class TestOnPostToolIdentityAudit:
    """Test _on_post_tool identity audit path."""

    async def test_post_tool_identity_audit(self, tmp_path: Path) -> None:
        """Post-tool handler captures after state for identity.md."""
        module = _make_module(tmp_path)
        identity_file = tmp_path / "identity.md"
        identity_file.write_text("before content")

        # Pre-tool to capture before
        pre_ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(identity_file)},
            },
        )
        await module._on_pre_tool(pre_ctx)

        # Modify file
        identity_file.write_text("after content")

        # Post-tool to capture after
        post_ctx = _make_ctx(
            "agent:post_tool",
            {
                "tool": "write",
                "args": {"path": str(identity_file)},
            },
        )
        await module._on_post_tool(post_ctx)

        # Audit file should exist
        audit_file = tmp_path / "audit" / "identity-changes.jsonl"
        assert audit_file.exists()


class TestOnPostRespondEmptyMessages:
    """Test _on_post_respond with empty messages."""

    async def test_post_respond_skips_on_empty_messages(self, tmp_path: Path) -> None:
        """_on_post_respond returns early when messages are empty."""
        module = _make_module(
            tmp_path,
            eval_config=EvalConfig(provider="test", model="model"),
        )

        with patch("arcagent.utils.model_helpers.load_eval_model") as mock_load:
            mock_model = MagicMock()
            mock_load.return_value = mock_model

            ctx = _make_ctx("agent:post_respond", {"messages": []})
            await module._on_post_respond(ctx)

            # Should have returned early, no background tasks
            assert len(module._background_tasks) == 0
