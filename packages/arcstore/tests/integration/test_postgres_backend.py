"""Real PostgreSQL conformance gates, enabled only with an explicit test DSN."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from arcstore.backends.base import ArcStoreBackend

_ACTOR = "did:arc:test:postgres"


async def test_postgres_schema_and_operational_round_trip(
    postgres_backend: ArcStoreBackend,
) -> None:
    key = f"pg-{uuid4().hex}"
    await postgres_backend.upsert(
        "llm_calls",
        key,
        {"kind": "llm_call", "actor_did": _ACTOR, "ts": "2026-08-22T00:00:00Z"},
    )
    rows = await postgres_backend.query("llm_calls", where={"record_id": key})
    assert rows == [
        {"record_id": key, "kind": "llm_call", "actor_did": _ACTOR, "ts": "2026-08-22T00:00:00Z"}
    ]


async def test_postgres_cas_serializes_cross_row_claim_guard(
    postgres_backend: ArcStoreBackend,
) -> None:
    collection = f"claims-{uuid4().hex}"
    await postgres_backend.mutable_write(collection, "one", {"owner": None}, actor_did=_ACTOR)
    await postgres_backend.mutable_write(collection, "two", {"owner": None}, actor_did=_ACTOR)

    async def claim(key: str) -> bool:
        return await postgres_backend.update_if(
            collection,
            key,
            {"owner": _ACTOR},
            {"owner": None},
            absent_where={"owner": _ACTOR},
            actor_did=_ACTOR,
        )

    first, second = await asyncio.gather(claim("one"), claim("two"))
    assert first != second


async def test_postgres_outbox_create_claim_ack_and_retry(
    postgres_backend: ArcStoreBackend,
) -> None:
    approval_id = f"approval-{uuid4().hex}"
    event_id = f"event-{uuid4().hex}"
    await postgres_backend.mutable_write_with_outbox(
        "approvals",
        approval_id,
        {"status": "pending"},
        event_id=event_id,
        event={"approval_id": approval_id, "status": "pending"},
        actor_did=_ACTOR,
    )
    claimed = await postgres_backend.claim_outbox("worker-a")
    event = next(item for item in claimed if item["event_id"] == event_id)
    assert event["event"]["status"] == "pending"
    assert await postgres_backend.nack_outbox("worker-a", event_id, retry_after_seconds=0)
    reclaimed = await postgres_backend.claim_outbox("worker-b")
    assert any(item["event_id"] == event_id for item in reclaimed)
    await postgres_backend.ack_outbox("worker-b", [event_id])
    assert not any(
        item["event_id"] == event_id for item in await postgres_backend.claim_outbox("worker-c")
    )
