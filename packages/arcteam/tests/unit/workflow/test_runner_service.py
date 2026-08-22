"""Lifecycle ownership for a headless ArcFlow runner."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcteam.workflow.service import WorkflowRunnerService


class _Runner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    async def run_forever(self, *, interval: float = 5.0) -> None:
        del interval
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def aclose(self) -> None:
        self.closed = True

    async def wait_for_terminal(self, run_id: str) -> Any:
        return {"run_id": run_id, "status": "done"}


async def test_service_keeps_runner_alive_until_explicit_shutdown() -> None:
    runner = _Runner()
    service = WorkflowRunnerService(runner, interval=0.01)

    await service.start()
    await asyncio.wait_for(runner.started.wait(), timeout=1)

    assert service.running is True
    await service.stop()

    assert runner.cancelled.is_set()
    assert runner.closed is True
    assert service.running is False


async def test_two_independent_services_can_run_in_one_process() -> None:
    first = WorkflowRunnerService(_Runner())
    second = WorkflowRunnerService(_Runner())
    await first.start()
    await second.start()
    try:
        assert first.running is True
        assert second.running is True
    finally:
        await first.stop()
        await second.stop()


async def test_service_waits_through_the_runner_notification_contract() -> None:
    service = WorkflowRunnerService(_Runner())
    await service.start()
    try:
        assert await service.wait_for_terminal("run-1") == {"run_id": "run-1", "status": "done"}
    finally:
        await service.stop()


async def test_service_shutdown_is_idempotent_after_runner_failure() -> None:
    class _BrokenRunner(_Runner):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("close failed")

    service = WorkflowRunnerService(_BrokenRunner())
    await service.start()

    await service.stop()
    await service.stop()


async def test_runner_crash_unblocks_a_terminal_wait() -> None:
    class _CrashingRunner(_Runner):
        async def run_forever(self, *, interval: float = 5.0) -> None:
            del interval
            raise RuntimeError("runner crashed")

        async def wait_for_terminal(self, run_id: str) -> Any:
            del run_id
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    service = WorkflowRunnerService(_CrashingRunner())
    await service.start()
    with pytest.raises(RuntimeError, match="runner crashed"):
        await service.wait_for_terminal("run-1")
    await service.stop()
