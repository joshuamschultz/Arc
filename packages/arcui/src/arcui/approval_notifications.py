"""Browser-safe approval notification projection.

The UI owns presentation fan-out only. Durable claiming remains in ArcStore;
the gateway/dispatcher composes this sink when the embedded UI is enabled.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from arcstore.approval_delivery import notification_payload
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

    _COLLECTION = "approval_notification_browser"
    _SINK_ID = "browser"

    def __init__(self, *, max_events: int = 256, backend: Any | None = None) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._backend = backend
        self._events: deque[BrowserApprovalNotification] = deque(maxlen=max_events)
        self._event_ids: set[str] = set()

    async def __call__(self, notification: ApprovalNotification) -> None:
        if self._backend is not None:
            key = self._key(notification.event_id)
            await self._backend.mutable_create_batch(
                self._COLLECTION,
                [
                    (
                        key,
                        {
                            "sink_id": self._SINK_ID,
                            "event_id": notification.event_id,
                            "state": "unacked",
                            "notification": notification_payload(notification),
                        },
                    )
                ],
                actor_did="did:arc:system:approval-browser",
            )
            return
        if notification.event_id in self._event_ids:
            return
        if len(self._events) == self._events.maxlen:
            self._event_ids.discard(self._events[0].event_id)
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
        self._event_ids.add(notification.event_id)

    async def list_recent(self) -> list[dict[str, object]]:
        """Read unacknowledged browser events from durable storage."""

        if self._backend is None:
            return self.recent()
        rows = await self._backend.mutable_query(
            self._COLLECTION,
            where={"sink_id": self._SINK_ID, "state": "unacked"},
        )
        events: list[dict[str, object]] = []
        for row in rows:
            payload = row.get("notification") if isinstance(row, dict) else None
            if isinstance(payload, dict):
                events.append(
                    BrowserApprovalNotification(
                        event_id=str(payload.get("event_id", "")),
                        approval_id=str(payload.get("approval_id", "")),
                        status=str(payload.get("status", "pending")),
                        agent_did=payload.get("agent_did")
                        if isinstance(payload.get("agent_did"), str)
                        else None,
                        tool=payload.get("tool") if isinstance(payload.get("tool"), str) else None,
                        classification=str(payload.get("classification", "UNCLASSIFIED")),
                    ).as_event()
                )
        return events[-256:]

    async def acknowledge(self, event_id: str) -> bool:
        """Acknowledge one event after the browser has shown it."""

        if not event_id or len(event_id) > 512:
            return False
        if self._backend is None:
            if event_id not in self._event_ids:
                return False
            self._event_ids.discard(event_id)
            self._events = deque(
                (event for event in self._events if event.event_id != event_id),
                maxlen=self._events.maxlen,
            )
            return True
        return bool(
            await self._backend.update_if(
                self._COLLECTION,
                self._key(event_id),
                {"state": "acked"},
                {"sink_id": self._SINK_ID, "state": "unacked"},
                actor_did="did:arc:ui:operator",
            )
        )

    @classmethod
    def _key(cls, event_id: str) -> str:
        if not event_id or len(event_id) > 512:
            raise ValueError("event_id must be a bounded non-empty identifier")
        return f"{cls._SINK_ID}:{event_id}"

    def recent(self) -> list[dict[str, object]]:
        return [event.as_event() for event in self._events]


__all__ = ["ApprovalNotificationHub", "BrowserApprovalNotification"]
