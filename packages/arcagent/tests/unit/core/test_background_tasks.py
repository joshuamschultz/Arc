"""Lifecycle tests for owned background-task supervision."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from arcagent.capabilities.capability_registry import BackgroundTaskEntry, CapabilityRegistry
from arcagent.core.background_tasks import BackgroundTaskSupervisor
from arcagent.tools._decorator import BackgroundTaskMetadata


async def test_failure_is_retrieved_and_logged(caplog) -> None:
    supervisor = BackgroundTaskSupervisor()

    async def fail() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="arcagent.background_tasks"):
        supervisor.create(fail(), name="failing-task")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "Background task failing-task failed" in caplog.text
    assert supervisor.task_count == 0


async def test_drain_cancels_and_awaits_all_tasks() -> None:
    supervisor = BackgroundTaskSupervisor()
    stopped = asyncio.Event()

    async def wait_forever() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = supervisor.create(wait_forever(), name="waiting-task")
    await asyncio.sleep(0)
    await supervisor.drain()

    assert task.cancelled()
    assert stopped.is_set()
    assert supervisor.task_count == 0


async def test_registry_shutdown_drains_supervised_tasks() -> None:
    supervisor = BackgroundTaskSupervisor()
    registry = CapabilityRegistry(task_supervisor=supervisor)

    async def wait_forever(_context) -> None:
        await asyncio.Event().wait()

    entry = BackgroundTaskEntry(
        meta=BackgroundTaskMetadata(name="poll", interval=1),
        fn=wait_forever,
        source_path=Path("/poll.py"),
        scan_root="builtins",
    )
    await registry.register_task(entry)
    await registry.shutdown()

    assert entry.task is None
    assert supervisor.task_count == 0
