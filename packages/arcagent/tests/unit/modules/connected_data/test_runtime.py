from __future__ import annotations

import asyncio

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPendingError, MappingPlan, SyncLimits
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
    statuses = await service.list_sources()
    await service.close()
    assert statuses[0].status == "degraded"
    assert statuses[0].detail == "arcstore_unavailable"
    assert not source.closed


class _SnapshotSource:
    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="email",
            account_id="mailbox",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint, has_more=False)
        from arcagent.extension.source import SourceObject, SourceObjectKind

        return SyncSourcePage(
            objects=(
                SourceObject(
                    object_id="message-1",
                    locator="inbox/message-1",
                    kind=SourceObjectKind.FILE,
                    version="1",
                    media_type="text/plain",
                ),
            ),
            next_checkpoint="cursor-1",
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"Alpha launch notes",
        )

    async def close_source(self) -> None:
        return None


class _ApprovalGatedIngest:
    def __init__(self) -> None:
        self.approved = False
        self.ingested: list[str] = []
        self.stage_calls: list[tuple[KnowledgeHome, ...]] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        if not self.approved:
            raise MappingPendingError()
        return MappingPlan(
            mapping_id="approval-1",
            homes=(KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT),
            revision="r1",
            content_hash="h1",
        )

    async def ingest(self, source, source_object, content, mapping) -> None:  # type: ignore[no-untyped-def]
        assert content is not None
        self.ingested.append(content.object_id)

    def allowed_homes(self, source: SourceDescription) -> tuple[KnowledgeHome, ...]:
        return (KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)

    async def stage_mapping(
        self, source: SourceDescription, homes: tuple[KnowledgeHome, ...]
    ) -> str:
        self.stage_calls.append(homes)
        return "approval-1"

    def canonical_source_id(self, source: SourceDescription) -> str:
        return "source-mailbox"


@pytest.mark.asyncio
async def test_mapping_gate_then_approval_backfill_and_restart_checkpoint() -> None:
    catalog = SourceCatalog()
    await catalog.register("mail", _SnapshotSource())
    state = InMemorySourceSyncStore()
    ingest = _ApprovalGatedIngest()

    async def open_state() -> InMemorySourceSyncStore:
        return state

    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=open_state,
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=60,
    )
    await service.start()
    await _wait_for_status(service, "awaiting_mapping")
    pending = (await service.list_sources())[0]
    assert pending.status == "awaiting_mapping"
    proposal = await service.stage_mapping(
        "mail", homes=(KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)
    )
    assert proposal is not None
    assert proposal.source_id == "source-mailbox"
    assert proposal.approval_id == "approval-1"

    ingest.approved = True
    assert (await service.sync_now("mail")).status == "scheduled"
    await _wait_for_status(service, "complete")
    assert ingest.ingested == ["message-1"]
    assert (await state.get_state("did:agent", "mail")).cursor == "cursor-1"
    await service.close()

    restarted = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=open_state,
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=60,
    )
    await restarted.start()
    await _wait_for_status(restarted, "complete")
    assert ingest.ingested == ["message-1"]
    await restarted.close()


async def _wait_for_status(service: ConnectedDataService, expected: str) -> None:
    for _ in range(100):
        statuses = await service.list_sources()
        if statuses and statuses[0].status == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"source did not reach {expected!r}: {await service.list_sources()!r}")
