from __future__ import annotations

import asyncio

import pytest

from arcagent.connected_data import SyncLimits
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDescription,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.service import ConnectedDataService


class FakeSource:
    def __init__(self) -> None:
        self.closed = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="test",
            account_id="account",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.started.set()
        await self.release.wait()
        return SyncSourcePage(next_checkpoint="", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"",
        )

    async def close_source(self) -> None:
        self.closed = True


class BrokenCloseSource(FakeSource):
    async def close_source(self) -> None:
        raise RuntimeError("close failed")


@pytest.mark.asyncio
async def test_catalog_revoke_waits_for_active_sync_lease() -> None:
    catalog = SourceCatalog()
    source = FakeSource()
    await catalog.register("dropbox", source)

    async with catalog.lease("dropbox") as registration:
        assert registration is not None
        revoke = asyncio.create_task(catalog.unregister("dropbox"))
        await asyncio.sleep(0)
        assert not revoke.done()
        assert not source.closed
    await revoke
    assert source.closed
    assert await catalog.snapshot() == ()


@pytest.mark.asyncio
async def test_catalog_close_contains_adapter_failure() -> None:
    catalog = SourceCatalog()
    first = BrokenCloseSource()
    second = FakeSource()
    await catalog.register("one", first)
    await catalog.register("two", second)
    await catalog.close()
    assert await catalog.snapshot() == ()
    assert second.closed


@pytest.mark.asyncio
async def test_service_degrades_without_arcstore_or_ingest() -> None:
    catalog = SourceCatalog()
    source = FakeSource()
    await catalog.register("dropbox", source)
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=None,
        ingest_factory=None,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=0.01,
    )
    await service.start()
    await asyncio.sleep(0.03)
    statuses = await service.list_status()
    await service.close()
    assert statuses[0].status == "degraded"
    assert statuses[0].detail == "arcstore_unavailable"
    assert not source.closed
