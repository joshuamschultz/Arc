"""Gateway composition for durable approval notifications.

ArcStore supplies a sanitized event; the gateway chooses the configured
operator channel.  This module never imports ArcUI, so headless gateway
deployments can use Telegram/Slack/etc. without reversing package seams.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from arcstore.approval_delivery import DurableApprovalDelivery
from arcstore.approval_dispatcher import ApprovalNotification

from arcgateway.delivery import DeliveryTarget
from arcgateway.session import SessionRouter


def compose_approval_message(notification: ApprovalNotification) -> str:
    """Render only safe approval metadata; arguments and grants are excluded."""
    actor = notification.agent_did or "unknown agent"
    tool = notification.tool or "unknown tool"
    return (
        f"Approval {notification.status}: {notification.approval_id}\n"
        f"Agent: {actor}\nTool: {tool}\n"
        f"Classification: {notification.classification}\n"
        f"Notification: {notification.event_id}"
    )


class GatewayApprovalNotificationSink:
    """Deliver approval notices to one explicitly configured operator target."""

    def __init__(
        self,
        session_router: SessionRouter,
        target: DeliveryTarget,
        *,
        agent_did: str = "",
        backend: Any | None = None,
        sink_id: str | None = None,
    ) -> None:
        self._session_router = session_router
        self._target = target
        self._agent_did = agent_did
        self._delivery = (
            DurableApprovalDelivery(
                backend,
                sink_id=sink_id
                or f"gateway-{str(target).replace(':', '-')}-{agent_did.replace(':', '-')}",
            )
            if backend is not None
            else None
        )
        # Compatibility for small/headless callers that do not compose ArcStore.
        # Production composition always supplies the shared backend above.
        self._seen: set[str] = set()

    async def __call__(self, notification: ApprovalNotification) -> None:
        if self._delivery is not None:
            if not await self._delivery.claim(notification):
                return
        elif notification.event_id in self._seen:
            return
        try:
            await self._session_router.send(
                self._target,
                compose_approval_message(notification),
                agent_did=self._agent_did,
            )
        except BaseException:
            if self._delivery is not None:
                await self._delivery.release(notification)
            raise
        if self._delivery is not None:
            await self._delivery.complete(notification)
        else:
            self._seen.add(notification.event_id)


class ApprovalNotificationFanout:
    """Deliver one typed event to browser and configured operator sinks."""

    def __init__(self, sinks: Iterable[object]) -> None:
        self._sinks = list(sinks)

    def add(self, sink: object) -> None:
        self._sinks.append(sink)

    async def __call__(self, notification: ApprovalNotification) -> None:
        failures: list[BaseException] = []
        for sink in self._sinks:
            try:
                await sink(notification)  # type: ignore[operator]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Continue all sinks. On an outbox retry, each successful sink
                # has durable event-id state and will suppress a duplicate.
                failures.append(exc)
        if failures:
            raise failures[0]


__all__ = [
    "ApprovalNotificationFanout",
    "GatewayApprovalNotificationSink",
    "compose_approval_message",
]
