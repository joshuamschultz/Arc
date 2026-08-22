"""Headless lifecycle ownership for one :class:`WorkflowRunner`."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .runner_contracts import RunRecord

_logger = logging.getLogger(__name__)


class RunnerLifecycle(Protocol):
    """The deterministic runner operations a service owns."""

    async def run_forever(self, *, interval: float = 5.0) -> None:
        """Advance active workflow runs until cancelled."""

    async def aclose(self) -> None:
        """Release runner-owned resources."""

    async def wait_for_terminal(self, run_id: str) -> RunRecord:
        """Wait until the service-owned loop settles one run."""


class WorkflowRunnerService:
    """Keep exactly one runner alive until an explicit shutdown."""

    _active: WorkflowRunnerService | None = None

    def __init__(self, runner: RunnerLifecycle, *, interval: float = 1.0) -> None:
        if interval <= 0:
            raise ValueError("runner interval must be positive")
        self._runner = runner
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def running(self) -> bool:
        """Whether this service currently owns a live runner task."""
        return self._task is not None and not self._task.done()

    @classmethod
    def active(cls) -> WorkflowRunnerService | None:
        """Return the process-owned service, when one exists."""
        return cls._active

    async def start(self) -> None:
        """Start the runner and reserve the process lifecycle slot."""
        if WorkflowRunnerService._active is not None:
            raise RuntimeError("a workflow runner service is already active in this process")
        if self._stopped:
            raise RuntimeError("a stopped workflow runner service cannot be restarted")
        WorkflowRunnerService._active = self
        self._task = asyncio.create_task(self._run(), name="arcflow:runner")

    async def _run(self) -> None:
        try:
            await self._runner.run_forever(interval=self._interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("workflow runner service stopped unexpectedly")

    async def stop(self) -> None:
        """Cancel the run loop, close its runner, and release the slot."""
        if self._stopped:
            return
        self._stopped = True
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await self._runner.aclose()
        except Exception:
            _logger.exception("workflow runner close failed")
        finally:
            if WorkflowRunnerService._active is self:
                WorkflowRunnerService._active = None

    async def wait_for_terminal(self, run_id: str) -> RunRecord:
        """Wait without manually advancing the runner frontier."""
        if not self.running:
            raise RuntimeError("workflow runner service is not running")
        return await self._runner.wait_for_terminal(run_id)


__all__ = ["RunnerLifecycle", "WorkflowRunnerService"]
