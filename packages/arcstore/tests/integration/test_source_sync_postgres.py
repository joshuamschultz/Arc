from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig
from arcstore.source_sync import ArcStoreSourceSyncStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARCSTORE_TEST_POSTGRES_DSN"),
    reason="set ARCSTORE_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_postgres_source_sync_fences_and_commits_expected_cursor() -> None:
    backend = PostgresBackend(
        ArcStoreConfig().postgres_settings(SecretStr(os.environ["ARCSTORE_TEST_POSTGRES_DSN"]))
    )
    await backend.start()
    try:
        store = ArcStoreSourceSyncStore(backend)
        lease = await store.acquire_lease(
            "did:integration", "integration-source", "worker", ttl_seconds=30
        )
        assert lease is not None
        assert await store.commit_page(
            "did:integration",
            "integration-source",
            expected_cursor=None,
            next_cursor="c1",
            page_id="p1",
            page_count=1,
            page_bytes=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
        )
        assert (await store.get_state("did:integration", "integration-source")).cursor == "c1"
    finally:
        await backend.stop()
