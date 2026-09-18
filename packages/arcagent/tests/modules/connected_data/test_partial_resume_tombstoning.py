"""SPEC-082 COMP-010 (gap coverage) — resume reads the DURABLE partial flag.

The arcstore suite proves ``budget_reached`` survives serialization and a
restart. This proves the coordinator actually USES it: a run that resumes a prior
partial crawl must read the durable ``budget_reached`` (not just its own in-memory
copy, which a fresh coordinator instance does not have) and suppress full-crawl
tombstoning — otherwise the resumed run, which saw only the tail of the account,
would reconcile a partial listing and delete every object the first run reached.

A positive control proves the guard is not simply disabled: a genuine fresh
full crawl still reconciles.
"""

from __future__ import annotations

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import MappingPlan, SyncLimits
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
        self.ingested: list[str] = []
        self.snapshots: list[frozenset[str]] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="map:source", homes=("document",), revision="r1", content_hash="hash"
        )

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        self.ingested.append(source_object.object_id)

    async def complete_snapshot(
        self,
        source: SourceDescription,
        object_ids: frozenset[str],
        mapping: MappingPlan,
    ) -> None:
        self.snapshots.append(object_ids)


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


def _described() -> SourceDescription:
    return SourceDescription(
        connection_id="source",
        source_kind="test",
        account_id="account",
        supports_incremental=False,
    )


@pytest.mark.asyncio
async def test_resume_reads_durable_partial_flag_and_suppresses_tombstoning() -> None:
    """A second coordinator instance resumes and must not tombstone the tail-only run."""
    source = FakeSource([page("a", cursor="c1"), page("b", cursor="")])
    ingest = FakeIngest()
    store = InMemorySourceSyncStore()

    # Run 1 stops at a one-page budget: partial, so it reconciles nothing and the
    # partial-run fact is persisted durably.
    await ConnectedDataCoordinator(source, ingest, store).run(
        _described(), agent_did="did:a", owner_id="w", limits=SyncLimits(max_pages=1)
    )
    assert ingest.ingested == ["a"]
    assert ingest.snapshots == []
    durable = await store.get_state("did:a", "source")
    assert durable.budget_reached is True, "the partial-run fact was not persisted durably"

    # Run 2 is a fresh coordinator with no in-memory budget flag; it must read the
    # DURABLE one, know the crawl is partial, and still not tombstone the tail.
    await ConnectedDataCoordinator(source, ingest, store).run(
        _described(), agent_did="did:a", owner_id="w", limits=SyncLimits(max_pages=10)
    )
    assert ingest.ingested == ["a", "b"]
    assert ingest.snapshots == [], (
        "a resumed run that saw only the tail reconciled a partial listing — it "
        "would tombstone every object the first run reached"
    )


@pytest.mark.asyncio
async def test_a_fresh_full_crawl_still_reconciles() -> None:
    """Positive control: the tombstoning guard is suppressed, not disabled."""
    source = FakeSource([page("a", "b", cursor="")])
    ingest = FakeIngest()
    store = InMemorySourceSyncStore()

    await ConnectedDataCoordinator(source, ingest, store).run(
        _described(), agent_did="did:a", owner_id="w", limits=SyncLimits()
    )

    assert ingest.snapshots == [frozenset({"a", "b"})]
    assert (await store.get_state("did:a", "source")).budget_reached is False
