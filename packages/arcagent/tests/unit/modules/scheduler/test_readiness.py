"""A scheduler that cannot fire must say so, not park in silence.

The live failure: a reminder stored days earlier sat `enabled: true`,
`run_count: 0`, due since Thursday, and nothing ever ran. The timer loop's
first statement was a bare `await self._ready.wait()` — if the `agent:ready`
hook never bound a run callback, the loop blocked forever, no log line, and
every schedule looked pending in the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore


def _due_store(tmp_path: Path) -> ScheduleStore:
    store = ScheduleStore(tmp_path / "schedules.json")
    due = (datetime.now() - timedelta(days=2)).replace(microsecond=0).isoformat()
    store.add(ScheduleEntry(id="sched_x", type="once", prompt="remind me", at=due, enabled=True))
    return store


def _engine(store: ScheduleStore, run_fn: Any = None) -> SchedulerEngine:
    engine = SchedulerEngine(
        store=store,
        # The interval is whole seconds by config; the loop is driven fast here
        # by patching the engine's own sleep, not by an invalid config.
        config=SchedulerConfig(check_interval_seconds=1),
        telemetry=MagicMock(),
        agent_run_fn=run_fn,
        bus=None,
    )
    # The configured interval is whole seconds; drive the loop at test speed.
    engine._tick_seconds = 0.05
    return engine


@pytest.mark.asyncio
async def test_a_due_schedule_fires_once_a_callback_is_bound(tmp_path: Path) -> None:
    fired: list[str] = []

    async def run_fn(prompt: str, **kwargs: Any) -> str:
        fired.append(prompt)
        return "ok"

    store = _due_store(tmp_path)
    engine = _engine(store, run_fn)
    engine.set_agent_run_fn(run_fn)
    await engine.start()
    try:
        await asyncio.sleep(0.4)
    finally:
        await engine.stop()

    assert fired == ["remind me"]
    assert store.load()[0].metadata.run_count == 1


@pytest.mark.asyncio
async def test_an_unready_engine_says_so_instead_of_going_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silence is the bug: work waiting, no callback, nothing said."""
    from arcagent.modules.scheduler import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "_UNREADY_WARN_SECONDS", 0.05)
    store = _due_store(tmp_path)
    engine = _engine(store)  # never bound — _ready stays unset

    with caplog.at_level(logging.WARNING):
        await engine.start()
        try:
            await asyncio.sleep(0.3)
        finally:
            await engine.stop()

    assert any("no agent run callback" in r.message for r in caplog.records), caplog.text
    # And it consumed nothing while it waited.
    assert store.load()[0].metadata.run_count == 0


@pytest.mark.asyncio
async def test_binding_late_starts_the_ticking(tmp_path: Path) -> None:
    """A callback that arrives after start must not be missed."""
    fired: list[str] = []

    async def run_fn(prompt: str, **kwargs: Any) -> str:
        fired.append(prompt)
        return "ok"

    store = _due_store(tmp_path)
    engine = _engine(store)
    await engine.start()
    try:
        await asyncio.sleep(0.15)
        assert fired == []
        engine.set_agent_run_fn(run_fn)
        await asyncio.sleep(0.4)
    finally:
        await engine.stop()

    assert fired == ["remind me"]
