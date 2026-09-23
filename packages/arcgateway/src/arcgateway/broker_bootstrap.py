"""COMP-008 BrokerBootstrap — a message broker on every launch path.

REQ-306: starting Arc is the only operator step; the broker follows from it.
REQ-307: when no broker can be started or reached, the failure is loud and the
handle says so explicitly, so a messaging surface can answer "I cannot see your
messages" instead of "no messages".

``arcteam.nats_server.ensure_nats_server`` already does the hard part — probe,
spawn, wait for readiness, reuse what is already listening. What was missing is
a caller on the composition root every launch path goes through. This module is
that caller: it resolves the deployment's broker url and JetStream store dir,
delegates, and wraps the result in a handle whose ownership is explicit.

Ownership is the whole design:
  * a broker this call **started** → ``managed`` holds the child; ``aclose``
    reaps it, and startup's own error path reaps it too, so a stage that fails
    after the broker came up cannot strand an orphan.
  * a broker that was **already listening** → ``managed`` is ``None``. Reuse is
    not ownership; ``aclose`` must never terminate someone else's process.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from arcteam.config import default_jetstream_store_dir, default_nats_url
from arcteam.nats_server import NatsServerUnavailableError, ensure_nats_server

if TYPE_CHECKING:
    from pathlib import Path

    from arcteam.nats_server import ManagedNatsServer

_logger = logging.getLogger("arcgateway.broker_bootstrap")
_MONITOR_INTERVAL_SECONDS = 1.0
_RESTART_DELAY_SECONDS = 0.25
_RESTART_MAX_SECONDS = 30.0
_READY_CACHE_SECONDS = 2.0


@dataclass
class BrokerHandle:
    """The broker outcome of one startup, and the right to stop what it started.

    ``available`` is the answer a messaging surface needs: ``True`` means a
    broker is reachable at ``url`` (started here or already running), ``False``
    means it is not and ``reason`` says why. An empty inbox behind an
    ``available=False`` handle is a blind spot, not an accurate answer.
    """

    url: str
    available: bool
    reason: str | None = None
    managed: ManagedNatsServer | None = None
    _closed: bool = field(default=False, repr=False)
    _supervisor: asyncio.Task[None] | None = field(default=None, repr=False)
    _readiness_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _readiness_at: float = field(default=0.0, repr=False)
    _readiness_result: bool = field(default=False, repr=False)

    async def check_ready(self) -> bool:
        """Prove the configured broker accepts an authenticated JetStream request."""
        if not self.available:
            return False
        async with self._readiness_lock:
            now = asyncio.get_running_loop().time()
            if now - self._readiness_at < _READY_CACHE_SECONDS:
                return self._readiness_result
            self._readiness_result = await self._probe_jetstream()
            self._readiness_at = asyncio.get_running_loop().time()
            return self._readiness_result

    async def _probe_jetstream(self) -> bool:
        """Use the configured connection authority to issue a bounded JS request."""
        import nats

        async def _ignore_client_error(_error: Exception) -> None:
            return None

        connection = None
        try:
            connection = await asyncio.wait_for(
                nats.connect(
                    self.url,
                    allow_reconnect=False,
                    max_reconnect_attempts=0,
                    connect_timeout=0.5,
                    error_cb=_ignore_client_error,
                ),
                timeout=1.0,
            )
            await asyncio.wait_for(connection.jetstream().account_info(), timeout=1.0)
            return True
        except Exception:  # reason: any failed JS request means this dependency is not ready
            return False
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception as exc:
                    _logger.warning(
                        "broker: readiness probe close failed (%s)", type(exc).__name__
                    )

    def supervise(self, store_dir: Path) -> None:
        """Recover a failed startup or owned child; never manage a reused broker."""
        if (self.managed is not None or not self.available) and self._supervisor is None:
            self._supervisor = asyncio.create_task(
                self._watch_owned_child(store_dir), name="arcgateway:nats-server"
            )

    async def _watch_owned_child(self, store_dir: Path) -> None:
        delay = _RESTART_DELAY_SECONDS
        started_at = asyncio.get_running_loop().time()
        while not self._closed:
            await asyncio.sleep(_MONITOR_INTERVAL_SECONDS)
            if self._closed or (self.managed is not None and self.managed.running):
                continue
            if self.managed is None and self.available:
                return
            if self.managed is not None:
                uptime = asyncio.get_running_loop().time() - started_at
                delay = (
                    _RESTART_DELAY_SECONDS
                    if uptime >= 60.0
                    else min(delay * 2, _RESTART_MAX_SECONDS)
                )
            self.available = False
            self._readiness_at = 0.0
            self.reason = "managed broker exited"
            _logger.error("broker: managed nats-server exited; restarting")
            if self.managed is not None:
                self.managed.terminate_sync()
            self.managed = None
            while not self._closed:
                await asyncio.sleep(
                    min(delay * (0.9 + secrets.randbelow(201) / 1000), _RESTART_MAX_SECONDS)
                )
                try:
                    replacement = await ensure_nats_server(url=self.url, store_dir=store_dir)
                except NatsServerUnavailableError as exc:
                    self.reason = type(exc).__name__
                    delay = min(delay * 2, _RESTART_MAX_SECONDS)
                    continue
                if self._closed:
                    if replacement is not None:
                        replacement.terminate_sync()
                    return
                self.managed = replacement
                self.available = True
                self.reason = None
                started_at = asyncio.get_running_loop().time()
                _logger.info("broker: managed nats-server recovered")
                break

    async def aclose(self) -> None:
        """Terminate the child this startup began. Idempotent; never kills a reuse.

        Shutdown reaches this from more than one place (the lifespan's ``finally``
        and startup's own error path), so a second call must be a no-op rather
        than a second signal at a pid that may have been recycled.
        """
        if self._closed:
            return
        self._closed = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await asyncio.wait_for(self._supervisor, timeout=12.0)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                _logger.error("broker: supervisor shutdown timed out")
        if self.managed is None:
            return
        _logger.info("broker: terminating the nats-server this gateway started")
        self.managed.terminate_sync()


async def start_broker(*, url: str | None = None, store_dir: Path | None = None) -> BrokerHandle:
    """Ensure a JetStream broker is reachable, and report what happened.

    Never raises for an absent broker: a deployment without ``nats-server``
    still serves its dashboard and web chat. The unavailability is carried in
    the returned handle and logged at ERROR, because the failure mode this
    guards against is a silent degrade that reads as a healthy empty inbox.
    """
    resolved_url = url or default_nats_url()
    resolved_store_dir = store_dir or default_jetstream_store_dir()

    try:
        managed = await ensure_nats_server(url=resolved_url, store_dir=resolved_store_dir)
    except NatsServerUnavailableError as exc:
        # ERROR without a traceback: the condition is actionable operator state
        # (no broker, install hint attached), not a crash to be debugged.
        _logger.error("broker unavailable (%s); messaging is degraded", type(exc).__name__)
        handle = BrokerHandle(
            url=resolved_url, available=False, reason=type(exc).__name__
        )
        handle.supervise(resolved_store_dir)
        return handle

    if managed is None:
        _logger.info("broker: reusing an existing broker")
    else:
        _logger.info("broker: started a supervised nats-server")
    handle = BrokerHandle(url=resolved_url, available=True, managed=managed)
    handle.supervise(resolved_store_dir)
    return handle


__all__ = ["BrokerHandle", "start_broker"]
