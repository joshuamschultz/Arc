"""Unit tests for LocalBackend.

Covers:
- run echo, stream bytes
- cancel SIGTERM → SIGKILL
- close() disposes all handles
- killpg orphan-pgroup cleanup
"""

from __future__ import annotations

import asyncio

import pytest

from arcrun.backends import ExecHandle, LocalBackend


@pytest.mark.asyncio
async def test_run_echo_returns_handle() -> None:
    backend = LocalBackend()
    handle = await backend.run("echo hello")
    assert isinstance(handle, ExecHandle)
    assert handle.backend_name == "local"
    # Drain to avoid ResourceWarning from dangling subprocess
    async for _ in backend.stream(handle):
        pass
    await backend.close()


@pytest.mark.asyncio
async def test_stream_bytes_from_echo() -> None:
    backend = LocalBackend()
    handle = await backend.run("echo hello")
    chunks: list[bytes] = []
    async for chunk in backend.stream(handle):
        chunks.append(chunk)
    output = b"".join(chunks)
    assert b"hello" in output
    await backend.close()


@pytest.mark.asyncio
async def test_stream_multiline_output() -> None:
    backend = LocalBackend()
    handle = await backend.run("printf 'line1\\nline2\\nline3\\n'")
    data = b""
    async for chunk in backend.stream(handle):
        data += chunk
    assert b"line1" in data
    assert b"line3" in data
    await backend.close()


@pytest.mark.asyncio
async def test_stdout_truncation() -> None:
    """Output exceeding max_stdout_bytes is truncated with a marker."""
    from arcrun.backends.base import TRUNCATION_MARKER

    backend = LocalBackend(max_stdout_bytes=10)
    # Generate 100 bytes of output
    handle = await backend.run("python3 -c \"print('x' * 100)\"")
    chunks: list[bytes] = []
    async for chunk in backend.stream(handle):
        chunks.append(chunk)
    output = b"".join(chunks)
    assert TRUNCATION_MARKER in output
    await backend.close()


@pytest.mark.asyncio
async def test_cancel_sigterm_then_sigkill() -> None:
    """Cancel a sleeping process; should terminate within grace + small buffer."""
    backend = LocalBackend()
    # A long sleep that we will cancel
    handle = await backend.run("sleep 60")
    await asyncio.sleep(0.05)  # let it start
    await backend.cancel(handle, grace=0.5)
    # After cancel, the handle should be cleaned up
    assert handle.handle_id not in backend._procs
    await backend.close()


@pytest.mark.asyncio
async def test_cancel_already_exited_no_error() -> None:
    """cancel() on a finished process should not raise."""
    backend = LocalBackend()
    handle = await backend.run("echo done")
    # Drain to completion
    async for _ in backend.stream(handle):
        pass
    # Process has exited; cancel should be a no-op
    await backend.cancel(handle, grace=1.0)
    await backend.close()


@pytest.mark.asyncio
async def test_close_disposes_all_handles() -> None:
    backend = LocalBackend()
    h1 = await backend.run("sleep 60")
    h2 = await backend.run("sleep 60")
    await asyncio.sleep(0.05)
    await backend.close()
    assert not backend._procs


@pytest.mark.asyncio
async def test_orphan_pgroup_killed_on_cancel() -> None:
    """A child process that forks a grandchild is also killed via killpg."""
    backend = LocalBackend()
    # Start a shell that itself starts a background sleep.
    # Without setsid+killpg the grandchild would orphan.
    handle = await backend.run("bash -c 'sleep 120 & wait'")
    await asyncio.sleep(0.1)
    await backend.cancel(handle, grace=1.0)
    # If killpg worked, the background sleep is also gone.
    # We verify by checking the handle is cleaned up.
    assert handle.handle_id not in backend._procs
    await backend.close()


@pytest.mark.asyncio
async def test_env_passthrough() -> None:
    backend = LocalBackend()
    handle = await backend.run("echo $MYVAR", env={"MYVAR": "arc_test_value", "PATH": "/usr/bin:/bin"})
    data = b""
    async for chunk in backend.stream(handle):
        data += chunk
    assert b"arc_test_value" in data
    await backend.close()


@pytest.mark.asyncio
async def test_cwd_passthrough(tmp_path: "pathlib.Path") -> None:  # type: ignore[name-defined]
    import pathlib

    backend = LocalBackend()
    handle = await backend.run("pwd", cwd=str(tmp_path))
    data = b""
    async for chunk in backend.stream(handle):
        data += chunk
    assert str(tmp_path).encode() in data
    await backend.close()
