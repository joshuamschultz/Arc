"""Report authorization worker lifetime under cancellation and stalled callbacks."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from arcstore.backends.memory import FakeBackend
from starlette.testclient import TestClient

from arcui.report_authorization import ReportReadBusyError, ReportReadWorkerPool
from arcui.server import create_app


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


def test_app_owns_and_closes_report_workers() -> None:
    authority = SimpleNamespace(authorize=lambda _request: None)
    app = create_app(arcstore_backend=FakeBackend(), report_read_authority=authority)
    pool = app.state.report_read_workers
    assert isinstance(pool, ReportReadWorkerPool)
    assert app.state.report_read_authority is authority
    with TestClient(app):
        pass
    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(pool.run(lambda: None))
