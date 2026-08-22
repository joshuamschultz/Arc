"""Gateway composition for durable approval notifications.

ArcStore supplies a sanitized event; the gateway chooses the configured
operator channel.  This module never imports ArcUI, so headless gateway
deployments can use Telegram/Slack/etc. without reversing package seams.
"""

from __future__ import annotations

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
    ) -> None:
        self._session_router = session_router
        self._target = target
        self._agent_did = agent_did

    async def __call__(self, notification: ApprovalNotification) -> None:
        await self._session_router.send(
            self._target,
            compose_approval_message(notification),
            agent_did=self._agent_did,
        )


__all__ = ["GatewayApprovalNotificationSink", "compose_approval_message"]
