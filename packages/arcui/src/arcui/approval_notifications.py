"""Browser-safe approval notification projection.

The UI owns presentation fan-out only. Durable claiming remains in ArcStore;
the gateway/dispatcher composes this sink when the embedded UI is enabled.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from arcstore.approval_dispatcher import ApprovalNotification


@dataclass(frozen=True)
class BrowserApprovalNotification:
    event_id: str
    approval_id: str
    status: str
    agent_did: str | None
    tool: str | None
    classification: str

    def as_event(self) -> dict[str, object]:
        return {
            "type": "approval_notification",
            "event_id": self.event_id,
            "approval_id": self.approval_id,
            "status": self.status,
            "agent_did": self.agent_did,
            "tool": self.tool,
            "classification": self.classification,
        }


class ApprovalNotificationHub:
    """Bounded in-process browser event buffer; no approval payloads/args."""

    def __init__(self, *, max_events: int = 256) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[BrowserApprovalNotification] = deque(maxlen=max_events)

    async def __call__(self, notification: ApprovalNotification) -> None:
        self._events.append(
            BrowserApprovalNotification(
                event_id=notification.event_id,
                approval_id=notification.approval_id,
                status=notification.status,
                agent_did=notification.agent_did,
                tool=notification.tool,
                classification=notification.classification,
            )
        )

    def recent(self) -> list[dict[str, object]]:
        return [event.as_event() for event in self._events]


__all__ = ["ApprovalNotificationHub", "BrowserApprovalNotification"]
