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


@pytest.mark.asyncio
async def test_a_callback_bound_in_another_task_still_reaches_the_engine(
    tmp_path: Path,
) -> None:
    """The live failure: the two halves of the binding met in different tasks.

    The capability builds the engine in the task that configured the module;
    `agent:ready` fires wherever the agent started. When those differ, the hook
    set a callback on a state object the engine never reads, and the engine
    waited forever for one that had already arrived — one agent's reminders sat
    enabled and due for days.
    """
    from arcagent.modules.scheduler import _runtime

    fired: list[str] = []

    async def run_fn(prompt: str, **kwargs: Any) -> str:
        fired.append(prompt)
        return "ok"

    _runtime.forget_run_fns()
    store = _due_store(tmp_path)
    engine = _engine(store)
    engine.label = str(tmp_path)
    engine.run_fn_resolver = lambda: _runtime.recall_run_fn(tmp_path)

    await engine.start()
    try:
        await asyncio.sleep(0.15)
        assert fired == [], "nothing may fire before a callback exists"

        # Bound from somewhere else entirely — a different task, no engine in
        # sight — exactly as the agent:ready hook does it.
        await asyncio.create_task(_bind_elsewhere(tmp_path, run_fn))

        await asyncio.sleep(0.4)
    finally:
        await engine.stop()
        _runtime.forget_run_fns()

    assert fired == ["remind me"]


async def _bind_elsewhere(workspace: Path, fn: Any) -> None:
    from arcagent.modules.scheduler import _runtime

    _runtime.remember_run_fn(workspace, fn)


def test_the_lifecycle_offers_the_run_callback_to_modules() -> None:
    """Producer and consumer of the binding, checked against each other.

    The scheduler declares `agent_run_fn` in `configure()`; core offers it in
    the by-signature kwargs. If either side drops it, the module is configured
    with no callback again and nothing fires — the failure this replaced.
    """
    import inspect

    from arcagent.core import agent_lifecycle
    from arcagent.modules.scheduler import _runtime

    source = Path(agent_lifecycle.__file__).read_text(encoding="utf-8")
    assert '"agent_run_fn": agent.run_collected' in source, (
        "core no longer offers the run callback to modules"
    )
    assert "agent_run_fn" in inspect.signature(_runtime.configure).parameters, (
        "the scheduler no longer asks for the run callback"
    )
