"""Backend-neutral ArcStore contract tests.

The suite deliberately talks to the public async backend seam only.  A backend
adapter (memory, PostgreSQL, or another durable implementation) can provide the
``arcstore_backend`` fixture and run these tests without exposing a driver
connection, SQL, or transaction object to the callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.base import (
    AUDIT_TABLE,
    APPROVAL_OUTBOX_TABLE,
    SKILL_BODIES_TABLE,
    SKILL_CANDIDATES_TABLE,
    ArcStoreBackend,
)
from arcstore.backends.memory import FakeBackend


_ACTOR = "did:arc:agent:test"


@pytest.fixture
async def arcstore_backend() -> AsyncIterator[ArcStoreBackend]:
    """Use the driver-free backend for the contract's default execution.

    PostgreSQL conformance runs may override this fixture at the package or
    invocation level; no test below imports or inspects a concrete driver.
    """

    backend = FakeBackend()
    await backend.start()
    yield backend
    await backend.stop()


def _row(key: str, *, ts: str = "2026-08-22T00:00:00Z") -> dict[str, Any]:
    return {
        "record_id": key,
        "kind": "llm_call",
        "actor_did": _ACTOR,
        "ts": ts,
        "extra": {"trace": key},
    }


async def test_operational_upsert_query_and_batch_are_idempotent(
    arcstore_backend: ArcStoreBackend,
) -> None:
    await arcstore_backend.upsert("llm_calls", "one", _row("one"))
    await arcstore_backend.upsert_many(
        "llm_calls", [("two", _row("two")), ("three", _row("three"))]
    )
    await arcstore_backend.upsert("llm_calls", "one", _row("one", ts="2099-01-01T00:00:00Z"))

    rows = await arcstore_backend.query("llm_calls", order_by="ts ASC")
    assert [row["record_id"] for row in rows] == ["one", "two", "three"]
    assert rows[0]["ts"] == "2026-08-22T00:00:00Z"


async def test_operational_query_filters_before_limit(
    arcstore_backend: ArcStoreBackend,
) -> None:
    await arcstore_backend.upsert_many(
        "llm_calls",
        [
            ("old", _row("old", ts="2026-08-21T00:00:00Z")),
            ("new", _row("new", ts="2026-08-22T00:00:00Z")),
            ("newer", _row("newer", ts="2026-08-23T00:00:00Z")),
        ],
    )
    rows = await arcstore_backend.query(
        "llm_calls", ts_gte="2026-08-22T00:00:00Z", order_by="ts ASC", limit=1
    )
    assert [row["record_id"] for row in rows] == ["new"]


async def test_audit_and_skill_tables_round_trip_structured_rows(
    arcstore_backend: ArcStoreBackend,
) -> None:
    audit = {"record_id": "audit-1", "seq": 1, "verified": True, "extra": {"source": "test"}}
    candidate = {
        "record_id": "candidate-1",
        "skill_name": "release",
        "candidate_id": "candidate-1",
        "generation": 1,
        "active": True,
        "scores": {"quality": 0.9},
    }
    body = {"record_id": "body-1", "body": "body text"}
    await arcstore_backend.upsert(AUDIT_TABLE, "audit-1", audit)
    await arcstore_backend.upsert(SKILL_CANDIDATES_TABLE, "candidate-1", candidate)
    await arcstore_backend.upsert(SKILL_BODIES_TABLE, "body-1", body)

    assert (await arcstore_backend.query(AUDIT_TABLE))[0]["extra"] == {"source": "test"}
    assert (await arcstore_backend.query(SKILL_CANDIDATES_TABLE))[0]["scores"] == {
        "quality": 0.9
    }
    assert (await arcstore_backend.query(SKILL_BODIES_TABLE))[0]["body"] == "body text"


async def test_cursor_is_durable_and_monotonic_by_name(
    arcstore_backend: ArcStoreBackend,
) -> None:
    assert await arcstore_backend.get_cursor("spool.jsonl") == 0
    await arcstore_backend.set_cursor("spool.jsonl", 128)
    assert await arcstore_backend.get_cursor("spool.jsonl") == 128
    await arcstore_backend.set_cursor("other.jsonl", 7)
    assert await arcstore_backend.get_cursor("spool.jsonl") == 128


async def test_mutable_crud_merge_cas_increment_and_batch(
    arcstore_backend: ArcStoreBackend,
) -> None:
    await arcstore_backend.mutable_write(
        "tasks", "task-1", {"status": "todo", "attempts": 0}, actor_did=_ACTOR
    )
    assert (await arcstore_backend.mutable_read("tasks", "task-1"))["status"] == "todo"  # type: ignore[index]
    assert await arcstore_backend.mutable_merge(
        "tasks", "task-1", {"label": "important"}, actor_did=_ACTOR
    )
    assert await arcstore_backend.update_if(
        "tasks", "task-1", {"status": "done"}, {"status": "todo"}, actor_did=_ACTOR
    )
    assert not await arcstore_backend.update_if(
        "tasks", "task-1", {"status": "lost"}, {"status": "todo"}, actor_did=_ACTOR
    )
    assert await arcstore_backend.mutable_increment(
        "tasks", "task-1", {"attempts": 2}, actor_did=_ACTOR
    )
    row = await arcstore_backend.mutable_read("tasks", "task-1")
    assert row is not None
    assert row["status"] == "done"
    assert row["label"] == "important"
    assert row["attempts"] == 2

    created = await arcstore_backend.mutable_create_batch(
        "tasks",
        [("task-2", {"status": "todo"}), ("task-3", {"status": "todo"})],
        actor_did=_ACTOR,
    )
    assert [item["status"] for item in created] == ["todo", "todo"]
    assert await arcstore_backend.mutable_delete("tasks", "task-3", actor_did=_ACTOR)
    assert await arcstore_backend.mutable_read("tasks", "task-3") is None


async def test_approval_resolution_is_single_winner(
    arcstore_backend: ArcStoreBackend,
) -> None:
    store = ApprovalStore(arcstore_backend)
    approval = await store.create(
        PendingApproval(id="approval-1", agent_did=_ACTOR, tool="send", call_hash="hash")
    )
    first = await store.resolve(
        approval.id, status="approved", actor_did="did:arc:human:one", resolved_by="one"
    )
    second = await store.resolve(
        approval.id, status="denied", actor_did="did:arc:human:two", resolved_by="two"
    )
    assert first is not None and first.status == "approved"
    assert second is None
    assert (await store.get(approval.id)).status == "approved"  # type: ignore[union-attr]


async def test_approval_mutation_and_outbox_share_one_commit(
    arcstore_backend: ArcStoreBackend,
) -> None:
    """A winning approval transition must enqueue exactly one durable event.

    ``update_if_with_outbox`` is the transaction boundary: a concurrent or
    repeated resolver may not publish an event when its conditional mutation
    loses, and a successful mutation must never leave the outbox empty.
    """

    await arcstore_backend.mutable_write(
        "approvals", "approval-1", {"status": "pending"}, actor_did=_ACTOR
    )
    event = {"approval_id": "approval-1", "status": "approved"}
    won = await arcstore_backend.update_if_with_outbox(
        "approvals",
        "approval-1",
        {"status": "approved"},
        {"status": "pending"},
        event_id="approval-event-1",
        event=event,
        actor_did="did:arc:human:operator",
    )
    lost = await arcstore_backend.update_if_with_outbox(
        "approvals",
        "approval-1",
        {"status": "denied"},
        {"status": "pending"},
        event_id="approval-event-2",
        event={"approval_id": "approval-1", "status": "denied"},
        actor_did="did:arc:human:other",
    )

    assert won is True
    assert lost is False
    assert (await arcstore_backend.mutable_read("approvals", "approval-1"))["status"] == "approved"  # type: ignore[index]
    outbox = await arcstore_backend.query(APPROVAL_OUTBOX_TABLE)
    assert len(outbox) == 1
    assert outbox[0]["record_id"] == "approval-event-1"
    assert outbox[0]["extra"] == event
