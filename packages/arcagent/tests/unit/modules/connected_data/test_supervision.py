"""A sync that crashes or hangs is caught, audited and retried with backoff.

One source must never take down its siblings or the service: each runs in its
own supervised task, a crash or a stall is charged to that source alone, and it
comes back after a capped exponential backoff.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import MappingPlan, SyncLimits
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connected_data.supervision import SyncSchedule


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_backoff_doubles_to_a_cap_and_a_completed_run_resets_it() -> None:
    clock = Clock()
    schedule = SyncSchedule(
        interval_seconds=3600,
        backoff_seconds=30,
        backoff_max_seconds=100,
        clock=clock,
        jitter=lambda: 1.0,
    )

    delays = [schedule.failed("s") for _ in range(4)]
    assert delays == [30, 60, 100, 100]
    assert not schedule.is_due("s")
    clock.now += 100
    assert schedule.is_due("s")

    schedule.completed("s", more_work=False)
    assert schedule.failures("s") == 0
    assert schedule.seconds_until_next() == 3600
    assert schedule.failed("s") == 30


def test_a_run_stopped_at_its_budget_continues_at_once() -> None:
    clock = Clock()
    schedule = SyncSchedule(
        interval_seconds=3600, backoff_seconds=1, backoff_max_seconds=1, clock=clock
    )

    schedule.completed("s", more_work=True)

    assert schedule.is_due("s")


def test_an_operator_request_during_a_run_is_kept() -> None:
    clock = Clock()
    schedule = SyncSchedule(
        interval_seconds=3600, backoff_seconds=1, backoff_max_seconds=1, clock=clock
    )
    schedule.started("s")
    schedule.run_now("s")  # "sync now" while the run is still going

    schedule.completed("s", more_work=False)

    assert schedule.is_due("s")
    schedule.started("s")
    schedule.completed("s", more_work=False)
    assert not schedule.is_due("s")


class Source:
    """A source that behaves per connection: healthy, crashing, or hanging."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.attempts = 0

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="test",
            account_id=request.connection_id,
            data_shape=SourceDataShape.DOCUMENT,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.attempts += 1
        if self.behaviour == "crash":
            raise RuntimeError("adapter bug")
        if self.behaviour == "hang":
            await asyncio.Event().wait()
        return SyncSourcePage(
            objects=(
                SourceObject(
                    object_id=f"doc-{self.attempts}",
                    locator="doc",
                    kind=SourceObjectKind.FILE,
                    version="1",
                ),
            ),
            next_checkpoint=f"c{self.attempts}",
            has_more=False,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"body",
        )

    async def close_source(self) -> None:
        return None


class Ingest:
    def __init__(self) -> None:
        self.ingested: list[str] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(mapping_id="m", homes=("document",), revision="r", content_hash="h")

    async def ingest(self, source: Any, source_object: Any, content: Any, mapping: Any) -> None:
        self.ingested.append(f"{source.connection_id}:{source_object.object_id}")

    async def complete_snapshot(self, source: Any, object_ids: Any, mapping: Any) -> None:
        return None


async def _service(
    sources: dict[str, Source], events: list[tuple[str, dict[str, Any]]], **options: Any
) -> tuple[ConnectedDataService, Ingest]:
    catalog = SourceCatalog()
    for connection_id, source in sources.items():
        await catalog.register(connection_id, source)
    store = InMemorySourceSyncStore()
    ingest = Ingest()

    async def open_store() -> InMemorySourceSyncStore:
        return store

    async def audit(action: str, payload: dict[str, Any]) -> None:
        events.append((action, payload))

    settings: dict[str, Any] = {
        "limits": SyncLimits(retries=0),
        "interval_seconds": 3600,
        "restart_backoff_seconds": 0.02,
        "restart_backoff_max_seconds": 0.08,
        **options,
    }
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=open_store,
        ingest_factory=lambda _: ingest,
        global_concurrency=4,
        audit=audit,
        **settings,
    )
    await service.start()
    return service, ingest


@pytest.mark.asyncio
async def test_a_crashing_source_backs_off_while_its_sibling_keeps_syncing() -> None:
    crashing, healthy = Source("crash"), Source("ok")
    events: list[tuple[str, dict[str, Any]]] = []
    service, ingest = await _service({"bad": crashing, "good": healthy}, events)
    try:
        await asyncio.sleep(0.4)
        assert service._monitor is not None and not service._monitor.done()
    finally:
        await service.close()

    # Retried, but not spun: 0.02, 0.04, 0.08, 0.08 ... within 0.4 s.
    assert 3 <= crashing.attempts <= 9, crashing.attempts
    crashes = [payload for action, payload in events if action == "connected_data.sync.crashed"]
    assert len(crashes) == crashing.attempts
    assert [payload["failures"] for payload in crashes][:3] == [1, 2, 3]
    assert crashes[0]["error"] == "RuntimeError"
    # The healthy source synced once and now waits its interval — untouched by the crash.
    assert healthy.attempts == 1
    assert ingest.ingested == ["good:doc-1"]


@pytest.mark.asyncio
async def test_a_hung_source_is_cut_off_audited_and_retried() -> None:
    hanging, healthy = Source("hang"), Source("ok")
    events: list[tuple[str, dict[str, Any]]] = []
    service, _ = await _service(
        {"stuck": hanging, "fine": healthy},
        events,
        limits=SyncLimits(retries=0, max_seconds=0.05),
        stall_grace_seconds=0.05,
    )
    try:
        await asyncio.sleep(0.5)
    finally:
        await service.close()

    assert hanging.attempts >= 2, "a stalled run must be abandoned and tried again"
    stalls = [payload for action, payload in events if action == "connected_data.sync.stalled"]
    assert stalls and stalls[0]["error"] == "sync_stalled" and stalls[0]["failures"] == 1
    # The stalled run was cancelled cleanly: its state says so, never "running".
    cancelled = [
        payload for action, payload in events if action == "connected_data.sync.cancelled"
    ]
    assert cancelled
    assert healthy.attempts == 1


@pytest.mark.asyncio
async def test_a_run_stopped_at_its_budget_is_continued_without_waiting_an_hour() -> None:
    class Paged(Source):
        async def sync_source(self, request: SyncSource) -> SyncSourcePage:
            self.attempts += 1
            number = int((request.checkpoint or "c0")[1:])
            return SyncSourcePage(
                objects=(
                    SourceObject(
                        object_id=f"doc-{number}",
                        locator="doc",
                        kind=SourceObjectKind.FILE,
                        version="1",
                    ),
                ),
                next_checkpoint=f"c{number + 1}",
                has_more=number < 3,
            )

    source = Paged("ok")
    service, ingest = await _service(
        {"big": source}, [], limits=SyncLimits(retries=0, max_pages=1)
    )
    try:
        for _ in range(200):
            if len(ingest.ingested) >= 4:
                break
            await asyncio.sleep(0.01)
    finally:
        await service.close()

    assert ingest.ingested == ["big:doc-0", "big:doc-1", "big:doc-2", "big:doc-3"]
