from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcstore.approval_dispatcher import (
    ApprovalDeliveryResult,
    ApprovalDispatcherConfig,
    ApprovalNotification,
    ApprovalNotificationDispatcher,
)
from arcstore.backends.memory import FakeBackend


class FakeOutbox:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.claims: list[tuple[str, int]] = []
        self.acks: list[list[str]] = []
        self.nacks: list[tuple[str, float]] = []
        self.rejects: list[str] = []

    async def claim_outbox(self, consumer_id: str, *, limit: int) -> list[dict[str, Any]]:
        self.claims.append((consumer_id, limit))
        claimed = self.rows[:limit]
        self.rows = self.rows[limit:]
        return claimed

    async def ack_outbox(self, consumer_id: str, event_ids: list[str]) -> None:
        self.acks.append(list(event_ids))

    async def nack_outbox(
        self, consumer_id: str, event_id: str, *, retry_after_seconds: float
    ) -> bool:
        self.nacks.append((event_id, retry_after_seconds))
        return True

    async def reject_outbox(self, consumer_id: str, event_id: str) -> bool:
        self.rejects.append(event_id)
        return True


def _row(event_id: str = "event-1", *, attempts: int = 1, **event: Any) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "approval_id": "approval-1",
        "event": {
            "approval_id": "approval-1",
            "status": "pending",
            "agent_did": "did:arc:test:agent",
            "tool": "send_message",
            **event,
        },
        "attempts": attempts,
    }


@pytest.mark.asyncio
async def test_bounded_claim_sanitized_sink_and_ack() -> None:
    backend = FakeOutbox([_row()])
    delivered: list[ApprovalNotification] = []

    async def sink(notification: ApprovalNotification) -> None:
        delivered.append(notification)

    worker = ApprovalNotificationDispatcher(
        backend,
        sink,
        ApprovalDispatcherConfig(worker_id="worker-a", batch_size=1),
    )
    assert await worker.dispatch_once() == 1
    assert backend.claims == [("worker-a", 1)]
    assert backend.acks == [["event-1"]]
    assert delivered[0].approval_id == "approval-1"
    assert delivered[0].model_dump() == {
        "event_id": "event-1",
        "approval_id": "approval-1",
        "status": "pending",
        "agent_did": "did:arc:test:agent",
        "tool": "send_message",
        "classification": "UNCLASSIFIED",
        "attempts": 1,
    }


@pytest.mark.asyncio
async def test_production_outbox_contract_with_memory_backend() -> None:
    backend = FakeBackend()
    await backend.start()
    await backend.mutable_write_with_outbox(
        "approvals",
        "approval-1",
        {"status": "pending"},
        event_id="event-1",
        event={"approval_id": "approval-1", "status": "pending", "tool": "send_message"},
        actor_did="did:arc:test:agent",
    )
    delivered: list[ApprovalNotification] = []

    async def sink(notification: ApprovalNotification) -> None:
        delivered.append(notification)

    worker = ApprovalNotificationDispatcher(
        backend,
        sink,
        ApprovalDispatcherConfig(worker_id="worker-a"),
    )
    assert await worker.dispatch_once() == 1
    assert [item.event_id for item in delivered] == ["event-1"]
    assert await backend.claim_outbox("worker-b") == []


@pytest.mark.asyncio
async def test_raw_secrets_and_unknown_fields_never_reach_sink() -> None:
    backend = FakeOutbox([_row(arguments={"token": "secret"}, secret="must-not-leak")])
    delivered: list[ApprovalNotification] = []

    async def sink(notification: ApprovalNotification) -> None:
        delivered.append(notification)

    worker = ApprovalNotificationDispatcher(
        backend,
        sink,
        ApprovalDispatcherConfig(worker_id="worker-a"),
    )
    await worker.dispatch_once()
    assert delivered[0].model_dump().keys() == {
        "event_id",
        "approval_id",
        "status",
        "agent_did",
        "tool",
        "classification",
        "attempts",
    }
    assert "secret" not in repr(delivered[0])


