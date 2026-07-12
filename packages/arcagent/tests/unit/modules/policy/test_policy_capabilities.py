"""Policy module decorator-form tests — the live self-learning policy path.

``capabilities.py`` exposes the ``@hook``-decorated functions that ARE the
policy module in production. This file verifies:

  1. The hooks register via :class:`CapabilityLoader` against the policy
     module directory at the expected priorities.
  2. ``inject_policy_md`` injects ``policy.md`` into the prompt sections.
  3. ``periodic_policy_eval`` counts turns, fires at the interval, and
     skips automated / no-model / empty-message runs.
  4. ``terminal_policy_eval`` runs a final eval and drains background tasks.
  5. ``_safe_evaluate`` honours ``fallback_behavior`` and ``_eval_model``
     caches; runtime config is validated on ``configure``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.config import EvalConfig
from arcagent.modules.policy import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _runtime.configure(workspace=workspace, agent_name="test")
    return workspace


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_three_hooks_register(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        module_dir = Path(policy_caps.__file__).parent
        # Loader scans .py files in the directory; only capabilities.py
        # has @hook stamps among the policy files.
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("policy", module_dir)], registry=reg)
        await loader.scan_and_register()

        prompt_hooks = await reg.get_hooks("agent:assemble_prompt")
        respond_hooks = await reg.get_hooks("agent:post_respond")
        shutdown_hooks = await reg.get_hooks("agent:shutdown")

        assert any(h.meta.name == "inject_policy_md" for h in prompt_hooks)
        assert any(h.meta.name == "periodic_policy_eval" for h in respond_hooks)
        assert any(h.meta.name == "terminal_policy_eval" for h in shutdown_hooks)

    async def test_hook_priorities_match_legacy(self) -> None:
        from arcagent.modules.policy.capabilities import (
            inject_policy_md,
            periodic_policy_eval,
            terminal_policy_eval,
        )

        assert inject_policy_md._arc_capability_meta.priority == 60  # type: ignore[attr-defined]
        assert periodic_policy_eval._arc_capability_meta.priority == 110  # type: ignore[attr-defined]
        assert terminal_policy_eval._arc_capability_meta.priority == 60  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestInjectPolicyMd:
    async def test_writes_section_when_file_present(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        (configured / "policy.md").write_text("learned lessons here")
        sections: dict[str, str] = {}
        ctx = SimpleNamespace(data={"sections": sections})
        await inject_policy_md(ctx)
        assert sections["policy"] == "learned lessons here"

    async def test_skips_when_file_absent(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        sections: dict[str, str] = {}
        ctx = SimpleNamespace(data={"sections": sections})
        await inject_policy_md(ctx)
        assert "policy" not in sections


@pytest.mark.asyncio
class TestPeriodicPolicyEval:
    async def test_skips_when_no_session_id(self, configured: Path) -> None:
        """Background runs (no session_id) must not advance turn count."""
        from arcagent.modules.policy.capabilities import periodic_policy_eval

        ctx = SimpleNamespace(data={"messages": [{"role": "user"}]})
        await periodic_policy_eval(ctx)
        assert _runtime.state().turn_count == 0


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await inject_policy_md(SimpleNamespace(data={"sections": {}}))


def _configure_with(
    workspace: Path,
    *,
    eval_config: EvalConfig | None = None,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    llm_config: Any = None,
) -> None:
    """Configure the policy runtime with explicit dependencies."""
    _runtime.configure(
        workspace=workspace,
        agent_name="test",
        eval_config=eval_config,
        config=config,
        telemetry=telemetry,
        llm_config=llm_config,
    )


@pytest.mark.asyncio
class TestInjectPolicyMdEdgeCases:
    async def test_skips_when_policy_empty(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        (configured / "policy.md").write_text("")
        sections: dict[str, str] = {}
        await inject_policy_md(SimpleNamespace(data={"sections": sections}))
        assert "policy" not in sections

    async def test_skips_when_sections_missing(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        # Should not raise
        await inject_policy_md(SimpleNamespace(data={}))

    async def test_skips_when_sections_not_dict(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        await inject_policy_md(SimpleNamespace(data={"sections": "not-a-dict"}))


@pytest.mark.asyncio
class TestPeriodicPolicyEvalNoModel:
    async def test_no_model_skips_turn_count(self, configured: Path) -> None:
        """No eval model and no llm_config → skip without advancing turn count."""
        from arcagent.modules.policy.capabilities import periodic_policy_eval

        ctx = SimpleNamespace(
            data={"messages": [{"role": "user", "content": "hi"}], "session_id": "sess-1"}
        )
        await periodic_policy_eval(ctx)
        assert _runtime.state().turn_count == 0

    async def test_empty_messages_skips_turn_count(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import periodic_policy_eval

        _runtime.state().eval_model = AsyncMock()
        ctx = SimpleNamespace(data={"messages": [], "session_id": "sess-1"})
        await periodic_policy_eval(ctx)
        assert _runtime.state().turn_count == 0

    async def test_automated_run_skipped(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import periodic_policy_eval

        _runtime.state().eval_model = AsyncMock()
        ctx = SimpleNamespace(
            data={
                "messages": [{"role": "user", "content": "hi"}],
                "session_id": "sess-1",
                "automated": True,
            }
        )
        await periodic_policy_eval(ctx)
        assert _runtime.state().turn_count == 0


@pytest.mark.asyncio
class TestPeriodicPolicyEvalTurnCounting:
    async def test_turn_count_increments(self, tmp_path: Path) -> None:
        from arcagent.modules.policy.capabilities import periodic_policy_eval

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws)
        _runtime.state().eval_model = AsyncMock()

        ctx = SimpleNamespace(
            data={"messages": [{"role": "user", "content": "hi"}], "session_id": "sess-1"}
        )
        await periodic_policy_eval(ctx)
        assert _runtime.state().turn_count == 1

    async def test_eval_fires_at_interval(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws, config={"eval_interval_turns": 2})
        _runtime.state().eval_model = AsyncMock()

        data = {"messages": [{"role": "user", "content": "hi"}], "session_id": "sess-1"}

        def _close(coro: Any, **_: Any) -> None:
            coro.close()  # avoid "coroutine never awaited" — we assert on the call, not the run

        with patch.object(policy_caps, "spawn_background", side_effect=_close) as mock_spawn:
            await policy_caps.periodic_policy_eval(SimpleNamespace(data=data))
            assert mock_spawn.call_count == 0  # turn 1 — no eval
            await policy_caps.periodic_policy_eval(SimpleNamespace(data=data))
            assert mock_spawn.call_count == 1  # turn 2 — eval fires


@pytest.mark.asyncio
class TestPolicyCadenceDefaults:
    async def test_policy_default_is_fifty(self, configured: Path) -> None:
        assert _runtime.state().config.eval_interval_turns == 50

    async def test_daily_notes_default_is_twenty(self, configured: Path) -> None:
        assert _runtime.state().config.daily_notes_every_turns == 20


@pytest.mark.asyncio
class TestReflectConsolidationCadence:
    """``reflect_on_consolidation`` must throttle to a turn cadence, not fire on
    every consolidation pass (the observed ``<agent>/eval`` cost burn)."""

    async def test_skips_before_cadence(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        st = _runtime.state()
        st.eval_model = AsyncMock()
        st.turn_count = 19  # default daily_notes_every_turns = 20 not yet reached
        ctx = SimpleNamespace(data={"episode_summary": "did work"})
        with patch.object(policy_caps, "reflect_and_curate", new_callable=AsyncMock) as mock:
            await policy_caps.reflect_on_consolidation(ctx)
            mock.assert_not_called()

    async def test_runs_at_cadence_and_records_turn(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        st = _runtime.state()
        st.eval_model = AsyncMock()
        st.turn_count = 20
        ctx = SimpleNamespace(data={"episode_summary": "did work"})
        with patch.object(policy_caps, "reflect_and_curate", new_callable=AsyncMock) as mock:
            await policy_caps.reflect_on_consolidation(ctx)
            mock.assert_called_once()
        assert _runtime.state().last_reflect_turn == 20

    async def test_second_consolidation_within_window_skips(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        st = _runtime.state()
        st.eval_model = AsyncMock()
        st.turn_count = 20
        ctx = SimpleNamespace(data={"episode_summary": "did work"})
        with patch.object(policy_caps, "reflect_and_curate", new_callable=AsyncMock) as mock:
            await policy_caps.reflect_on_consolidation(ctx)  # runs, sets last_reflect_turn=20
            st.turn_count = 39  # only 19 turns later — still inside the window
            await policy_caps.reflect_on_consolidation(ctx)
            assert mock.call_count == 1

    async def test_config_override_changes_gate(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws, config={"daily_notes_every_turns": 5})
        st = _runtime.state()
        st.eval_model = AsyncMock()
        ctx = SimpleNamespace(data={"episode_summary": "did work"})
        with patch.object(policy_caps, "reflect_and_curate", new_callable=AsyncMock) as mock:
            st.turn_count = 4
            await policy_caps.reflect_on_consolidation(ctx)
            assert mock.call_count == 0
            st.turn_count = 5
            await policy_caps.reflect_on_consolidation(ctx)
            assert mock.call_count == 1

    async def test_empty_grounding_never_evals(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        st = _runtime.state()
        st.eval_model = AsyncMock()
        st.turn_count = 500  # well past any cadence
        ctx = SimpleNamespace(data={})  # empty grounding
        with patch.object(policy_caps, "reflect_and_curate", new_callable=AsyncMock) as mock:
            await policy_caps.reflect_on_consolidation(ctx)
            mock.assert_not_called()
        assert _runtime.state().last_reflect_turn == 0  # window slot not consumed


@pytest.mark.asyncio
class TestTerminalPolicyEval:
    async def test_evaluates_session_messages(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import terminal_policy_eval

        st = _runtime.state()
        st.session_messages = [{"role": "user", "content": "test"}]
        st.eval_model = AsyncMock()

        with patch.object(st.engine, "evaluate", new_callable=AsyncMock) as mock_evaluate:
            await terminal_policy_eval(SimpleNamespace(data={"session_id": "sess-1"}))
            mock_evaluate.assert_called_once()

    async def test_skips_without_messages(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        with patch.object(policy_caps, "_safe_evaluate", new_callable=AsyncMock) as mock_eval:
            await policy_caps.terminal_policy_eval(SimpleNamespace(data={}))
            mock_eval.assert_not_called()

    async def test_skips_without_model(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        _runtime.state().session_messages = [{"role": "user", "content": "test"}]

        with patch.object(policy_caps, "_safe_evaluate", new_callable=AsyncMock) as mock_eval:
            await policy_caps.terminal_policy_eval(SimpleNamespace(data={}))
            mock_eval.assert_not_called()

    async def test_cancels_background_tasks(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import terminal_policy_eval

        async def _never_finish() -> None:
            await asyncio.sleep(999)

        task = asyncio.create_task(_never_finish())
        _runtime.state().background_tasks.add(task)

        await terminal_policy_eval(SimpleNamespace(data={}))
        assert task.cancelled()


@pytest.mark.asyncio
class TestSafeEvaluate:
    async def test_skip_behavior_suppresses_errors(self, tmp_path: Path) -> None:
        from arcagent.modules.policy.capabilities import _safe_evaluate

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws, eval_config=EvalConfig(fallback_behavior="skip"))

        with patch.object(
            _runtime.state().engine, "evaluate", side_effect=RuntimeError("eval failed")
        ):
            # Should not raise
            await _safe_evaluate([{"role": "user", "content": "test"}], AsyncMock())

    async def test_error_behavior_raises(self, tmp_path: Path) -> None:
        from arcagent.modules.policy.capabilities import _safe_evaluate

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws, eval_config=EvalConfig(fallback_behavior="error"))

        with patch.object(
            _runtime.state().engine, "evaluate", side_effect=RuntimeError("eval failed")
        ):
            with pytest.raises(RuntimeError, match="eval failed"):
                await _safe_evaluate([{"role": "user", "content": "test"}], AsyncMock())


class TestEvalModelLoading:
    def test_caches_model(self, tmp_path: Path) -> None:
        from arcagent.modules.policy.capabilities import _eval_model

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws)
        cached = MagicMock()
        _runtime.state().eval_model = cached
        assert _eval_model() is cached

    def test_returns_none_without_config(self, tmp_path: Path) -> None:
        from arcagent.modules.policy.capabilities import _eval_model

        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws)  # no provider/model, no llm_config
        assert _eval_model() is None


class TestConfigValidation:
    def test_valid_config(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _configure_with(ws, config={"eval_interval_turns": 5})
        assert _runtime.state().config.eval_interval_turns == 5

    def test_invalid_config_key_raises(self, tmp_path: Path) -> None:
        from pydantic import ValidationError

        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(ValidationError):
            _configure_with(ws, config={"eval_intervall_turns": 5})  # typo
