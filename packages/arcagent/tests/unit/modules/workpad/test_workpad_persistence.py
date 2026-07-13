"""Workpad cadence-persistence + idle-flush tests.

The production box restarts every 1-5 minutes. An in-memory ``run_count`` reset
on every restart never reached ``every_n_runs``, so ``context.md`` was never
rewritten. These tests lock:

  1. ``run_count`` survives a process restart (persisted to a workspace dotfile).
  2. A long session below the ``every_n_runs`` boundary still flushes once an
     idle gap elapses, and does NOT re-fire without new activity.
  3. A raising ``perform_maintenance`` is logged, not silently swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.workpad import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


def _model(content: str) -> Any:
    model = MagicMock()
    model.invoke = AsyncMock(return_value=SimpleNamespace(content=content))
    return model


def _post_respond(user: str, assistant: str) -> Any:
    return SimpleNamespace(
        data={
            "messages": [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ],
            "session_id": "s1",
            "automated": False,
        }
    )


async def _drain(st: _runtime._State) -> None:
    await asyncio.gather(*list(st.background_tasks), return_exceptions=True)


@pytest.mark.asyncio
class TestCounterPersistence:
    async def test_run_count_survives_restart(self, tmp_path: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(workspace=ws, agent_name="t", config={"every_n_runs": 100})
        for _ in range(3):  # no eval model → never fires; just counts
            await track_runs(_post_respond("a", "b"))
        assert _runtime.state().run_count == 3

        # Simulate a process restart: fresh runtime, same workspace.
        _runtime.reset()
        _runtime.configure(workspace=ws, agent_name="t", config={"every_n_runs": 100})
        assert _runtime.state().run_count == 3

    async def test_corrupt_state_file_defaults_to_zero(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".workpad-state.json").write_text("{ not json", encoding="utf-8")
        _runtime.configure(workspace=ws, agent_name="t")
        assert _runtime.state().run_count == 0


@pytest.mark.asyncio
class TestIdleFlush:
    async def test_idle_gap_flushes_below_threshold(self, tmp_path: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            config={"every_n_runs": 100, "flush_idle_seconds": 900},
        )
        st = _runtime.state()
        st.eval_model = _model("# cockpit\n- open loop")
        # Pretend the last maintenance was long ago (idle gap).
        st.last_maintenance_ts = time.time() - 100_000

        await track_runs(_post_respond("did work", "ok"))  # run 1 of 100
        await _drain(st)

        assert (ws / "context.md").read_text(encoding="utf-8").startswith("# cockpit")
        st.eval_model.invoke.assert_awaited_once()

    async def test_no_refire_without_new_activity(self, tmp_path: Path) -> None:
        from arcagent.modules.workpad.capabilities import _should_maintain

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            config={"every_n_runs": 100, "flush_idle_seconds": 900},
        )
        st = _runtime.state()
        st.run_count = 5
        st.runs_at_last_maintenance = 5  # nothing new since the last flush
        st.last_maintenance_ts = time.time() - 100_000  # idle elapsed
        assert _should_maintain(st) is False

    async def test_idle_flush_fires_exactly_once(self, tmp_path: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            config={"every_n_runs": 100, "flush_idle_seconds": 900},
        )
        st = _runtime.state()
        st.eval_model = _model("# cockpit\n- loop")
        st.last_maintenance_ts = time.time() - 100_000

        await track_runs(_post_respond("a", "b"))  # idle gap → fires, resets clock
        await _drain(st)
        await track_runs(_post_respond("c", "d"))  # clock now fresh → no fire
        await _drain(st)

        st.eval_model.invoke.assert_awaited_once()


@pytest.mark.asyncio
class TestErrorLogging:
    async def test_maintenance_error_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from arcagent.modules.workpad.capabilities import _safe_maintain

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(workspace=ws, agent_name="t")
        st = _runtime.state()
        model = MagicMock()
        model.invoke = AsyncMock(side_effect=RuntimeError("boom"))
        with caplog.at_level(logging.WARNING):
            await _safe_maintain(st, model, "activity")
        assert any("maintenance failed" in r.message for r in caplog.records)
