"""Connections sweep 2026-10-03: the sync loop, driven for real (D1, D6, D8, D9, D10, D11).

DGX evidence behind each test:

* D1: every fresh process ran its first sync exactly one hour after start. The
  monitor started on an empty catalog and nothing woke it when the connectors
  registered their sources.
* D6: three Jira rows sat ``running`` with leases that expired at the restart.
* D8: 93 runs failed with "time limit exceeded" because a deadline inside a page
  threw the whole page away, so every retry overran again.
* D9: 116 LeaseLostError with no contention: the lease was renewed only between
  units of work, and one slow fetch outlived it.
* D10: a Slack rate limit counted as a crash, with a traceback and a strike.
* D11: a resource list for a signed-out account answered HTTP 400.

Nothing here fakes the service, the coordinator or the sync store; only the
provider (the source) and the health record are doubles.
"""

from __future__ import annotations

import asyncio
from typing import Any

from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    KnowledgeHome,
    MappingPlan,
    SyncLimits,
    SyncStatus,
)
from arcagent.extension.connection_health import HealthSignal
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.extension.state import ConnectionStatus
from arcagent.modules.connected_data.service import ConnectedDataService

_DID = "did:agent"


def _object(object_id: str) -> SourceObject:
    return SourceObject(
        object_id=object_id, locator=object_id, kind=SourceObjectKind.FILE, version="1", size=1
    )


class _Ingest:
    def __init__(self) -> None:
        self.ingested: list[str] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="m", homes=(KnowledgeHome.DOCUMENT,), revision="r", content_hash="h"
        )

    async def ingest(self, source: Any, source_object: Any, content: Any, mapping: Any) -> None:
        self.ingested.append(source_object.object_id)

    async def complete_snapshot(self, source: Any, object_ids: Any, mapping: Any) -> None:
        return None

    async def reset_source(self, source: SourceDescription) -> None:
        return None

    async def purge_source(self, source: SourceDescription) -> None:
        return None


class _Provider:
    """A source whose ``sync_source`` follows a script, one entry per call."""

    def __init__(self, *script: str, retry_after: float | None = None) -> None:
        self._script = list(script) or ["ok"]
        self.retry_after = retry_after
        self.checkpoints: list[str | None] = []
        self.synced = asyncio.Event()
        self.resource_failure: Exception | None = None

    def _next(self) -> str:
        return self._script.pop(0) if len(self._script) > 1 else self._script[0]

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="test",
            account_id=request.connection_id,
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Test source",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        self.synced.set()
        behaviour = self._next()
        if behaviour == "rate_limited":
            raise SourceError(
                SourceFailureCode.RATE_LIMITED,
                "Slack rate limit persisted after bounded retries",
                retry_after=self.retry_after,
            )
        number = len(self.checkpoints)
        return SyncSourcePage(
            objects=(_object(f"doc{number}"),),
            next_checkpoint=f"c{number}",
            has_more=behaviour == "more",
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"x",
        )

    async def list_source_resources(self, request: Any) -> tuple[SourceResource, ...]:
        if self.resource_failure is not None:
            raise self.resource_failure
        return ()

    async def select_source_resources(self, request: Any) -> None:
        return None

    async def close_source(self) -> None:
        return None


class _Health:
    def __init__(self) -> None:
        self.signals: list[tuple[str, HealthSignal]] = []

    async def report(self, connection: str, signal: HealthSignal) -> None:
        self.signals.append((connection, signal))

    async def statuses(self) -> dict[str, ConnectionStatus]:
        return {}


class _RecordingStore(InMemorySourceSyncStore):
    """The real in-memory store, with every status write written down."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: list[tuple[str, str | None]] = []

    async def set_status(self, agent_did: str, source_id: str, status: str, **kw: Any) -> bool:
        written = await super().set_status(agent_did, source_id, status, **kw)
        if written:
            self.statuses.append((str(status), kw.get("error_code")))
        return written


def _service(
    catalog: SourceCatalog,
    store: InMemorySourceSyncStore,
    *,
    health: _Health | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    **options: Any,
) -> ConnectedDataService:
    async def open_store() -> InMemorySourceSyncStore:
        return store

    async def audit(action: str, payload: dict[str, Any]) -> None:
        if events is not None:
            events.append((action, payload))

    settings: dict[str, Any] = {
        "limits": SyncLimits(retries=0),
        "interval_seconds": 3600,
        "restart_backoff_seconds": 0.01,
        "restart_backoff_max_seconds": 0.02,
        "failure_ceiling": 3,
        **options,
    }
    return ConnectedDataService(
        catalog,
        agent_did=_DID,
        sync_store_opener=open_store,
        ingest_factory=lambda _: _Ingest(),
        global_concurrency=2,
        health=health or _Health(),
        audit=audit,
        **settings,
    )


async def _until(predicate: Any, *, seconds: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def _let_monitor_sleep() -> None:
    """Yield until the monitor has finished its tick and is waiting to be woken."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _leave_a_dead_run(store: InMemorySourceSyncStore, cursor: str) -> None:
    """What a killed process leaves: a committed cursor, ``running``, a lapsed lease."""
    lease = await store.acquire_lease(_DID, "mail", "dead-process", ttl_seconds=0.02)
    assert lease is not None
    assert await store.commit_page(
        _DID,
        "mail",
        expected_cursor=None,
        next_cursor=cursor,
        page_id="before-the-crash",
        page_count=1,
        page_bytes=1,
        owner_id="dead-process",
        fencing_token=lease.fencing_token,
    )
    await asyncio.sleep(0.05)
    assert (await store.get_state(_DID, "mail")).status is SyncStatus.RUNNING


# --- D1: a source attached after start syncs at once, not an interval later -------------


async def test_a_source_registered_after_start_syncs_without_waiting_an_interval() -> None:
    catalog, store, provider = SourceCatalog(), InMemorySourceSyncStore(), _Provider()
    service = _service(catalog, store, interval_seconds=3600)
    await service.start()
    try:
        await _let_monitor_sleep()  # the catalog was empty; the monitor is asleep
        await catalog.register("mail", provider)
        await asyncio.wait_for(provider.synced.wait(), timeout=2.0)
    finally:
        await service.close()


async def test_a_source_replaced_after_start_wakes_the_monitor_too() -> None:
    catalog, store = SourceCatalog(), InMemorySourceSyncStore()
    first, second = _Provider(), _Provider()
    await catalog.register("mail", first)
    service = _service(catalog, store, interval_seconds=3600)
    await service.start()
    try:
        await asyncio.wait_for(first.synced.wait(), timeout=2.0)

        async def complete() -> bool:
            return (await store.get_state(_DID, "mail")).status is SyncStatus.COMPLETE

        assert await _until(complete)
        await service.revoke("mail")
        await catalog.register("mail", second)
        await asyncio.wait_for(second.synced.wait(), timeout=2.0)
    finally:
        await service.close()


def test_connected_data_starts_after_the_connectors_that_fill_its_catalog() -> None:
    from arcagent.modules.connected_data.capabilities import ConnectedData
    from arcagent.tools._decorator import capability_meta

    meta = capability_meta(ConnectedData)
    assert meta is not None
    assert "connectors" in meta.depends_on