@pytest.mark.asyncio
async def test_sink_failure_nacks_with_deterministic_backoff() -> None:
    backend = FakeOutbox([_row(attempts=3)])

    async def sink(_notification: ApprovalNotification) -> None:
        raise RuntimeError("transport down")

    worker = ApprovalNotificationDispatcher(
        backend,
        sink,
        ApprovalDispatcherConfig(worker_id="worker-a", retry_base_seconds=2, retry_max_seconds=10),
    )
    await worker.dispatch_once()
    assert backend.acks == []
    assert backend.nacks == [("event-1", 8)]


@pytest.mark.asyncio
async def test_inflight_duplicate_requests_retry_without_acknowledging_outbox() -> None:
    backend = FakeOutbox([_row()])

    async def sink(_notification: ApprovalNotification) -> ApprovalDeliveryResult:
        return ApprovalDeliveryResult.RETRY

    worker = ApprovalNotificationDispatcher(
        backend,
        sink,
        ApprovalDispatcherConfig(worker_id="worker-a", retry_base_seconds=2),
    )

    await worker.dispatch_once()

    assert backend.acks == []
    assert backend.nacks == [("event-1", 2)]


@pytest.mark.asyncio
async def test_duplicate_after_ack_is_at_least_once_for_idempotent_sink() -> None:
    backend = FakeOutbox([_row()])
    delivered: list[str] = []
    worker = ApprovalNotificationDispatcher(
        backend,
        lambda event: _record(delivered, event),
        ApprovalDispatcherConfig(worker_id="worker-a"),
    )
    await worker.dispatch_once()
    backend.rows = [_row()]
    await worker.dispatch_once()
    assert delivered == ["event-1", "event-1"]
    assert backend.acks == [["event-1"], ["event-1"]]


@pytest.mark.asyncio
async def test_malformed_row_is_durably_rejected() -> None:
    backend = FakeOutbox([{"event_id": "", "approval_id": "a", "event": {}}])
    worker = ApprovalNotificationDispatcher(
        backend,
        lambda _event: asyncio.sleep(0),
        ApprovalDispatcherConfig(worker_id="worker-a"),
    )
    await worker.dispatch_once()
    assert backend.rejects == [""]


@pytest.mark.asyncio
async def test_validation_poison_is_rejected_not_retried() -> None:
    backend = FakeOutbox([_row(status="not-a-status")])
    worker = ApprovalNotificationDispatcher(
        backend,
        lambda _event: asyncio.sleep(0),
        ApprovalDispatcherConfig(worker_id="worker-a"),
    )
    await worker.dispatch_once()
    assert backend.rejects == ["event-1"]
    assert backend.nacks == []


async def _record(target: list[str], event: ApprovalNotification) -> None:
    target.append(event.event_id)


@pytest.mark.asyncio
async def test_start_stop_is_deterministic_and_cancellation_propagates() -> None:
    backend = FakeOutbox([])
    worker = ApprovalNotificationDispatcher(
        backend,
        lambda _event: asyncio.sleep(0),
        ApprovalDispatcherConfig(worker_id="worker-a", poll_interval_seconds=60),
    )
    await worker.start()
    await asyncio.sleep(0)
    await worker.stop()
    assert worker._task is None


@pytest.mark.asyncio
async def test_stop_cancels_an_inflight_sink_without_leaking_the_worker_task() -> None:
    backend = FakeOutbox([_row()])
    entered = asyncio.Event()

    async def sink(_notification: ApprovalNotification) -> None:
        entered.set()
        await asyncio.Event().wait()

    worker = ApprovalNotificationDispatcher(
        backend,
        sink,
        ApprovalDispatcherConfig(worker_id="worker-a"),
    )
    await worker.start()
    await entered.wait()
    task = worker._task
    await worker.stop()
    assert task is not None and task.cancelled()
    assert worker._task is None
