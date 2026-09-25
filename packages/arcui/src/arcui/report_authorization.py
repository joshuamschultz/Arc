"""Deployment authority contract for reading an agent's rendered report."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ReportReadRequest(BaseModel, frozen=True):
    """Identity and pinned file metadata evaluated before report bytes are read."""

    caller_did: str = Field(min_length=1)
    agent_did: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    root: str = Field(min_length=1)
    device: int
    inode: int
    size: int = Field(ge=0)
    modified_ns: int = Field(ge=0)


class ReportReadGrant(BaseModel, frozen=True):
    """Authorized provenance for the exact report body served to the caller."""

    caller_did: str = Field(min_length=1)
    agent_did: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    source_id: str = Field(
        min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]*$"
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportReadAuthority(Protocol):
    """Deployment-scoped lookup with its own bounded transport deadline."""

    def authorize(self, request: ReportReadRequest) -> ReportReadGrant | None: ...


class ReportReadBusyError(RuntimeError):
    """Every report read worker remains occupied."""


class ReportReadWorkerPool:
    """Bound report reads even when a timed-out client leaves a worker running."""

    def __init__(self, *, max_workers: int = 2, operation_timeout: float = 5.0) -> None:
        if max_workers < 1 or operation_timeout <= 0:
            raise ValueError("report read worker limits must be positive")
        self._slots = asyncio.Semaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="arc-report"
        )
        self._pending: set[Future[Any]] = set()
        self._operation_timeout = operation_timeout
        self._closed = False

    async def run(self, operation: Callable[[], T]) -> T:
        """Return within deadline and retain the slot until the worker really exits."""
        if self._closed:
            raise RuntimeError("report read workers are closed")
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=0.1)
        except TimeoutError as exc:
            raise ReportReadBusyError("report read workers are busy") from exc
        loop = asyncio.get_running_loop()
        try:
            future = self._executor.submit(operation)
        except BaseException:
            self._slots.release()
            raise
        self._pending.add(future)

        def finished(_done: Future[T]) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(release)

        def release() -> None:
            self._pending.discard(future)
            self._slots.release()

        future.add_done_callback(finished)
        return await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)), timeout=self._operation_timeout
        )

    def close(self) -> None:
        """Stop accepting work without waiting indefinitely for a hostile callback."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "ReportReadAuthority",
    "ReportReadBusyError",
    "ReportReadGrant",
    "ReportReadRequest",
    "ReportReadWorkerPool",
]
