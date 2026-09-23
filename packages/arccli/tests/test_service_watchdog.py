"""systemd readiness and an event-loop heartbeat for ``arc ui start``.

The loop that serves chat, NATS and health was pinned for days while the
process looked alive. The heartbeat runs ON that loop: when the loop is
blocked no ping goes out, and systemd's watchdog kills and restarts the
service (its SIGABRT makes faulthandler dump every thread to the journal).
Tested against a real unix datagram socket, as systemd provides one.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from arccli.service_watchdog import (
    ServiceWatchdog,
    heartbeat_interval,
    notify,
    socket_address,
)


class Receiver:
    """A stand-in for systemd's notify socket, recording what arrives and when."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[tuple[float, str]] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(str(path))
        self._sock.settimeout(0.02)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._sock.recv(4096)
            except TimeoutError:
                continue
            self.messages.append((time.monotonic(), data.decode()))

    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        self._sock.close()

    def arrivals(self, message: str) -> list[float]:
        return [at for at, text in self.messages if text == message]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    # AF_UNIX paths are short (104 bytes on macOS); pytest's tmp_path is not.
    directory = Path(tempfile.mkdtemp(prefix="wd", dir="/tmp"))
    listener = Receiver(directory / "notify")
    try:
        yield listener
    finally:
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_notify_writes_one_datagram_to_the_socket(receiver: Receiver) -> None:
    assert notify("READY=1", environ={"NOTIFY_SOCKET": str(receiver.path)})
    deadline = time.monotonic() + 1
    while not receiver.messages and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [text for _, text in receiver.messages] == ["READY=1"]


def test_notify_is_a_no_op_outside_systemd() -> None:
    assert notify("READY=1", environ={}) is False


def test_notify_never_raises_when_the_socket_is_gone(tmp_path: Path) -> None:
    assert notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": str(tmp_path / "gone")}) is False


def test_an_abstract_namespace_socket_is_addressed_with_a_leading_nul() -> None:
    assert socket_address("@/org/freedesktop/systemd1/notify") == (
        "\0/org/freedesktop/systemd1/notify"
    )
    assert socket_address("/run/systemd/notify") == "/run/systemd/notify"


def test_heartbeat_runs_at_a_third_of_the_watchdog_timeout() -> None:
    environ = {"NOTIFY_SOCKET": "/run/notify", "WATCHDOG_USEC": "60000000"}
    assert heartbeat_interval(environ) == pytest.approx(20.0)
    assert heartbeat_interval({"NOTIFY_SOCKET": "/run/notify"}) is None
    assert heartbeat_interval({"WATCHDOG_USEC": "60000000"}) is None
    other = {**environ, "WATCHDOG_PID": str(os.getpid() + 1)}
    assert heartbeat_interval(other) is None, "the watchdog belongs to another process"
    mine = {**environ, "WATCHDOG_PID": str(os.getpid())}
    assert heartbeat_interval(mine) == pytest.approx(20.0)


async def test_ready_then_heartbeats_stop_while_the_loop_is_blocked(receiver: Receiver) -> None:
    environ = {"NOTIFY_SOCKET": str(receiver.path), "WATCHDOG_USEC": "150000"}  # 50 ms beats
    watchdog = ServiceWatchdog(environ=environ)
    await watchdog.start()
    try:
        await asyncio.sleep(0.3)
        blocked_from = time.monotonic()
        time.sleep(0.4)  # the event loop is pinned: nothing on it can run
        blocked_until = time.monotonic()
        await asyncio.sleep(0.3)
    finally:
        await watchdog.stop()
    await asyncio.sleep(0.05)

    assert receiver.arrivals("READY=1"), "readiness is announced once startup completes"
    beats = receiver.arrivals("WATCHDOG=1")
    before = [at for at in beats if at < blocked_from]
    during = [at for at in beats if blocked_from + 0.01 < at < blocked_until - 0.01]
    after = [at for at in beats if at > blocked_until]
    assert len(before) >= 4
    assert during == [], "a blocked loop must not look alive to systemd"
    assert len(after) >= 3, "beats resume once the loop turns again"
    assert receiver.arrivals("STOPPING=1")


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX signals only")
def test_sigusr1_dumps_every_thread_stack_to_stderr() -> None:
    """How the next stall is diagnosed on a box where py-spy cannot attach."""
    script = (
        "import os, signal, threading, time\n"
        "from arccli.service_watchdog import enable_fault_dumps\n"
        "assert enable_fault_dumps()\n"
        "threading.Thread(target=time.sleep, args=(5,), daemon=True, name='sync').start()\n"
        "os.kill(os.getpid(), signal.SIGUSR1)\n"
        "time.sleep(0.2)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=True
    )

    # The signalled (loop) thread and every other thread, each with its stack.
    assert "Current thread 0x" in result.stderr
    assert result.stderr.count("hread 0x") >= 2, result.stderr


async def test_no_heartbeat_task_without_a_watchdog(receiver: Receiver) -> None:
    watchdog = ServiceWatchdog(environ={"NOTIFY_SOCKET": str(receiver.path)})
    await watchdog.start()
    await asyncio.sleep(0.05)
    await watchdog.stop()

    assert receiver.arrivals("WATCHDOG=1") == []
