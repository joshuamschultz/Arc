"""Real PostgreSQL conformance gates, enabled only with an explicit test DSN."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from packages.arcstore.tests.inbox_conformance import (
    assert_inbox_repository_conforms,
    participant,
)

from arcstore.backends.base import ArcStoreBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.backends.postgres_inbox import PostgresInboxRepository

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


async def test_postgres_rejects_ambiguous_or_malformed_timestamps(
    postgres_backend: ArcStoreBackend,
) -> None:
    for timestamp in ("2026-08-22T00:00:00", "not-a-timestamp"):
        with pytest.raises(ValueError, match="timestamp"):
            await postgres_backend.upsert(
                "llm_calls", uuid4().hex, {"ts": timestamp, "actor_did": _ACTOR}
            )


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


async def test_postgres_inbox_repository_conformance(
    postgres_backend: ArcStoreBackend,
) -> None:
    assert isinstance(postgres_backend, PostgresBackend)
    await assert_inbox_repository_conforms(PostgresInboxRepository(postgres_backend))


async def test_postgres_inbox_uses_v1_foreign_keys_and_canonical_payloads(
    postgres_backend: ArcStoreBackend,
) -> None:
    assert isinstance(postgres_backend, PostgresBackend)
    repository = PostgresInboxRepository(postgres_backend)
    owner = participant(f"did:arc:human:{uuid4().hex}", "human")
    inbox = await repository.create_inbox(owner, classification="CUI")
    thread = await repository.create_thread(inbox.inbox_id, (owner,), subject="schema")
    message = await repository.append_message(
        thread.thread_id, sender=owner, recipients=(owner,), body="canonical"
    )
    async with postgres_backend._require_pool().acquire() as connection:
        inbox_row = await connection.fetchrow(
            "SELECT payload::text AS payload FROM inboxes WHERE inbox_id=$1", inbox.inbox_id
        )
        thread_row = await connection.fetchrow(
            "SELECT inbox_id, payload::text AS payload FROM inbox_threads WHERE thread_id=$1",
            thread.thread_id,
        )
        message_row = await connection.fetchrow(
            "SELECT thread_id, payload::text AS payload FROM inbox_messages WHERE message_id=$1",
            message.message_id,
        )
    assert inbox_row is not None and json.loads(inbox_row["payload"]) == inbox.model_dump(
        mode="json"
    )
    assert thread_row is not None and thread_row["inbox_id"] == inbox.inbox_id
    assert json.loads(thread_row["payload"])["thread_id"] == thread.thread_id
    assert message_row is not None and message_row["thread_id"] == thread.thread_id
    assert json.loads(message_row["payload"]) == message.model_dump(mode="json")


async def test_postgres_inbox_idempotent_event_survives_backend_restart(
    postgres_backend: ArcStoreBackend,
) -> None:
    """A retry after a pool restart reads the original durable event copy."""
    assert isinstance(postgres_backend, PostgresBackend)
    repository = PostgresInboxRepository(postgres_backend)
    owner = participant(f"did:arc:human:{uuid4().hex}", "human")
    inbox = await repository.create_inbox(owner, inbox_id=f"inbox-{uuid4().hex}")
    thread = await repository.create_thread(
        inbox.inbox_id,
        (owner,),
        thread_id=f"thread-{uuid4().hex}",
    )
    message_id = f"message-{uuid4().hex}"
    first = await repository.append_message(
        thread.thread_id,
        sender=owner,
        recipients=(owner,),
        body="survives restart",
        message_id=message_id,
    )

    await postgres_backend.stop()
    await postgres_backend.start()
    retry = await PostgresInboxRepository(postgres_backend).append_message(
        thread.thread_id,
        sender=owner,
        recipients=(owner,),
        body="survives restart",
        message_id=message_id,
    )

    assert retry == first
