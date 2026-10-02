"""Listing one connection's sync rows, with live-lease deadlines (P18-1 card API)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import ArcStoreSourceSyncStore, InMemorySourceSyncStore


async def _seed(store: InMemorySourceSyncStore | ArcStoreSourceSyncStore) -> None:
    await store.acquire_lease("did:a", "gmail", "owner-a", ttl_seconds=60)
    await store.acquire_lease("did:b", "gmail:inbox", "owner-b", ttl_seconds=60)
    await store.set_status("did:b", "gmail:inbox", "failed", owner_id="owner-b", fencing_token=1)
    await store.acquire_lease("did:a", "gmail_work", "owner-c", ttl_seconds=60)
    await store.acquire_lease("did:a", "jira", "owner-d", ttl_seconds=60)


@pytest.mark.parametrize("flavour", ["in_memory", "arcstore"])
async def test_lists_the_instance_and_its_suffixed_sources_only(flavour: str) -> None:
    store = (
        InMemorySourceSyncStore()
        if flavour == "in_memory"
        else ArcStoreSourceSyncStore(FakeBackend())
    )
    await _seed(store)

    rows = await store.list_for_connection("gmail")

    assert sorted(row.source_id for row in rows) == ["gmail", "gmail:inbox"]
    live = {row.source_id: row.is_live(datetime.now(UTC)) for row in rows}
    assert live == {"gmail": True, "gmail:inbox": False}


@pytest.mark.parametrize("flavour", ["in_memory", "arcstore"])
async def test_a_running_row_with_an_expired_lease_is_not_live(flavour: str) -> None:
    store = (
        InMemorySourceSyncStore()
        if flavour == "in_memory"
        else ArcStoreSourceSyncStore(FakeBackend())
    )
    await store.acquire_lease("did:a", "gmail", "owner-a", ttl_seconds=60)

    rows = await store.list_for_connection("gmail")

    assert rows[0].status.value == "running"
    assert rows[0].is_live(datetime.now(UTC) + timedelta(minutes=5)) is False


async def test_postgres_migration_13_replaces_health_with_status() -> None:
    from importlib.resources import files

    sql = files("arcstore.migrations").joinpath("v13.sql").read_text(encoding="utf-8")

    assert "value - 'health'" in sql
    for key in ("'status'", "'revision'", "'notice_claim'", "'notified_seq'", "'next_check_at'"):
        assert key in sql
