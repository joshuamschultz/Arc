"""Owned supervision for ArcAgent background tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any


class BackgroundTaskSupervisor:
    """Own tasks, retrieve failures, and drain them deterministically."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._logger = logger or logging.getLogger("arcagent.background_tasks")

    def create(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Start and own ``coroutine`` until its result is retrieved."""
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        return task

    def _completed(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException:  # reason: callback must retrieve every terminal outcome
            self._logger.exception("Background task %s failed", task.get_name())

    async def drain(self) -> None:
        """Cancel and await every owned task."""
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)

    @property
    def task_count(self) -> int:
        """Number of tasks still owned by this supervisor."""
        return len(self._tasks)
