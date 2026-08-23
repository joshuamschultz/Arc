from __future__ import annotations

import asyncio

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    LeaseLostError,
    MappingPendingError,
    MappingPlan,
    SyncError,
    SyncLimits,
    SyncStatus,
    TransientSyncError,
)
from arcagent.extension.source import (
    FetchSourceObject,
    SourceContent,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.modules.connected_data import ConnectedDataCoordinator


class FakeSource:
    def __init__(self, pages: list[SyncSourcePage]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.calls.append(request.checkpoint)
        return self.pages[len(self.calls) - 1]

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=request.object_id.encode(),
        )


class FakeIngest:
    def __init__(self) -> None:
        self.staged: list[str] = []
        self.ingested: list[str] = []
        self.fail = False

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        self.staged.append("mapping")
        return MappingPlan(mapping_id="map:source", revision="r1", content_hash="hash")

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        if self.fail:
            raise RuntimeError("temporary ingest outage")
        self.ingested.append(source_object.object_id)


def page(*ids: str, cursor: str) -> SyncSourcePage:
    return SyncSourcePage(
        objects=tuple(
            SourceObject(
                object_id=identifier,
                locator=identifier,
                kind=SourceObjectKind.FILE,
                version="1",
                size=1,
            )
            for identifier in ids
        ),
        next_checkpoint=cursor,
        has_more=bool(cursor),
    )


@pytest.mark.asyncio
async def test_mapping_is_a_gate_and_cursor_follows_ingest() -> None:
    source = FakeSource([page("a", "b", cursor="c1"), page("c", cursor="")])
    ingest = FakeIngest()
    result = await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
    )
    assert result.status.value == "complete"
    assert ingest.staged == ["mapping"]
    assert ingest.ingested == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_failed_ingest_does_not_advance_cursor() -> None:
    source = FakeSource([page("a", cursor="c1")])
    ingest = FakeIngest()
    ingest.fail = True
    store = InMemorySourceSyncStore()
    with pytest.raises(RuntimeError):
        await ConnectedDataCoordinator(source, ingest, store).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(retries=0),
        )
    assert (await store.get_state("did:a", "source")).cursor is None


@pytest.mark.asyncio
async def test_cancellation_is_cooperative() -> None:
    source = FakeSource([page("a", cursor="c1")])
    cancelled = asyncio.Event()
    cancelled.set()
    result = await ConnectedDataCoordinator(source, FakeIngest(), InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
        cancel_event=cancelled,
    )
    assert result.status.value == "cancelled"
    assert source.calls == []


@pytest.mark.asyncio
async def test_pending_mapping_does_not_read_or_write() -> None:
    source = FakeSource([page("a", cursor="c1")])

    class Pending(FakeIngest):
        async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
            raise MappingPendingError()

    ingest = Pending()
    result = await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
    )
    assert result.status is SyncStatus.AWAITING_MAPPING
    assert source.calls == []
    assert ingest.ingested == []


@pytest.mark.asyncio
async def test_same_connection_isolated_by_agent_identity() -> None:
    store = InMemorySourceSyncStore()
    first = await store.acquire_lease("did:a", "source", "worker", ttl_seconds=60)
    second = await store.acquire_lease("did:b", "source", "worker", ttl_seconds=60)
    assert first is not None and second is not None
    assert (await store.get_state("did:a", "source")).agent_did == "did:a"
    assert (await store.get_state("did:b", "source")).agent_did == "did:b"


@pytest.mark.asyncio
async def test_actual_fetched_bytes_are_bounded_across_objects() -> None:
    source = FakeSource([page("a", "b", cursor="")])
    with pytest.raises(SyncError, match="byte limit"):
        await ConnectedDataCoordinator(source, FakeIngest(), InMemorySourceSyncStore()).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(max_bytes=1),
        )


@pytest.mark.asyncio
async def test_retry_sleep_is_capped_by_remaining_deadline() -> None:
    class Flaky(FakeSource):
        async def sync_source(self, request: SyncSource) -> SyncSourcePage:
            raise TransientSyncError("busy", retry_after=100)

    sleeps: list[float] = []
    source = Flaky([])

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(TransientSyncError):
        await ConnectedDataCoordinator(
            source,
            FakeIngest(),
            InMemorySourceSyncStore(),
            sleep=record_sleep,
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(max_seconds=1, retries=1),
        )
    assert sleeps and sleeps[0] <= 1


@pytest.mark.asyncio
async def test_first_ingest_failure_cancels_siblings() -> None:
    started: list[str] = []
    cancelled: list[str] = []

    class Ingest(FakeIngest):
        async def ingest(
            self,
            source: SourceDescription,
            source_object: SourceObject,
            content: SourceContent | None,
            mapping: MappingPlan,
        ) -> None:
            started.append(source_object.object_id)
            if source_object.object_id == "a":
                raise RuntimeError("failed")
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(source_object.object_id)
                raise

    with pytest.raises(RuntimeError):
        await ConnectedDataCoordinator(
            FakeSource([page("a", "b", cursor="")]), Ingest(), InMemorySourceSyncStore()
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )
    assert "b" in cancelled


