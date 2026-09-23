"""Own dashboard messaging composition, recovery, and teardown."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcteam.mail import MailDeliveryWorker

from arcui.messaging import (
    build_agent_mail_service,
    build_messaging_service,
    build_team_post_forwarder,
)
from arcui.team_stream import TeamBusObserver

_logger = logging.getLogger(__name__)


class MessagingLifecycle:
    """Build required messaging once, retry degraded startup, and own every task."""

    def __init__(
        self,
        app: Any,
        *,
        required: bool,
        injected_service: Any | None,
        injected_forwarder: Any | None,
        outbox: Any,
        observer_interval: float,
    ) -> None:
        self._app = app
        self._required = required
        self._injected_service = injected_service
        self._injected_forwarder = injected_forwarder
        self._outbox = outbox
        self._observer_interval = observer_interval
        self._backend: Any | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._mail_task: asyncio.Task[None] | None = None
        self._observer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Attempt initial composition, then supervise any required retry."""
        if self._injected_service is not None:
            self._install(self._injected_service, None, None)
            return
        if not self._required:
            return
        if not await self._connect_once():
            self._recovery_task = asyncio.create_task(
                self._recover(), name="arcui:messaging-recovery"
            )

    async def _connect_once(self) -> bool:
        backend: Any | None = None
        try:
            service, registry, backend = await build_messaging_service()
            if service is None:
                return False
            self._install(service, registry, backend)
            return True
        except asyncio.CancelledError:
            if backend is not None:
                await self._close_backend(backend)
            raise
        except Exception as exc:
            _logger.warning("messaging setup failed (%s)", type(exc).__name__)
            if backend is not None:
                await self._close_backend(backend)
            return False

    def _install(self, service: Any, registry: Any, backend: Any) -> None:
        forwarder = self._injected_forwarder
        if forwarder is None:
            forwarder = build_team_post_forwarder(service=service, registry=registry)
        mail = None
        worker = None
        if registry is not None and self._app.state.inbox_service is not None:
            from arcteam.mail import MailDeliveryWorker

            mail = build_agent_mail_service(
                transport=service,
                store=self._app.state.inbox_service,
                outbox=self._outbox,
                registry=registry,
            )
            worker = MailDeliveryWorker(self._outbox, service, worker_id="arcui-agent-mail")
        observer = TeamBusObserver(service, self._app.state.team_stream)
        self._backend = backend
        self._app.state.messaging_service = service
        self._app.state.messaging_registry = registry
        self._app.state.messaging_backend = backend
        self._app.state.team_post_forwarder = forwarder
        self._app.state.agent_mail = mail
        if worker is not None:
            self._mail_task = asyncio.create_task(
                self._drain_mail(worker), name="arcui-agent-mail"
            )
        self._observer_task = asyncio.create_task(
            observer.run(interval=self._observer_interval), name="arcui:team-bus-observer"
        )

    async def _recover(self) -> None:
        delay = 0.5
        while True:
            await asyncio.sleep(delay)
            if await self._connect_once():
                return
            delay = min(delay * 2, 30.0)

    @staticmethod
    async def _drain_mail(worker: MailDeliveryWorker) -> None:
        delay = 0.5
        while True:
            try:
                await worker.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning("mail delivery retry (%s)", type(exc).__name__)
                delay = min(delay * 2, 30.0)
            else:
                delay = 0.5
            await asyncio.sleep(delay)

    @staticmethod
    async def _close_backend(backend: Any) -> None:
        close = getattr(backend, "close", None)
        if close is not None:
            try:
                await asyncio.wait_for(close(), timeout=5.0)
            except Exception as exc:
                _logger.warning("messaging backend close failed (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        """Cancel owned tasks before closing the connection they use."""
        for task in (self._recovery_task, self._mail_task, self._observer_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _logger.warning("messaging task failed during close (%s)", type(exc).__name__)
        if self._backend is not None:
            await self._close_backend(self._backend)
