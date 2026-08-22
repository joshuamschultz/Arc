"""Durable per-sink delivery state for approval notifications.

The approval outbox is at-least-once.  A sink therefore needs a durable key of
its own: acknowledging the outbox row is not enough because a process can die
after a sink accepts the event and before the dispatcher acknowledges it.

This helper deliberately uses ArcStore's mutable plane instead of a process
global.  PostgreSQL's ``mutable_create_batch`` is an insert-if-absent operation
under the backend transaction, while the in-memory backend provides the same
contract for tests and local operation.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from arcstore.approval_dispatcher import ApprovalDeliveryResult, ApprovalNotification


class ApprovalDeliveryBackend(Protocol):
    async def mutable_create_batch(
        self,
        collection: str,
        entries: list[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        absent_where: dict[str, Any] | None = None,
    ) -> bool: ...


_COLLECTION = "approval_notification_deliveries"
_ACTOR = "did:arc:system:approval-notification"


@dataclass(frozen=True)
class ApprovalDeliveryLease:
    """An opaque ownership token for one in-flight notification delivery."""

    owner_token: str | None
    result: ApprovalDeliveryResult | None = None


def notification_payload(notification: ApprovalNotification) -> dict[str, Any]:
    """Return the bounded, browser/gateway-safe projection for persistence."""

    return notification.model_dump(mode="json")


class DurableApprovalDelivery:
    """Insert-if-absent delivery records for one named sink.

    ``claim`` creates a leased owner token. A completed record is never sent
    again after a restart; an unexpired foreign lease asks the outbox to retry;
    and a crashed worker's expired lease can be reclaimed. Completion and
    release both require the exact owner token, so a stale worker cannot
    acknowledge a newer worker's claim.
    """

    def __init__(
        self,
        backend: ApprovalDeliveryBackend,
        *,
        sink_id: str,
        lease_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not sink_id or len(sink_id) > 128 or ":" in sink_id:
            raise ValueError("sink_id must be a bounded identifier without ':'")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._backend = backend
        self._sink_id = sink_id
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def _key(self, event_id: str) -> str:
        if not event_id or len(event_id) > 512:
            raise ValueError("event_id must be a bounded non-empty identifier")
        return f"{self._sink_id}:{event_id}"

    async def claim(self, notification: ApprovalNotification) -> ApprovalDeliveryLease:
        """Claim an event or return its durable outbox disposition."""

        key = self._key(notification.event_id)
        token = secrets.token_urlsafe(18)
        expires_at = self._lease_expires_at()
        rows = await self._backend.mutable_create_batch(
            _COLLECTION,
            [
                (
                    key,
                    {
                        "event_id": notification.event_id,
                        "sink_id": self._sink_id,
                        "state": "inflight",
                        "claim_token": token,
                        "claim_expires_at": expires_at,
                        "notification": notification_payload(notification),
                    },
                )
            ],
            actor_did=_ACTOR,
        )
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            return ApprovalDeliveryLease(None, ApprovalDeliveryResult.RETRY)
        if row.get("state") == "delivered":
            return ApprovalDeliveryLease(None, ApprovalDeliveryResult.DUPLICATE_COMPLETE)
        if row.get("state") == "pending" and row.get("claim_token") == "":
            claimed = await self._backend.update_if(
                _COLLECTION,
                key,
                {
                    "claim_token": token,
                    "claim_expires_at": expires_at,
                    "state": "inflight",
                },
                {"state": "pending", "claim_token": ""},
                actor_did=_ACTOR,
            )
            return self._owned_or_retry(token, claimed)
        if row.get("claim_token") == token and row.get("state") == "inflight":
            return ApprovalDeliveryLease(token)
        if row.get("state") != "inflight" or not self._lease_expired(row):
            return ApprovalDeliveryLease(None, ApprovalDeliveryResult.RETRY)
        previous_token = row.get("claim_token")
        previous_expires_at = row.get("claim_expires_at")
        if not isinstance(previous_token, str) or not isinstance(previous_expires_at, str):
            return ApprovalDeliveryLease(None, ApprovalDeliveryResult.RETRY)
        claimed = await self._backend.update_if(
            _COLLECTION,
            key,
            {"claim_token": token, "claim_expires_at": expires_at},
            {
                "state": "inflight",
                "claim_token": previous_token,
                "claim_expires_at": previous_expires_at,
            },
            actor_did=_ACTOR,
        )
        return self._owned_or_retry(token, claimed)

    async def complete(
        self, notification: ApprovalNotification, lease: ApprovalDeliveryLease
    ) -> ApprovalDeliveryResult:
        """Mark a successful send complete only when this worker still owns it."""

        key = self._key(notification.event_id)
        token = lease.owner_token
        if not token:
            return ApprovalDeliveryResult.RETRY
        completed = await self._backend.update_if(
            _COLLECTION,
            key,
            {"state": "delivered", "claim_token": "", "claim_expires_at": ""},
            {"state": "inflight", "claim_token": token},
            actor_did=_ACTOR,
        )
        return ApprovalDeliveryResult.DELIVERED if completed else ApprovalDeliveryResult.RETRY

    async def release(
        self, notification: ApprovalNotification, lease: ApprovalDeliveryLease
    ) -> bool:
        """Release a failed claim so the dispatcher can retry it."""

        key = self._key(notification.event_id)
        token = lease.owner_token
        if not token:
            return False
        return await self._backend.update_if(
            _COLLECTION,
            key,
            {"state": "pending", "claim_token": "", "claim_expires_at": ""},
            {"state": "inflight", "claim_token": token},
            actor_did=_ACTOR,
        )

    def _lease_expires_at(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("approval delivery clock must return a timezone-aware datetime")
        return (now.astimezone(UTC) + timedelta(seconds=self._lease_seconds)).isoformat()

    def _lease_expired(self, row: dict[str, Any]) -> bool:
        expires_at = row.get("claim_expires_at")
        if not isinstance(expires_at, str):
            return False
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires.tzinfo is None or expires.utcoffset() is None:
            return False
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("approval delivery clock must return a timezone-aware datetime")
        return expires <= now.astimezone(UTC)

    @staticmethod
    def _owned_or_retry(token: str, claimed: bool) -> ApprovalDeliveryLease:
        if claimed:
            return ApprovalDeliveryLease(token)
        return ApprovalDeliveryLease(None, ApprovalDeliveryResult.RETRY)

    async def records(self) -> list[dict[str, Any]]:
        """Read this sink's durable records for recovery/replay surfaces."""

        return await self._backend.mutable_query(_COLLECTION, where={"sink_id": self._sink_id})


__all__ = [
    "ApprovalDeliveryBackend",
    "ApprovalDeliveryLease",
    "DurableApprovalDelivery",
    "notification_payload",
]
