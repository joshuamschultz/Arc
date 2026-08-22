from __future__ import annotations

from uuid import uuid4

from arcstore.approval_dispatcher import (
    ApprovalDispatcherConfig,
    ApprovalNotification,
    ApprovalNotificationDispatcher,
)
from arcstore.backends.base import ArcStoreBackend


async def test_dispatcher_reads_and_acks_real_postgres_outbox(
    postgres_backend: ArcStoreBackend,
) -> None:
    approval_id = f"dispatcher-approval-{uuid4().hex}"
    event_id = f"dispatcher-event-{uuid4().hex}"
    await postgres_backend.mutable_write_with_outbox(
        "approvals",
        approval_id,
        {"status": "pending"},
        event_id=event_id,
        event={
            "approval_id": approval_id,
            "status": "pending",
            "agent_did": "did:arc:test:agent",
            "tool": "send_message",
        },
        actor_did="did:arc:test:agent",
    )
    delivered: list[str] = []

    async def sink(notification: ApprovalNotification) -> None:
        delivered.append(notification.event_id)

    dispatcher = ApprovalNotificationDispatcher(
        postgres_backend,
        sink,
        ApprovalDispatcherConfig(worker_id=f"dispatcher-{uuid4().hex}"),
    )
    await dispatcher.dispatch_once()
    assert event_id in delivered
