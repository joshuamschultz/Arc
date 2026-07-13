"""Policy cadence-persistence + idle-flush tests.

Mirrors the workpad fix: the policy ``turn_count`` was an in-memory counter reset
on every process restart, so on a box that restarts every 1-5 minutes it never
reached ``eval_interval_turns`` and policy bullets were never learned. Locks:

  1. ``turn_count`` survives a restart (persisted to a workspace dotfile).
  2. A session below the ``eval_interval_turns`` boundary still evaluates once an
     idle gap elapses, and does NOT re-fire without new activity.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from arcagent.modules.policy import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


def _data() -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": "hi"}], "session_id": "sess-1"}


@pytest.mark.asyncio
class TestCounterPersistence:
    async def test_turn_count_survives_restart(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(workspace=ws, agent_name="t", config={"eval_interval_turns": 100})
        _runtime.state().eval_model = AsyncMock()

        def _close(coro: Any, **_: Any) -> None:
            coro.close()

        with patch.object(policy_caps, "spawn_background", side_effect=_close):
            for _ in range(3):
                await policy_caps.periodic_policy_eval(SimpleNamespace(data=_data()))
        assert _runtime.state().turn_count == 3

        # Simulate a restart: fresh runtime, same workspace.
        _runtime.reset()
        _runtime.configure(workspace=ws, agent_name="t", config={"eval_interval_turns": 100})
        assert _runtime.state().turn_count == 3

    async def test_corrupt_state_file_defaults_to_zero(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".policy-state.json").write_text("nonsense{", encoding="utf-8")
        _runtime.configure(workspace=ws, agent_name="t")
        assert _runtime.state().turn_count == 0


@pytest.mark.asyncio
class TestIdleFlush:
    async def test_idle_gap_evaluates_below_interval(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            config={"eval_interval_turns": 100, "flush_idle_seconds": 900},
        )
        st = _runtime.state()
        st.eval_model = AsyncMock()
        st.last_eval_ts = time.time() - 100_000  # idle gap

        def _close(coro: Any, **_: Any) -> None:
            coro.close()

        with patch.object(policy_caps, "spawn_background", side_effect=_close) as mock_spawn:
            await policy_caps.periodic_policy_eval(SimpleNamespace(data=_data()))
            assert mock_spawn.call_count == 1  # turn 1 of 100, but idle gap fired it

    async def test_no_refire_without_new_activity(self, tmp_path: Path) -> None:
        from arcagent.modules.policy.capabilities import _should_eval

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            config={"eval_interval_turns": 100, "flush_idle_seconds": 900},
        )
        st = _runtime.state()
        st.turn_count = 5
        st.turns_at_last_eval = 5
        st.last_eval_ts = time.time() - 100_000
        assert _should_eval(st) is False


@pytest.mark.asyncio
class TestFlushIdleDefault:
    async def test_default_flush_idle_seconds(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(workspace=ws, agent_name="t")
        assert _runtime.state().config.flush_idle_seconds == 900