@pytest.mark.asyncio
async def test_parent_cancellation_awaits_all_ingest_tasks() -> None:
    finished: list[str] = []

    class Ingest(FakeIngest):
        async def ingest(
            self,
            source: SourceDescription,
            source_object: SourceObject,
            content: SourceContent | None,
            mapping: MappingPlan,
        ) -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                finished.append(source_object.object_id)
                raise

    task = asyncio.create_task(
        ConnectedDataCoordinator(
            FakeSource([page("a", "b", cursor="")]), Ingest(), InMemorySourceSyncStore()
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(finished) == ["a", "b"]


@pytest.mark.asyncio
async def test_stale_lease_audits_without_attempting_fenced_status_write() -> None:
    events: list[str] = []

    class RejectStatus(InMemorySourceSyncStore):
        async def set_status(
            self, agent_did: str, source_id: str, status: str, **kwargs: object
        ) -> bool:
            if status != SyncStatus.RUNNING:
                raise AssertionError("stale worker attempted durable status mutation")
            return await super().set_status(agent_did, source_id, status, **kwargs)

        async def commit_page(self, agent_did: str, source_id: str, **kwargs: object) -> bool:
            return False

    with pytest.raises(LeaseLostError):
        await ConnectedDataCoordinator(
            FakeSource([page("a", cursor="")]),
            FakeIngest(),
            RejectStatus(),
            audit=lambda event, payload: events.append(event),
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )
    assert "connected_data.sync.lease_lost" in events


@pytest.mark.asyncio
async def test_cooperative_cancellation_preserves_cancelled_status_audit_when_fence_is_stale() -> (
    None
):
    events: list[tuple[str, dict[str, object]]] = []
    cancel_event = asyncio.Event()

    class ExpiredCancellationState(InMemorySourceSyncStore):
        async def set_status(
            self, agent_did: str, source_id: str, status: str, **kwargs: object
        ) -> bool:
            if status == SyncStatus.CANCELLED:
                return False
            return await super().set_status(agent_did, source_id, status, **kwargs)

    class CancellingSource(FakeSource):
        async def sync_source(self, request: SyncSource) -> SyncSourcePage:
            cancel_event.set()
            return await super().sync_source(request)

    result = await ConnectedDataCoordinator(
        CancellingSource([page("a", cursor="")]),
        FakeIngest(),
        ExpiredCancellationState(),
        audit=lambda event, payload: events.append((event, dict(payload))),
    ).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
        cancel_event=cancel_event,
    )
    assert result.status is SyncStatus.RUNNING
    assert any(
        event == "connected_data.sync.cancelled" and payload["persisted"] is False
        for event, payload in events
    )


@pytest.mark.asyncio
async def test_parent_cancellation_preserves_cancelled_error_when_fence_is_stale() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class ExpiredCancellationState(InMemorySourceSyncStore):
        async def set_status(
            self, agent_did: str, source_id: str, status: str, **kwargs: object
        ) -> bool:
            if status == SyncStatus.CANCELLED:
                return False
            return await super().set_status(agent_did, source_id, status, **kwargs)

    class BlockingIngest(FakeIngest):
        async def ingest(
            self,
            source: SourceDescription,
            source_object: SourceObject,
            content: SourceContent | None,
            mapping: MappingPlan,
        ) -> None:
            await asyncio.sleep(10)

    task = asyncio.create_task(
        ConnectedDataCoordinator(
            FakeSource([page("a", cursor="")]),
            BlockingIngest(),
            ExpiredCancellationState(),
            audit=lambda event, payload: events.append((event, dict(payload))),
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(
        event == "connected_data.sync.cancelled" and payload["persisted"] is False
        for event, payload in events
    )


@pytest.mark.asyncio
async def test_complete_status_fence_failure_is_audited_as_lease_loss() -> None:
    events: list[str] = []

    class CompleteFenceLost(InMemorySourceSyncStore):
        async def set_status(
            self, agent_did: str, source_id: str, status: str, **kwargs: object
        ) -> bool:
            if status == SyncStatus.COMPLETE:
                return False
            return await super().set_status(agent_did, source_id, status, **kwargs)

    store = CompleteFenceLost()
    with pytest.raises(LeaseLostError):
        await ConnectedDataCoordinator(
            FakeSource([page(cursor="")]),
            FakeIngest(),
            store,
            audit=lambda event, payload: events.append(event),
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )
    assert "connected_data.sync.lease_lost" in events
    assert (await store.get_state("did:a", "source")).status.value == SyncStatus.RUNNING.value


@pytest.mark.asyncio
async def test_failed_status_fence_failure_is_audited_as_lease_loss() -> None:
    events: list[str] = []

    class FailedFenceLost(InMemorySourceSyncStore):
        async def set_status(
            self, agent_did: str, source_id: str, status: str, **kwargs: object
        ) -> bool:
            if status == SyncStatus.FAILED:
                return False
            return await super().set_status(agent_did, source_id, status, **kwargs)

    store = FailedFenceLost()
    failing = FakeIngest()
    failing.fail = True
    with pytest.raises(LeaseLostError):
        await ConnectedDataCoordinator(
            FakeSource([page("a", cursor="")]),
            failing,
            store,
            audit=lambda event, payload: events.append(event),
        ).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(retries=0),
        )
    assert "connected_data.sync.lease_lost" in events
    assert (await store.get_state("did:a", "source")).status.value == SyncStatus.RUNNING.value
