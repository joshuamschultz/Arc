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
from typing import Any, Protocol

from arcstore.approval_dispatcher import ApprovalNotification


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


def notification_payload(notification: ApprovalNotification) -> dict[str, Any]:
    """Return the bounded, browser/gateway-safe projection for persistence."""

    return notification.model_dump(mode="json")


class DurableApprovalDelivery:
    """Insert-if-absent delivery records for one named sink.

    ``claim`` returns true exactly for the worker that owns a new record.  A
    completed record is never sent again after a restart.  Failed sends release
    their claim, allowing the outbox retry to make progress.  A claim is kept
    through the external send, so concurrent workers cannot both send the same
    event.  The external platform call remains responsible for its own
    retry/transport semantics; this class owns Arc's durable idempotency key.
    """

    def __init__(self, backend: ApprovalDeliveryBackend, *, sink_id: str) -> None:
        if not sink_id or len(sink_id) > 128 or ":" in sink_id:
            raise ValueError("sink_id must be a bounded identifier without ':'")
        self._backend = backend
        self._sink_id = sink_id

    def _key(self, event_id: str) -> str:
        if not event_id or len(event_id) > 512:
            raise ValueError("event_id must be a bounded non-empty identifier")
        return f"{self._sink_id}:{event_id}"

    async def claim(self, notification: ApprovalNotification) -> bool:
        """Claim one event, returning false for delivered or concurrent work."""

        key = self._key(notification.event_id)
        token = secrets.token_urlsafe(18)
        rows = await self._backend.mutable_create_batch(
            _COLLECTION,
            [
                (
                    key,
                    {
                        "event_id": notification.event_id,
                        "sink_id": self._sink_id,
                        "state": "pending",
                        "claim_token": token,
                        "notification": notification_payload(notification),
                    },
                )
            ],
            actor_did=_ACTOR,
        )
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            return False
        if row.get("state") == "delivered":
            return False
        if row.get("state") == "pending" and row.get("claim_token") == "":
            # A previous attempt failed and released its claim.  CAS the empty
            # token so only one retry wins across processes.
            claimed = await self._backend.update_if(
                _COLLECTION,
                key,
                {"claim_token": token, "state": "inflight"},
                {"state": "pending", "claim_token": ""},
                actor_did=_ACTOR,
            )
            return claimed
        # ``mutable_create_batch`` returns the already-existing row on a race.
        # Only the creator's token may transition pending -> inflight.
        if row.get("claim_token") != token:
            return False
        return await self._backend.update_if(
            _COLLECTION,
            key,
            {"state": "inflight"},
            {"state": "pending", "claim_token": token},
            actor_did=_ACTOR,
        )

    async def complete(self, notification: ApprovalNotification) -> None:
        key = self._key(notification.event_id)
        row = await self._backend.mutable_read(_COLLECTION, key)
        token = row.get("claim_token") if isinstance(row, dict) else None
        if not isinstance(token, str) or not token:
            return
        await self._backend.update_if(
            _COLLECTION,
            key,
            {"state": "delivered", "claim_token": ""},
            {"state": "inflight", "claim_token": token},
            actor_did=_ACTOR,
        )

    async def release(self, notification: ApprovalNotification) -> None:
        """Release a failed claim so the dispatcher can retry it."""

        key = self._key(notification.event_id)
        row = await self._backend.mutable_read(_COLLECTION, key)
        token = row.get("claim_token") if isinstance(row, dict) else None
        if not isinstance(token, str) or not token:
            return
        await self._backend.update_if(
            _COLLECTION,
            key,
            {"state": "pending", "claim_token": ""},
            {"state": "inflight", "claim_token": token},
            actor_did=_ACTOR,
        )

    async def records(self) -> list[dict[str, Any]]:
        """Read this sink's durable records for recovery/replay surfaces."""

        return await self._backend.mutable_query(_COLLECTION, where={"sink_id": self._sink_id})


__all__ = ["ApprovalDeliveryBackend", "DurableApprovalDelivery", "notification_payload"]
