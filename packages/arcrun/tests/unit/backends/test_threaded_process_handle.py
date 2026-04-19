"""Tests for _ThreadedProcessHandle — SDK-only backend async bridge.

Covers:
- exec_fn wrapped into async stream
- cancel() invokes cancel_fn
- error in exec_fn propagates through stream()
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from arcrun.backends.base import TRUNCATION_MARKER, _ThreadedProcessHandle


@pytest.mark.asyncio
async def test_stream_bytes_from_sync_fn() -> None:
    """exec_fn result is available through async stream()."""

    def exec_fn(command: str) -> bytes:
        return b"hello from sdk"

    handle = _ThreadedProcessHandle(exec_fn=exec_fn, cancel_fn=lambda: None)
    await handle.start("some-command")

    chunks: list[bytes] = []
    async for chunk in handle.stream():
        chunks.append(chunk)

    assert b"hello from sdk" in b"".join(chunks)


@pytest.mark.asyncio
async def test_stream_large_output_truncated() -> None:
    """Output exceeding max_stdout_bytes is truncated with the marker."""

    def exec_fn(command: str) -> bytes:
        return b"x" * 200

    handle = _ThreadedProcessHandle(
        exec_fn=exec_fn, cancel_fn=lambda: None, max_stdout_bytes=10
    )
    await handle.start("cmd")

    chunks: list[bytes] = []
    async for chunk in handle.stream():
        chunks.append(chunk)

    output = b"".join(chunks)
    assert TRUNCATION_MARKER in output
    # The actual data part must be at most max_stdout_bytes
    data_part = output[: output.index(TRUNCATION_MARKER)]
    assert len(data_part) <= 10


@pytest.mark.asyncio
async def test_cancel_calls_cancel_fn() -> None:
    """cancel() calls the cancel_fn."""
    cancelled = threading.Event()

    def exec_fn(command: str) -> bytes:
        # Block until cancelled
        for _ in range(100):
            if cancelled.is_set():
                break
            time.sleep(0.01)
        return b""

    def cancel_fn() -> None:
        cancelled.set()

    handle = _ThreadedProcessHandle(exec_fn=exec_fn, cancel_fn=cancel_fn)
    await handle.start("blocking-cmd")
    await asyncio.sleep(0.02)
    handle.cancel()

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_error_in_exec_fn_propagates() -> None:
    """An exception raised by exec_fn surfaces through stream()."""

    def exec_fn(command: str) -> bytes:
        raise RuntimeError("SDK exploded")

    handle = _ThreadedProcessHandle(exec_fn=exec_fn, cancel_fn=lambda: None)
    await handle.start("cmd")

    with pytest.raises(RuntimeError, match="SDK exploded"):
        async for _ in handle.stream():
            pass


@pytest.mark.asyncio
async def test_cancel_before_start_is_safe() -> None:
    """cancel() before start() sets the cancelled event without error."""

    handle = _ThreadedProcessHandle(exec_fn=lambda _: b"", cancel_fn=lambda: None)
    handle.cancel()
    assert handle._cancelled.is_set()
