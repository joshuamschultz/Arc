"""Managed NATS JetStream server for local team serving.

``arc team serve`` / ``arc ui start`` need a JetStream-enabled broker so the
entity registry, the signed audit chain, and the messaging streams all work
out of the box — without the operator hand-starting ``nats-server``. This
module spawns ``nats-server -js`` as a supervised child bound to the configured
URL and tears it down on exit.

Behaviour:
  * A broker already listening on the URL is **reused** as-is — ``ensure`` then
    returns ``None`` (the caller owns nothing to stop).
  * ``nats-server`` on PATH → spawn it with JetStream enabled on the configured
    host/port, backed by a persistent store dir, and wait until it accepts
    connections.
  * ``nats-server`` absent → raise :class:`NatsServerUnavailableError` with an
    actionable install hint. The caller prints the message, never a traceback.

The child is a plain :class:`subprocess.Popen` (not an asyncio subprocess) so
teardown is loop-independent: the caller typically starts the broker inside one
``asyncio.run`` and then runs a blocking server (uvicorn) in another loop, and
``terminate_sync`` must reap the child without depending on the bootstrap loop
or on the asyncio child-watcher that owned it. The store dir is persistent so
registrations survive a restart and are visible to separate one-shot ``arc
team`` invocations while the broker is up.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_INSTALL_HINT = (
    "install it with `brew install nats-server` (macOS) or from "
    "https://github.com/nats-io/nats-server/releases, then retry"
)


class NatsServerUnavailableError(RuntimeError):
    """No usable NATS broker and none could be started (actionable, not fatal)."""


def parse_host_port(url: str) -> tuple[str, int]:
    """Split a ``nats://host:port`` URL into ``(host, port)`` with defaults."""
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 4222


async def broker_listening(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds within timeout."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


@dataclass
class ManagedNatsServer:
    """Handle to a ``nats-server`` child process this module started."""

    process: subprocess.Popen[bytes]
    url: str
    store_dir: Path
    _atexit_armed: bool = field(default=False, repr=False)

    def arm_atexit_reaper(self) -> None:
        """Register a last-resort reaper at interpreter exit.

        The explicit ``terminate_sync`` in the caller's ``finally`` is the
        normal teardown; this guarantees the child broker never outlives its
        parent even if that ``finally`` is somehow skipped.
        """
        if not self._atexit_armed:
            atexit.register(self.terminate_sync)
            self._atexit_armed = True

    def terminate_sync(self) -> None:
        """Terminate the managed broker and reap it (idempotent, loop-free)."""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=2.0)


async def ensure_nats_server(
    *,
    url: str,
    store_dir: Path,
    startup_timeout: float = 10.0,
) -> ManagedNatsServer | None:
    """Ensure a JetStream broker is reachable at ``url``.

    Returns a :class:`ManagedNatsServer` when this call started one (the caller
    must ``terminate_sync()`` it on shutdown), or ``None`` when an existing
    broker was reused. Raises :class:`NatsServerUnavailableError` when no broker
    is running and ``nats-server`` is not installed (or fails to become ready).
    """
    host, port = parse_host_port(url)

    if await broker_listening(host, port):
        return None

    binary = shutil.which("nats-server")
    if binary is None:
        raise NatsServerUnavailableError(
            f"no NATS broker reachable at {url} and `nats-server` is not on PATH — {_INSTALL_HINT}"
        )

    store_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(  # noqa: S603  # reason: args are fixed literals + validated host/port
        [binary, "-js", "-a", host, "-p", str(port), "-sd", str(store_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = asyncio.get_running_loop().time() + startup_timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            raise NatsServerUnavailableError(
                f"nats-server exited with code {process.returncode} while starting "
                f"on {url} — is the port already in use by a non-JetStream broker?"
            )
        if await broker_listening(host, port, timeout=0.2):
            managed = ManagedNatsServer(process=process, url=url, store_dir=store_dir)
            managed.arm_atexit_reaper()
            return managed
        await asyncio.sleep(0.05)

    process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2.0)
    raise NatsServerUnavailableError(
        f"nats-server did not accept connections on {url} within {startup_timeout}s"
    )


__all__ = [
    "ManagedNatsServer",
    "NatsServerUnavailableError",
    "broker_listening",
    "ensure_nats_server",
    "parse_host_port",
]
