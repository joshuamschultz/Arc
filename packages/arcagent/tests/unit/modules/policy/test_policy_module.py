"""Tests for PolicyModule — Module Bus integration and event handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.config import EvalConfig
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.policy.policy_module import PolicyModule


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_module(
    workspace: Path,
    *,
    eval_config: EvalConfig | None = None,
    config: dict[str, Any] | None = None,
    llm_config: Any | None = None,
) -> PolicyModule:
    return PolicyModule(
        config=config or {},
        eval_config=eval_config or EvalConfig(),
        telemetry=_make_telemetry(),
        workspace=workspace,
        llm_config=llm_config,
    )


def _make_ctx(event: str, data: dict[str, Any] | None = None) -> EventContext:
    return EventContext(
        event=event,
        data=data or {},
        agent_did="did:key:test",
        trace_id="trace-test",
    )


def _make_module_ctx(bus: ModuleBus, workspace: Path) -> ModuleContext:
    return ModuleContext(
        bus=bus,
        tool_registry=MagicMock(),
        config=MagicMock(),
        telemetry=_make_telemetry(),
        workspace=workspace,
        llm_config=MagicMock(model="test/model"),
    )


class TestModuleName:
    """PolicyModule.name property."""

    def test_name_is_policy(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        assert mod.name == "policy"


class TestStartupRegistration:
    """Startup registers event handlers on the bus."""

    @pytest.mark.asyncio()
    async def test_startup_subscribes_events(self, tmp_path: Path) -> None:
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        mod = _make_module(tmp_path)
        ctx = _make_module_ctx(bus, tmp_path)
        await mod.startup(ctx)

        assert bus.handler_count("agent:post_respond") == 1
        assert bus.handler_count("agent:assemble_prompt") == 1
        assert bus.handler_count("agent:shutdown") == 1


class TestAssemblePrompt:
    """Policy injection into system prompt via assemble_prompt."""

    @pytest.mark.asyncio()
    async def test_injects_policy_when_file_exists(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        policy_path = tmp_path / "policy.md"
        policy_path.write_text("# Policy\n\n- [P01] Test rule {score:5}")

        sections: dict[str, str] = {}
        ctx = _make_ctx("agent:assemble_prompt", {"sections": sections})
        await mod._on_assemble_prompt(ctx)

        assert "policy" in sections
        assert "Test rule" in sections["policy"]

    @pytest.mark.asyncio()
    async def test_skips_when_no_policy_file(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        sections: dict[str, str] = {}
        ctx = _make_ctx("agent:assemble_prompt", {"sections": sections})
        await mod._on_assemble_prompt(ctx)

        assert "policy" not in sections

    @pytest.mark.asyncio()
    async def test_skips_when_policy_empty(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        policy_path = tmp_path / "policy.md"
        policy_path.write_text("")

        sections: dict[str, str] = {}
        ctx = _make_ctx("agent:assemble_prompt", {"sections": sections})
        await mod._on_assemble_prompt(ctx)

        assert "policy" not in sections

    @pytest.mark.asyncio()
    async def test_skips_when_sections_missing(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        ctx = _make_ctx("agent:assemble_prompt", {})
        # Should not raise
        await mod._on_assemble_prompt(ctx)

    @pytest.mark.asyncio()
    async def test_skips_when_sections_not_dict(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        ctx = _make_ctx("agent:assemble_prompt", {"sections": "not-a-dict"})
        await mod._on_assemble_prompt(ctx)


class TestPostRespondNoModel:
    """post_respond does nothing when eval model unavailable."""

    @pytest.mark.asyncio()
    async def test_no_model_skips(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        ctx = _make_ctx(
            "agent:post_respond",
            {"messages": [{"role": "user", "content": "hi"}]},
        )
        # No model configured, no llm_config — should skip silently
        await mod._on_post_respond(ctx)
        assert mod._turn_count == 0  # Never incremented

    @pytest.mark.asyncio()
    async def test_empty_messages_skips(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        ctx = _make_ctx("agent:post_respond", {"messages": []})
        await mod._on_post_respond(ctx)
        assert mod._turn_count == 0


class TestPostRespondTurnCounting:
    """Periodic evaluation fires at configured interval."""

    @pytest.mark.asyncio()
    async def test_turn_count_increments(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        mock_model = AsyncMock()

        with patch.object(mod, "_get_eval_model", return_value=mock_model):
            ctx = _make_ctx(
                "agent:post_respond",
                {"messages": [{"role": "user", "content": "hi"}]},
            )
            await mod._on_post_respond(ctx)
            assert mod._turn_count == 1

    @pytest.mark.asyncio()
    async def test_eval_fires_at_interval(self, tmp_path: Path) -> None:
        config = {"eval_interval_turns": 2}
        mod = _make_module(tmp_path, config=config)
        mock_model = AsyncMock()

        with (
            patch.object(mod, "_get_eval_model", return_value=mock_model),
            patch(
                "arcagent.modules.policy.policy_module.spawn_background"
            ) as mock_spawn,
        ):
            messages = [{"role": "user", "content": "hi"}]

            # Turn 1 — no eval
            ctx = _make_ctx("agent:post_respond", {"messages": messages})
            await mod._on_post_respond(ctx)
            assert mock_spawn.call_count == 0

            # Turn 2 — eval fires
            ctx = _make_ctx("agent:post_respond", {"messages": messages})
            await mod._on_post_respond(ctx)
            assert mock_spawn.call_count == 1


class TestShutdownEval:
    """Shutdown triggers final policy evaluation."""

    @pytest.mark.asyncio()
    async def test_shutdown_evaluates_session(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        mock_model = AsyncMock()
        mod._session_messages = [{"role": "user", "content": "test"}]

        with (
            patch.object(mod, "_get_eval_model", return_value=mock_model),
            patch.object(mod, "_safe_evaluate", new_callable=AsyncMock) as mock_eval,
        ):
            ctx = _make_ctx("agent:shutdown", {"session_id": "sess-1"})
            await mod._on_shutdown(ctx)
            mock_eval.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_skips_without_messages(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)

        with patch.object(
            mod, "_safe_evaluate", new_callable=AsyncMock
        ) as mock_eval:
            ctx = _make_ctx("agent:shutdown", {})
            await mod._on_shutdown(ctx)
            mock_eval.assert_not_called()

    @pytest.mark.asyncio()
    async def test_shutdown_skips_without_model(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        mod._session_messages = [{"role": "user", "content": "test"}]

        with patch.object(
            mod, "_safe_evaluate", new_callable=AsyncMock
        ) as mock_eval:
            ctx = _make_ctx("agent:shutdown", {})
            await mod._on_shutdown(ctx)
            mock_eval.assert_not_called()


class TestSafeEvaluate:
    """Error handling in _safe_evaluate."""

    @pytest.mark.asyncio()
    async def test_skip_behavior_suppresses_errors(self, tmp_path: Path) -> None:
        eval_config = EvalConfig(fallback_behavior="skip")
        mod = _make_module(tmp_path, eval_config=eval_config)

        with patch.object(
            mod._engine, "evaluate", side_effect=RuntimeError("eval failed")
        ):
            # Should not raise
            await mod._safe_evaluate(
                [{"role": "user", "content": "test"}],
                AsyncMock(),
            )

    @pytest.mark.asyncio()
    async def test_error_behavior_raises(self, tmp_path: Path) -> None:
        eval_config = EvalConfig(fallback_behavior="error")
        mod = _make_module(tmp_path, eval_config=eval_config)

        with patch.object(
            mod._engine, "evaluate", side_effect=RuntimeError("eval failed")
        ):
            with pytest.raises(RuntimeError, match="eval failed"):
                await mod._safe_evaluate(
                    [{"role": "user", "content": "test"}],
                    AsyncMock(),
                )


class TestModuleShutdown:
    """PolicyModule.shutdown cancels background tasks."""

    @pytest.mark.asyncio()
    async def test_shutdown_cancels_tasks(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)

        async def _never_finish() -> None:
            await asyncio.sleep(999)

        task = asyncio.create_task(_never_finish())
        mod._background_tasks.add(task)

        await mod.shutdown()
        assert task.cancelled()

    @pytest.mark.asyncio()
    async def test_shutdown_empty_tasks_ok(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        await mod.shutdown()  # Should not raise


class TestGetEvalModel:
    """Lazy eval model loading via shared helper."""

    def test_caches_model(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        mod._eval_model = MagicMock()  # Pre-cached
        result = mod._get_eval_model()
        assert result is mod._eval_model

    def test_returns_none_without_config(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path)
        # No provider, no model, no llm_config
        result = mod._get_eval_model()
        assert result is None


class TestConfigValidation:
    """PolicyConfig with extra="forbid" catches typos."""

    def test_valid_config(self, tmp_path: Path) -> None:
        mod = _make_module(tmp_path, config={"eval_interval_turns": 5})
        assert mod._config.eval_interval_turns == 5

    def test_invalid_config_key_raises(self, tmp_path: Path) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_module(tmp_path, config={"eval_intervall_turns": 5})  # typo
