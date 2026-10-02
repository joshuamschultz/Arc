"""An uncertain schedule firing keeps the same due slot across restart."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import scheduled_occurrence
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore


@pytest.mark.asyncio
async def test_timeout_preserves_exact_occurrence_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScheduleStore(tmp_path / "schedules.json")
    entry = ScheduleEntry(id="s1", type="interval", prompt="Check status", every_seconds=60)
    store.add(entry)

    async def timeout(_: ScheduleEntry) -> Any:
        raise TimeoutError("response lost")

    first = SchedulerEngine(store, SchedulerConfig(), MagicMock(), timeout)
    monkeypatch.setattr(first, "_dispatch", timeout)
    await first.execute(entry)
    pending = store.get(entry.id)
    assert pending is not None
    assert pending.metadata.pending_due_at is not None
    assert pending.metadata.last_run is None
    due_at = datetime.fromisoformat(pending.metadata.pending_due_at)
    expected = scheduled_occurrence(pending, due_at).run_id

    observed: list[str] = []

    async def recovered(current: ScheduleEntry) -> str:
        assert current.metadata.pending_due_at is not None
        due = datetime.fromisoformat(current.metadata.pending_due_at)
        observed.append(scheduled_occurrence(current, due).run_id)
        return "done"

    second = SchedulerEngine(store, SchedulerConfig(), MagicMock(), recovered)
    monkeypatch.setattr(second, "_dispatch", recovered)
    await second.execute(pending)
    assert observed == [expected]
    complete = store.get(entry.id)
    assert complete is not None
    assert complete.metadata.pending_due_at is None
    assert complete.metadata.run_count == 1
    assert datetime.fromisoformat(complete.metadata.last_run or "").tzinfo is UTC


@pytest.mark.asyncio
async def test_reply_reconciles_on_restart_without_repeating_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScheduleStore(tmp_path / "schedules.json")
    entry = ScheduleEntry(
        id="s1",
        type="interval",
        prompt="Check status",
        every_seconds=60,
        deliver_to="channel://ops",
    )
    store.add(entry)

    async def completed(_: ScheduleEntry) -> str:
        return "answer"

    first = SchedulerEngine(store, SchedulerConfig(), MagicMock(), completed)
    monkeypatch.setattr(first, "_dispatch", completed)
    await first.execute(entry)
    pending = store.get(entry.id)
    assert pending is not None
    run_id = pending.metadata.pending_reply_run_id
    assert run_id is not None

    replay = AsyncMock()
    reply = AsyncMock(return_value="sent")
    second = SchedulerEngine(store, SchedulerConfig(), MagicMock(), replay)
    second.set_reply_delivery(reply, AsyncMock(), AsyncMock(return_value=True))
    await second._tick()

    reply.assert_awaited_once()
    assert reply.await_args.args == (run_id,)
    replay.assert_not_awaited()
    current = store.get(entry.id)
    assert current is not None and current.metadata.pending_reply_run_id is None
