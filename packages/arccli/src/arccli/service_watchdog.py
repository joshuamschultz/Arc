"""systemd readiness, an event-loop heartbeat, and stack dumps for ``arc ui start``.

The DGX incident: one asyncio loop thread was pinned for days, so chat, NATS
and health all timed out while the process still looked alive to systemd.
Under ``Type=notify`` with ``WatchdogSec=``, this module

* announces ``READY=1`` once startup has completed,
* sends ``WATCHDOG=1`` from a task ON the event loop every third of the
  watchdog timeout, so a blocked loop stops the beats and systemd restarts the
  service, and
* enables :mod:`faulthandler`, so the watchdog's SIGABRT (and a manual
  ``SIGUSR1``) dumps every thread's Python stack to the journal - how the next
  stall gets diagnosed where a ptrace-based profiler cannot attach.

No dependency: ``sd_notify`` is one datagram to the unix socket in
``$NOTIFY_SOCKET``. Outside systemd every call is a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import io
import logging
import os
import signal
import socket
import sys
from collections.abc import Mapping

_logger = logging.getLogger("arccli.service_watchdog")


def socket_address(value: str) -> str:
    """Translate systemd's ``@name`` abstract-namespace form to its NUL form."""
    return "\0" + value[1:] if value.startswith("@") else value


def notify(message: str, *, environ: Mapping[str, str] = os.environ) -> bool:
    """Send one ``sd_notify`` datagram; False when not under systemd or unsent.

    Never raises and never blocks: a full or missing socket costs one beat,
    never the service.
    """
    address = environ.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    try:
        # Python sockets are non-inheritable by default (PEP 446).
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.connect(socket_address(address))
            sock.send(message.encode("utf-8"))
    except OSError as exc:
        _logger.warning("sd_notify %s not delivered (%s)", message, type(exc).__name__)
        return False
    return True


def heartbeat_interval(environ: Mapping[str, str] = os.environ) -> float | None:
    """Seconds between beats (a third of the timeout), or None with no watchdog."""
    usec = environ.get("WATCHDOG_USEC", "")
    if not usec or not environ.get("NOTIFY_SOCKET"):
        return None
    owner = environ.get("WATCHDOG_PID", "")
    if owner and owner != str(os.getpid()):
        return None
    try:
        timeout = int(usec) / 1_000_000
    except ValueError:
        return None
    return timeout / 3 if timeout > 0 else None


def enable_fault_dumps() -> bool:
    """Dump every thread's stack on a fatal signal, and on demand with SIGUSR1.

    Writes to the process's original stderr (the journal under systemd), which
    stays a real file descriptor even when ``sys.stderr`` has been replaced.
    Returns False, with a warning, when there is no such descriptor.
    """
    stream = sys.__stderr__
    try:
        if stream is None:
            raise ValueError("no stderr")
        stream.fileno()
    except (OSError, ValueError, io.UnsupportedOperation):
        _logger.warning("fault stack dumps unavailable: stderr has no file descriptor")
        return False
    faulthandler.enable(file=stream, all_threads=True)
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True, chain=False)
    return True


class ServiceWatchdog:
    """Announce readiness and keep the systemd watchdog fed from the event loop."""

    def __init__(self, *, environ: Mapping[str, str] = os.environ) -> None:
        self._environ = environ
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The heartbeat task, for an owner that cancels it on shutdown."""
        return self._task

    async def start(self) -> None:
        """Signal ``READY=1`` and, when a watchdog is armed, start the beats."""
        notify("READY=1", environ=self._environ)
        interval = heartbeat_interval(self._environ)
        if interval is not None:
            self._task = asyncio.create_task(self._beat(interval), name="arc:systemd-watchdog")

    async def stop(self) -> None:
        """Stop the beats and tell systemd the service is going down on purpose."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        notify("STOPPING=1", environ=self._environ)

    async def _beat(self, interval: float) -> None:
        while True:
            notify("WATCHDOG=1", environ=self._environ)
            await asyncio.sleep(interval)


__all__ = [
    "ServiceWatchdog",
    "enable_fault_dumps",
    "heartbeat_interval",
    "notify",
    "socket_address",
]
