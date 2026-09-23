"""Source-wide derived state is refreshed once per run, never per object.

A routing index describes the whole source. Rebuilding it per object made each
object cost the size of the source; the coordinator now asks the ingest port to
finish once, after its pages — and once after a failure, so pages that landed
before it still reach the index.
"""

from __future__ import annotations

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import MappingPlan, SyncError, SyncLimits
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


class Source:
    def __init__(self, pages: list[SyncSourcePage]) -> None:
        self.pages = pages
        self.checkpoints: list[str | None] = []

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        return self.pages[len(self.checkpoints) - 1]

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=request.object_id.encode(),
        )


class Ingest:
    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.fail_on = fail_on
        self.ingested: list[str] = []
        self.finishes = 0

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(mapping_id="m", homes=("document",), revision="r", content_hash="h")

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        if source_object.object_id in self.fail_on:
            raise RuntimeError("store unavailable")
        self.ingested.append(source_object.object_id)

    async def complete_snapshot(
        self, source: SourceDescription, object_ids: frozenset[str], mapping: MappingPlan
    ) -> None:
        return None

    async def finish_sync(self, source: SourceDescription) -> None:
        self.finishes += 1


def _page(*ids: str, cursor: str, more: bool) -> SyncSourcePage:
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
        has_more=more,
    )


@pytest.mark.asyncio
async def test_a_run_finishes_once_after_all_of_its_pages() -> None:
    source = Source(
        [_page("a", "b", cursor="c1", more=True), _page("c", "d", "e", cursor="c2", more=False)]
    )
    ingest = Ingest()

    await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        _SOURCE, agent_did="did:a", owner_id="worker"
    )

    assert ingest.ingested == ["a", "b", "c", "d", "e"]
    assert ingest.finishes == 1


@pytest.mark.asyncio
async def test_pages_that_landed_before_a_failure_are_still_finished() -> None:
    source = Source([_page("a", cursor="c1", more=True), _page("b", cursor="c2", more=False)])
    ingest = Ingest(fail_on=frozenset({"b"}))

    with pytest.raises(SyncError):
        await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
            _SOURCE, agent_did="did:a", owner_id="worker", limits=SyncLimits(retries=0)
        )

    assert ingest.ingested == ["a"]
    assert ingest.finishes == 1


@pytest.mark.asyncio
async def test_a_completed_crawl_resumes_from_its_checkpoint_after_a_restart() -> None:
    """After the backfill only the changes are asked for, even across a restart.

    The durable store outlives the coordinator (the process); a new coordinator
    must hand the source the checkpoint it committed, not start over.
    """
    store = InMemorySourceSyncStore()
    first = Source([_page("a", "b", cursor="delta-1", more=False)])
    await ConnectedDataCoordinator(first, Ingest(), store).run(
        _SOURCE, agent_did="did:a", owner_id="worker-before-restart"
    )

    after_restart = Source([_page("c", cursor="delta-2", more=False)])
    ingest = Ingest()
    await ConnectedDataCoordinator(after_restart, ingest, store).run(
        _SOURCE, agent_did="did:a", owner_id="worker-after-restart"
    )

    assert first.checkpoints == [None]
    assert after_restart.checkpoints == ["delta-1"]
    assert ingest.ingested == ["c"]
    assert (await store.get_state("did:a", "source")).cursor == "delta-2"
