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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from arcteam.config import default_jetstream_store_dir, default_nats_url
from arcteam.nats_server import NatsServerUnavailableError, ensure_nats_server

if TYPE_CHECKING:
    from pathlib import Path

    from arcteam.nats_server import ManagedNatsServer

_logger = logging.getLogger("arcgateway.broker_bootstrap")


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

    async def aclose(self) -> None:
        """Terminate the child this startup began. Idempotent; never kills a reuse.

        Shutdown reaches this from more than one place (the lifespan's ``finally``
        and startup's own error path), so a second call must be a no-op rather
        than a second signal at a pid that may have been recycled.
        """
        if self._closed:
            return
        self._closed = True
        if self.managed is None:
            return
        _logger.info("broker: terminating the nats-server this gateway started at %s", self.url)
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
        _logger.error(
            "broker unavailable at %s — messaging surfaces will report unavailable, "
            "and an empty inbox from here is a blind spot, not an answer: %s",
            resolved_url,
            exc,
        )
        return BrokerHandle(url=resolved_url, available=False, reason=str(exc))

    if managed is None:
        _logger.info("broker: reusing the one already listening at %s", resolved_url)
    else:
        _logger.info("broker: started a supervised nats-server at %s", resolved_url)
    return BrokerHandle(url=resolved_url, available=True, managed=managed)


__all__ = ["BrokerHandle", "start_broker"]
