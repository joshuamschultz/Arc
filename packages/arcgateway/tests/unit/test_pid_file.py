"""Unit tests for PID file lifecycle in GatewayRunner.

Covers:
- PID file written on run() startup
- PID file unlinked on clean shutdown (via _remove_pid_file directly)
- GatewayAlreadyRunning raised if a live PID exists
- Stale PID (dead process) overwritten with warning
- Atomic temp+rename write (no partial file visible)
- 0644 permissions on written PID file
- run() calls _write_pid_file before adapters start
- run() raises GatewayAlreadyRunning before any TaskGroup work
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from arcgateway.runner import GatewayAlreadyRunning, GatewayRunner, _pid_is_alive

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(tmp_path: Path) -> GatewayRunner:
    """Build a GatewayRunner using tmp_path as runtime_dir."""
    return GatewayRunner(adapters=[], runtime_dir=tmp_path)


# ---------------------------------------------------------------------------
# _pid_is_alive helper tests
# ---------------------------------------------------------------------------


def test_pid_is_alive_current_process() -> None:
    """The current process PID is always alive."""
    assert _pid_is_alive(os.getpid()) is True


def test_pid_is_alive_dead_process() -> None:
    """A PID that does not exist returns False."""
    # PID 999999999 is extremely unlikely to be a live process.
    assert _pid_is_alive(999999999) is False


# ---------------------------------------------------------------------------
# _write_pid_file — basic write
# ---------------------------------------------------------------------------


def test_write_pid_file_creates_file(tmp_path: Path) -> None:
    """_write_pid_file() creates gateway.pid containing the current PID."""
    runner = _make_runner(tmp_path)
    runner._write_pid_file()

    pid_file = tmp_path / "gateway.pid"
    assert pid_file.exists(), "gateway.pid must be created"
    content = pid_file.read_text(encoding="utf-8").strip()
    assert content == str(os.getpid()), f"Expected pid={os.getpid()}, got {content!r}"


def test_write_pid_file_permissions(tmp_path: Path) -> None:
    """gateway.pid must be written with 0644 permissions."""
    runner = _make_runner(tmp_path)
    runner._write_pid_file()

    pid_file = tmp_path / "gateway.pid"
    mode = stat.S_IMODE(pid_file.stat().st_mode)
    assert mode == 0o644, f"Expected 0644, got {oct(mode)}"


def test_write_pid_file_atomic_rename(tmp_path: Path) -> None:
    """PID file is written via atomic temp+rename (temp file must be gone after write)."""
    runner = _make_runner(tmp_path)
    runner._write_pid_file()

    # After write, no *.pid.tmp files should remain in the runtime dir.
    tmp_files = list(tmp_path.glob("*.pid.tmp"))
    assert tmp_files == [], f"Stale temp files found: {tmp_files}"


# ---------------------------------------------------------------------------
# _remove_pid_file
# ---------------------------------------------------------------------------


def test_remove_pid_file_unlinks_file(tmp_path: Path) -> None:
    """_remove_pid_file() unlinks the PID file."""
    runner = _make_runner(tmp_path)
    runner._write_pid_file()
    assert (tmp_path / "gateway.pid").exists()

    runner._remove_pid_file()
    assert not (tmp_path / "gateway.pid").exists(), "PID file must be removed"


def test_remove_pid_file_noop_when_missing(tmp_path: Path) -> None:
    """_remove_pid_file() does NOT raise if the PID file doesn't exist."""
    runner = _make_runner(tmp_path)
    # File was never written — should be a no-op.
    runner._remove_pid_file()  # must not raise


# ---------------------------------------------------------------------------
# GatewayAlreadyRunning — refuse if live PID
# ---------------------------------------------------------------------------


def test_write_pid_file_raises_if_live_pid(tmp_path: Path) -> None:
    """_write_pid_file() raises GatewayAlreadyRunning when PID is alive."""
    runner = _make_runner(tmp_path)

    # Write a PID file containing our own (live) PID.
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with pytest.raises(GatewayAlreadyRunning) as exc_info:
        runner._write_pid_file()

    assert exc_info.value.pid == os.getpid()
    assert exc_info.value.runtime_dir == tmp_path


