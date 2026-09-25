"""Report authorization worker lifetime under cancellation and stalled callbacks."""

from __future__ import annotations

import asyncio
import threading

import pytest

from arcui.report_authorization import ReportReadBusyError, ReportReadWorkerPool


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_release_running_worker() -> None:
    pool = ReportReadWorkerPool(max_workers=1, operation_timeout=0.5)
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> str:
        entered.set()
        release.wait(2)
        return "done"

    try:
        task = asyncio.create_task(pool.run(blocked))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ReportReadBusyError):
            await pool.run(lambda: "unauthorized overlap")
    finally:
        release.set()
        pool.close()
