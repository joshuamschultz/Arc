from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest
from arcstore.backends.postgres import PostgresBackend
from arcstore.backends.postgres_inbox import PostgresInboxRepository
from arcstore.config import ArcStoreConfig
from pydantic import SecretStr

from tools.one_time.arcstore_sqlite_to_postgres import (
    FakeDestination,
    MappedRow,
    MigrationError,
    PostgresDestination,
    digest,
    migrate,
)


def _source_db(path: Path, *, unknown: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE arcstore_schema(version INTEGER);
        CREATE TABLE llm_calls(record_key TEXT PRIMARY KEY, payload TEXT, ts TEXT);
        CREATE TABLE mutable_records(collection TEXT, key TEXT, value TEXT, updated_at TEXT,
            PRIMARY KEY(collection, key));
        CREATE TABLE sync_state(source TEXT PRIMARY KEY, offset INTEGER);
        CREATE TABLE inboxes(inbox_id TEXT PRIMARY KEY, payload TEXT, created_at TEXT);
        CREATE TABLE inbox_threads(thread_id TEXT PRIMARY KEY, inbox_id TEXT, payload TEXT,
            updated_at TEXT);
        CREATE TABLE inbox_messages(message_id TEXT PRIMARY KEY, thread_id TEXT, payload TEXT,
            created_at TEXT);
        CREATE TABLE inbox_handoffs(handoff_id TEXT PRIMARY KEY, thread_id TEXT, payload TEXT,
            created_at TEXT);
        CREATE TABLE approval_outbox(event_id TEXT PRIMARY KEY, approval_id TEXT, payload TEXT,
            status TEXT, attempts INTEGER, available_at TEXT, lease_owner TEXT, lease_until TEXT,
            delivered_at TEXT, created_at TEXT);
        """
    )
    stamp = "2026-08-22T00:00:00+00:00"
    owner = '{"participant_id":"did:arc:human:owner","role":"human","display_name":"Owner"}'
    inbox = (
        '{"inbox_id":"inbox-1","owner":'
        + owner
        + ',"classification":"CUI","created_at":"'
        + stamp
        + '"}'
    )
    participant = owner
    thread = (
        '{"thread_id":"thread-1","inbox_id":"inbox-1","participants":['
        + participant
        + '],"subject":"migration","classification":"CUI","status":"open",'
        + '"created_at":"'
        + stamp
        + '","updated_at":"'
        + stamp
        + '","last_message_id":null,"unread_count":0}'
    )
    message = (
        '{"message_id":"message-1","thread_id":"thread-1","sender":'
        + participant
        + ',"recipients":['
        + participant
        + '],"body":"hello","reply_to_id":null,"trace":{"classification":"CUI"},'
        + '"created_at":"'
        + stamp
        + '","read_receipts":[]}'
    )
    handoff = (
        '{"handoff_id":"handoff-1","thread_id":"thread-1","from_participant":'
        + participant
        + ',"to_participants":['
        + participant
        + '],"source_message_id":"message-1","trace":{"classification":"CUI"},'
        + '"created_at":"'
        + stamp
        + '","status":"pending"}'
    )
    connection.execute(
        "INSERT INTO llm_calls VALUES (?, ?, ?)",
        ("call-1", '{"kind":"llm_call","actor_did":"did:arc:test"}', stamp),
    )
    connection.execute(
        "INSERT INTO mutable_records VALUES (?, ?, ?, ?)",
        ("tasks", "task-1", '{"state":"ready"}', stamp),
    )
    connection.execute("INSERT INTO sync_state VALUES (?, ?)", ("spool:one", 42))
    connection.execute("INSERT INTO inboxes VALUES (?, ?, ?)", ("inbox-1", inbox, stamp))
    connection.execute(
        "INSERT INTO inbox_threads VALUES (?, ?, ?, ?)", ("thread-1", "inbox-1", thread, stamp)
    )
    connection.execute(
        "INSERT INTO inbox_messages VALUES (?, ?, ?, ?)",
        ("message-1", "thread-1", message, stamp),
    )
    connection.execute(
        "INSERT INTO inbox_handoffs VALUES (?, ?, ?, ?)",
        ("handoff-1", "thread-1", handoff, stamp),
    )
    connection.execute(
        "INSERT INTO approval_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "event-1",
            "approval-1",
            '{"status":"pending"}',
            "pending",
            0,
            stamp,
            None,
            None,
            None,
            stamp,
        ),
    )
    if unknown:
        connection.execute("CREATE TABLE unrecognized_legacy_table(value TEXT)")
    connection.commit()
    connection.close()


@pytest.mark.asyncio
async def test_dry_run_is_read_only_and_reports_canonical_destination_tables(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    backup = tmp_path / "legacy.backup.db"
    _source_db(source)
    before = source.read_bytes()
    report = await migrate(source, FakeDestination(), dry_run=True, backup_path=backup)
    assert source.read_bytes() == before
    assert backup.exists()
    assert report.tables["sync_state"]["destination_table"] == "arcstore_cursors"
    assert report.tables["inbox_threads"]["destination_table"] == "inbox_threads"
    assert report.tables["approval_outbox"]["destination_table"] == "approval_outbox"
    assert report.mapped_tables == [
        "inboxes",
        "inbox_threads",
        "inbox_messages",
        "inbox_handoffs",
        "approval_outbox",
        "llm_calls",
        "mutable_records",
        "sync_state",
    ]
    assert report.skipped_tables == ["arcstore_schema"]


@pytest.mark.asyncio
async def test_resume_and_digest_verification_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _source_db(source)
    destination = FakeDestination()
    first = await migrate(source, destination, batch_size=1)
    second = await migrate(source, destination, batch_size=1, resume=True)
    assert (
        first.tables["inbox_messages"]["destination_digest"]
        == first.tables["inbox_messages"]["source_digest"]
    )
    assert second.tables["llm_calls"]["migrated_count"] == 0


@pytest.mark.asyncio
async def test_unknown_sqlite_table_fails_before_destination_write(tmp_path: Path) -> None:
    source = tmp_path / "unknown.db"
    _source_db(source, unknown=True)
    destination = FakeDestination()
    with pytest.raises(MigrationError, match="unknown source tables"):
        await migrate(source, destination)
    assert destination.writes == 0


@pytest.mark.asyncio
async def test_deleted_sqlite_backend_flat_operational_shapes_are_canonicalized(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old-flat.db"
    connection = sqlite3.connect(source)
    connection.executescript(
        """
        CREATE TABLE llm_calls(record_id TEXT PRIMARY KEY, kind TEXT, actor_did TEXT,
            ts TEXT, extra TEXT);
        CREATE TABLE skill_candidate_bodies(record_id TEXT PRIMARY KEY, body TEXT);
        """
    )
    connection.execute(
        "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?)",
        (
            "call-flat",
            "llm_call",
            "did:arc:flat",
            "2026-08-22T00:00:00Z",
            '{"cache":"hit"}',
        ),
    )
    connection.execute("INSERT INTO skill_candidate_bodies VALUES (?, ?)", ("body-flat", "body"))
    connection.commit()
    connection.close()
    destination = FakeDestination()
    await migrate(source, destination)
    call = destination.rows["llm_calls"]["call-flat"].values
    body = destination.rows["skill_candidate_bodies"]["body-flat"].values
    assert call["payload"] == {
        "actor_did": "did:arc:flat",
        "extra": {"cache": "hit"},
        "kind": "llm_call",
        "record_id": "call-flat",
        "ts": "2026-08-22T00:00:00+00:00",
    }
    assert body == {
        "record_key": "body-flat",
        "payload": {"body": "body", "record_id": "body-flat"},
        "ts": None,
    }


def test_digest_normalizes_equivalent_postgres_and_model_utc_timestamps() -> None:
    model_payload = {
        "inbox_id": "inbox-1",
        "created_at": "2026-08-22T00:00:00Z",
        "owner": {"participant_id": "did:arc:owner"},
    }
    planned = {
        "inbox_id": "inbox-1",
        "payload": model_payload,
        "created_at": "2026-08-22T00:00:00Z",
    }
    readback = {
        "inbox_id": "inbox-1",
        "payload": model_payload,
        "created_at": "2026-08-22T00:00:00+00:00",
    }
    assert digest(planned) == digest(readback)


@pytest.mark.asyncio
async def test_actual_postgres_migration_and_inbox_repository_readback(tmp_path: Path) -> None:
    dsn = os.environ.get("ARCSTORE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ARCSTORE_TEST_DATABASE_URL is required for real PostgreSQL migration test")
    source = tmp_path / "legacy.db"
    _source_db(source)
    connection = sqlite3.connect(source)
    connection.execute(f"PRAGMA user_version = {int(uuid4().int % 2_000_000_000)}")
    connection.commit()
    connection.close()
    backend = PostgresBackend(ArcStoreConfig().postgres_settings(SecretStr(dsn)))

    class InterruptAfterFirstBatch(PostgresDestination):
        def __init__(self, destination_dsn: str) -> None:
            super().__init__(destination_dsn)
            self.batch_calls = 0

        async def write_batch(
            self, migration_id: str, table: str, rows: Sequence[MappedRow]
        ) -> None:
            self.batch_calls += 1
            if self.batch_calls > 1:
                raise MigrationError("intentional interruption after committed checkpoint")
            await super().write_batch(migration_id, table, rows)

    interrupted = InterruptAfterFirstBatch(dsn)
    destination: PostgresDestination | None = None
    await backend.start()
    try:
        with pytest.raises(MigrationError, match="intentional interruption"):
            await migrate(source, interrupted, batch_size=1)
        assert interrupted.writes == 1
        await interrupted.close()
        destination = PostgresDestination(dsn)
        report = await migrate(source, destination, batch_size=1, resume=True)
        repository = PostgresInboxRepository(backend)
        thread = await repository.get_thread(
            "thread-1", reader_id="did:arc:human:owner", classification_max="CUI"
        )
        messages = await repository.list_messages(
            thread.thread_id, reader_id="did:arc:human:owner", classification_max="CUI"
        )
        assert report.tables["inbox_messages"]["destination_count"] == 1
        assert thread.inbox_id == "inbox-1"
        assert [message.message_id for message in messages.items] == ["message-1"]
        mutable = await backend.mutable_read("tasks", "task-1")
        assert mutable is not None and mutable["state"] == "ready"
        assert await backend.get_cursor("spool:one") == 42
    finally:
        await interrupted.close()
        if destination is not None:
            await destination.close()
        await backend.stop()