def test_already_running_error_message(tmp_path: Path) -> None:
    """GatewayAlreadyRunning carries a helpful error message."""
    err = GatewayAlreadyRunning(pid=12345, runtime_dir=tmp_path)
    assert "12345" in str(err)
    assert str(tmp_path) in str(err)


# ---------------------------------------------------------------------------
# Stale PID (dead process) — overwrite with warning
# ---------------------------------------------------------------------------


def test_write_pid_file_overwrites_stale_pid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stale PID file (dead process) is overwritten with a log warning."""
    import logging

    runner = _make_runner(tmp_path)

    # Write a PID file with a non-existent PID.
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("999999999\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="arcgateway.runner"):
        runner._write_pid_file()

    # File must now contain current PID.
    new_content = pid_file.read_text(encoding="utf-8").strip()
    assert new_content == str(os.getpid())

    # A warning must have been logged.
    assert any("stale" in r.message.lower() for r in caplog.records), (
        f"Expected stale-PID warning in logs; got: {[r.message for r in caplog.records]}"
    )


def test_write_pid_file_overwrites_corrupted_pid(tmp_path: Path) -> None:
    """Corrupted PID file (non-integer) is overwritten without raising."""
    runner = _make_runner(tmp_path)

    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("not-a-pid\n", encoding="utf-8")

    # Must not raise — treat as stale.
    runner._write_pid_file()

    new_content = pid_file.read_text(encoding="utf-8").strip()
    assert new_content == str(os.getpid())


# ---------------------------------------------------------------------------
# Lifecycle: PID written at startup and removed at shutdown
# (Use task-cancel pattern to avoid hanging on TaskGroup infinite loops)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_writes_pid_file_at_startup(tmp_path: Path) -> None:
    """run() writes gateway.pid before the TaskGroup starts."""
    runner = _make_runner(tmp_path)
    pid_file = tmp_path / "gateway.pid"

    # Cancel the task immediately after a short sleep to observe PID write.
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.03)

    assert pid_file.exists(), "PID file must be written during run() startup"
    content = pid_file.read_text(encoding="utf-8").strip()
    assert content == str(os.getpid())

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: S110 — test cleanup, swallow is intentional
        pass


@pytest.mark.asyncio
async def test_run_removes_pid_file_on_cancel(tmp_path: Path) -> None:
    """run() finally block removes PID file even on task cancellation.

    Note: task cancellation propagates CancelledError into the finally block
    via _shutdown_adapters → _remove_pid_file → _write_clean_shutdown_marker.
    """
    runner = _make_runner(tmp_path)
    pid_file = tmp_path / "gateway.pid"

    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.03)
    assert pid_file.exists(), "PID must be written before cancel"

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: S110 — test cleanup, swallow is intentional
        pass

    # finally block runs on cancellation too — PID should be removed.
    assert not pid_file.exists(), "PID file must be removed in finally block"


@pytest.mark.asyncio
async def test_run_raises_already_running_before_starting(tmp_path: Path) -> None:
    """run() raises GatewayAlreadyRunning if PID file belongs to a live process."""
    runner = _make_runner(tmp_path)

    # Pre-populate with our own live PID.
    pid_file = tmp_path / "gateway.pid"
    tmp_path.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with patch.object(runner, "_install_signal_handlers", return_value=None):
        with pytest.raises(GatewayAlreadyRunning):
            await runner.run()


# ---------------------------------------------------------------------------
# Verify GatewayAlreadyRunning attributes
# ---------------------------------------------------------------------------


def test_gateway_already_running_attributes() -> None:
    """GatewayAlreadyRunning.pid and .runtime_dir are set correctly."""
    err = GatewayAlreadyRunning(pid=42, runtime_dir=Path("/tmp/test"))
    assert err.pid == 42
    assert err.runtime_dir == Path("/tmp/test")
