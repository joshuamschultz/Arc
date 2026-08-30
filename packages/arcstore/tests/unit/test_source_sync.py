from __future__ import annotations

import pytest

from arcstore.source_sync import InMemorySourceSyncStore


@pytest.mark.asyncio
async def test_schema_head_3_upgrades_to_source_sync_and_generation_fence() -> None:
    from arcstore.migrations import migrate

    executed: list[str] = []

    class Connection:
        async def execute(self, sql: str, *args: object) -> None:
            executed.append(sql)

        async def fetchval(self, sql: str, *args: object) -> int:
            return 3

    await migrate(Connection())
    assert any("connected_source_sync" in sql and "agent_did" in sql for sql in executed)
    assert any("generation" in sql for sql in executed)
    assert any("VALUES ($1)" in sql for sql in executed)


@pytest.mark.asyncio
async def test_compare_and_acquire_fences_previous_owner() -> None:
    store = InMemorySourceSyncStore()
    first = await store.acquire_lease("did:a", "source", "one", ttl_seconds=60)
    assert first is not None
    assert await store.acquire_lease("did:a", "source", "two", ttl_seconds=60) is None
    assert await store.commit_page(
        "did:a",
        "source",
        expected_cursor=None,
        next_cursor="c1",
        page_id="p1",
        page_count=1,
        page_bytes=3,
        owner_id="one",
        fencing_token=first.fencing_token,
    )
    assert (await store.get_state("did:a", "source")).cursor == "c1"


@pytest.mark.asyncio
async def test_expected_cursor_and_page_id_make_commit_idempotent() -> None:
    store = InMemorySourceSyncStore()
    lease = await store.acquire_lease("did:a", "source", "one", ttl_seconds=60)
    assert lease is not None
    kwargs = {
        "expected_cursor": None,
        "next_cursor": "c1",
        "page_id": "p1",
        "page_count": 1,
        "page_bytes": 3,
        "owner_id": "one",
        "fencing_token": lease.fencing_token,
    }
    assert await store.commit_page("did:a", "source", **kwargs)
    assert await store.commit_page("did:a", "source", **kwargs)
    assert (await store.get_state("did:a", "source")).pages == 1
    assert not await store.commit_page(
        "did:a",
        "source",
        expected_cursor="wrong",
        next_cursor="c2",
        page_id="p2",
        page_count=1,
        page_bytes=3,
        owner_id="one",
        fencing_token=lease.fencing_token,
    )


@pytest.mark.asyncio
async def test_same_connection_isolated_by_agent() -> None:
    store = InMemorySourceSyncStore()
    first = await store.acquire_lease("did:a", "source", "one", ttl_seconds=60)
    second = await store.acquire_lease("did:b", "source", "one", ttl_seconds=60)
    assert first is not None and second is not None
    assert (await store.get_state("did:a", "source")).agent_did == "did:a"
    assert (await store.get_state("did:b", "source")).agent_did == "did:b"


@pytest.mark.asyncio
async def test_expired_lease_rejects_commit() -> None:
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    clock = [now]
    store = InMemorySourceSyncStore(clock=lambda: clock[0])
    lease = await store.acquire_lease("did:a", "source", "one", ttl_seconds=1)
    assert lease is not None
    clock[0] = now + datetime.timedelta(seconds=2)
    assert not await store.commit_page(
        "did:a",
        "source",
        expected_cursor=None,
        next_cursor="c1",
        page_id="p1",
        page_count=1,
        page_bytes=1,
        owner_id="one",
        fencing_token=lease.fencing_token,
    )
    assert not await store.set_status(
        "did:a", "source", "failed", owner_id="one", fencing_token=lease.fencing_token
    )


@pytest.mark.asyncio
async def test_purge_advances_durable_source_generation() -> None:
    store = InMemorySourceSyncStore()
    assert (await store.get_state("did:a", "source")).generation == 1
    assert await store.purge("did:a", "source")
    assert (await store.get_state("did:a", "source")).generation == 2


@pytest.mark.asyncio
async def test_completion_stamps_durable_last_synced_at() -> None:
    """The card's 'Last sync' reads a durable time stamped only on COMPLETE."""
    store = InMemorySourceSyncStore()
    lease = await store.acquire_lease("did:a", "source", "one", ttl_seconds=60)
    assert lease is not None
    assert (await store.get_state("did:a", "source")).last_synced_at is None
    await store.commit_page(
        "did:a",
        "source",
        expected_cursor=None,
        next_cursor="c1",
        page_id="p1",
        page_count=1,
        page_bytes=42,
        owner_id="one",
        fencing_token=lease.fencing_token,
    )
    # A non-terminal status change must NOT stamp a successful-sync time.
    assert await store.set_status(
        "did:a", "source", "running", owner_id="one", fencing_token=lease.fencing_token
    )
    assert (await store.get_state("did:a", "source")).last_synced_at is None
    # Completion records the durable last-successful-sync time next to the counters.
    assert await store.set_status(
        "did:a", "source", "complete", owner_id="one", fencing_token=lease.fencing_token
    )
    state = await store.get_state("did:a", "source")
    assert state.last_synced_at is not None
    assert state.pages == 1
    assert state.bytes_processed == 42


@pytest.mark.asyncio
async def test_arcstore_backend_stamps_last_synced_at_on_complete() -> None:
    """The production adapter over an ArcStore backend stamps it the same way."""
    from arcstore.backends.memory import FakeBackend
    from arcstore.source_sync import ArcStoreSourceSyncStore

    store = ArcStoreSourceSyncStore(FakeBackend())
    lease = await store.acquire_lease("did:a", "src", "one", ttl_seconds=60)
    assert lease is not None
    await store.commit_page(
        "did:a",
        "src",
        expected_cursor=None,
        next_cursor="c1",
        page_id="p1",
        page_count=2,
        page_bytes=99,
        owner_id="one",
        fencing_token=lease.fencing_token,
    )
    assert await store.set_status(
        "did:a", "src", "complete", owner_id="one", fencing_token=lease.fencing_token
    )
    state = await store.get_state("did:a", "src")
    assert state.last_synced_at is not None
    assert state.pages == 2
    assert state.bytes_processed == 99
