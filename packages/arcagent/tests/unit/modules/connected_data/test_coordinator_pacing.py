"""The coordinator paces every source and never starves the event loop.

Each page fetch, object fetch and object ingest is one unit of the source's
duty cycle. The rest a source owes does not count against its time budget, and
its lease is kept alive through a long, well-paced page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import MappingPlan, SyncLimits, SyncStatus
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

_SOURCE = SourceDescription(connection_id="source", source_kind="test", account_id="account")


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class Source:
    def __init__(self, objects_per_page: int, *, pages: int | None = 1) -> None:
        self.objects_per_page = objects_per_page
        self.pages = pages
        self.served = 0

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.served += 1
        more = self.pages is None or self.served < self.pages
        return SyncSourcePage(
            objects=tuple(
                SourceObject(
                    object_id=f"p{self.served}-o{number}",
                    locator="doc",
                    kind=SourceObjectKind.FILE,
                    version="1",
                    size=1,
                )
                for number in range(self.objects_per_page)
            ),
            next_checkpoint=f"c{self.served}",
            has_more=more,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"x",
        )


class Ingest:
    """Each ingest costs ``work`` of fake time, or blocks the real loop."""

    def __init__(self, fake: FakeTime | None = None, *, blocking: float = 0.0) -> None:
        self.fake = fake
        self.blocking = blocking
        self.ingested: list[str] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(mapping_id="m", homes=("document",), revision="r", content_hash="h")

    async def ingest(self, source: Any, source_object: Any, content: Any, mapping: Any) -> None:
        if self.fake is not None:
            self.fake.now += 1.0
        if self.blocking:
            time.sleep(self.blocking)  # synchronous work on the loop thread
        self.ingested.append(source_object.object_id)

    async def complete_snapshot(self, source: Any, object_ids: Any, mapping: Any) -> None:
        return None


class CountingStore(InMemorySourceSyncStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0

    async def renew_lease(self, *args: Any, **kwargs: Any) -> bool:
        self.renewals += 1
        return await super().renew_lease(*args, **kwargs)


@pytest.mark.asyncio
async def test_a_source_works_at_most_its_share_of_wall_time() -> None:
    fake = FakeTime()
    ingest = Ingest(fake)
    started = fake.now

    result = await ConnectedDataCoordinator(
        Source(8), ingest, InMemorySourceSyncStore(), clock=fake.clock, sleep=fake.sleep
    ).run(
        _SOURCE,
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_duty_fraction=0.25, max_concurrency=4),
    )

    assert result.status is SyncStatus.COMPLETE
    assert len(ingest.ingested) == 8
    work = 8.0
    # Seven rests of three seconds between the eight one-second ingests.
    assert sum(fake.sleeps) == pytest.approx(21.0)
    assert work / (fake.now - started) <= 0.25 + work / 100


@pytest.mark.asyncio
async def test_rest_does_not_count_against_the_time_budget_and_the_lease_is_kept() -> None:
    fake = FakeTime()
    store = CountingStore()

    result = await ConnectedDataCoordinator(
        Source(4), Ingest(fake), store, clock=fake.clock, sleep=fake.sleep
    ).run(
        _SOURCE,
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_duty_fraction=0.25, max_seconds=6.0),
    )

    # 4 s of work and 9 s of rest: well past max_seconds of wall time, still fine.
    assert result.status is SyncStatus.COMPLETE
    assert store.renewals > 1, "a long paced page must keep renewing its lease"


@pytest.mark.asyncio
async def test_the_time_budget_ends_a_run_at_a_page_boundary_not_as_a_failure() -> None:
    fake = FakeTime()
    source = Source(1, pages=None)  # an account that never ends
    coordinator = ConnectedDataCoordinator(
        source, Ingest(fake), InMemorySourceSyncStore(), clock=fake.clock, sleep=fake.sleep
    )

    result = await coordinator.run(
        _SOURCE,
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_duty_fraction=1.0, max_seconds=2.5),
    )

    assert result.status is SyncStatus.COMPLETE
    assert result.budget_reached and coordinator.stopped_at_ceiling
    assert result.cursor == "c3"


@pytest.mark.asyncio
async def test_the_event_loop_keeps_turning_while_a_page_ingests() -> None:
    """On-loop work is paced one unit at a time; a whole page never runs in one go."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.002)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    await ConnectedDataCoordinator(
        Source(20), Ingest(blocking=0.03), InMemorySourceSyncStore()
    ).run(
        _SOURCE,
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_duty_fraction=0.5, max_concurrency=8),
    )
    stop.set()
    await task

    assert max(gaps) < 0.075, max(gaps)
