"""Headless lifecycle ownership for one :class:`WorkflowRunner`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
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
    """Keep one composition-owned runner alive until explicit shutdown."""

    def __init__(self, runner: RunnerLifecycle, *, interval: float = 1.0) -> None:
        if interval <= 0:
            raise ValueError("runner interval must be positive")
        self._runner = runner
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        """Whether this service currently owns a live runner task."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start this service's runner exactly once."""
        if self._closed:
            raise RuntimeError("a stopped workflow runner service cannot be restarted")
        if self._task is not None:
            raise RuntimeError("workflow runner service is already running")
        self._task = asyncio.create_task(self._run(), name="arcflow:runner")

    async def _run(self) -> None:
        try:
            await self._runner.run_forever(interval=self._interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("workflow runner service stopped unexpectedly")
            raise

    async def stop(self) -> None:
        """Cancel the run loop, close its runner, and release the slot."""
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger.exception("workflow runner task raised during shutdown")
        try:
            await self._runner.aclose()
        except Exception:
            _logger.exception("workflow runner close failed")

    async def wait_for_terminal(self, run_id: str) -> RunRecord:
        """Wait without manually advancing the runner frontier."""
        runner_task = self._task
        if runner_task is None:
            raise RuntimeError("workflow runner service is not running")
        terminal_task = asyncio.create_task(self._runner.wait_for_terminal(run_id))
        try:
            done, _ = await asyncio.wait(
                {runner_task, terminal_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if runner_task in done:
                runner_task.result()
                raise RuntimeError(
                    "workflow runner stopped before the run reached a terminal state"
                )
            return terminal_task.result()
        finally:
            if not terminal_task.done():
                terminal_task.cancel()
                with suppress(asyncio.CancelledError):
                    await terminal_task


__all__ = ["RunnerLifecycle", "WorkflowRunnerService"]
