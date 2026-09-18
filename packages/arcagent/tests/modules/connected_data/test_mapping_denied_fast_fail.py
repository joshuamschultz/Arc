"""SPEC-082 COMP-011 (T-1097 RED) — a denied mapping fails the run fast, account-wide.

REQ-430: when a mapping is denied, the coordinator SHALL fail the run fast
account-wide rather than failing every object before aborting via the
"all failed" guard.

Today ``MappingDeniedError`` carries ``code == "mapping_denied"``, which is NOT
in the coordinator's ``_FATAL_SYNC_CODES`` set. So a per-object denial is caught
in ``apply`` and charged to that one object (``outcomes["failed"] += 1``) exactly
like an ordinary flaky file. With N objects and a denied mapping the coordinator
therefore attempts ALL N ingests, and only then — if none succeeded — raises the
generic ``SyncError("every object on the page failed to ingest")``. Worse, if a
single object is denied among healthy ones, the denial is silently absorbed and
the run COMPLETES, advancing the cursor over data an operator never authorized.

These tests fail RED because that fatal-code path does not exist yet.
"""

from __future__ import annotations

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    MappingDeniedError,
    MappingPlan,
    SyncError,
    SyncLimits,
    SyncStatus,
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
    return SourceDescription(connection_id="source", source_kind="test", account_id="account")


class _MappingDeniedIngest(FakeIngest):
    """An ingest port whose mapping is denied for one or all objects mid-run."""

    def __init__(self, deny: set[str] | None) -> None:
        super().__init__()
        self._deny = deny  # None means deny every object
        self.attempts = 0

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        self.attempts += 1
        if self._deny is None or source_object.object_id in self._deny:
            raise MappingDeniedError()
        await super().ingest(source, source_object, content, mapping)


@pytest.mark.asyncio
async def test_denied_mapping_aborts_before_trying_every_object() -> None:
    """A denied mapping is an account-wide verdict — abort on the first object.

    The whole account shares one mapping. If the mapping is denied, trying object
    two through N is wasted work against a decision that already covers them all.
    The run must abort fast with the denial's own reason, not grind through every
    object and then report the generic "every object failed".
    """
    source = FakeSource([page("a", "b", "c", "d", "e", cursor="")])
    ingest = _MappingDeniedIngest(deny=None)

    with pytest.raises(MappingDeniedError):
        await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
            _described(),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(retries=0, max_concurrency=1),
        )

    assert ingest.attempts == 1, (
        "a denied mapping was retried object-by-object instead of failing the run fast; "
        f"{ingest.attempts} ingest attempts were made"
    )


@pytest.mark.asyncio
async def test_one_denied_object_does_not_let_the_run_complete() -> None:
    """A denial buried among healthy objects must still stop the whole run.

    With the denial treated as an ordinary per-object failure, healthy siblings
    succeed, the "all failed" guard never fires, and the run COMPLETES — silently
    advancing the cursor over content the operator never authorized. Account-wide
    fail-fast means the denied object aborts the run before the siblings land.
    """
    source = FakeSource([page("denied", "ok-1", "ok-2", "ok-3", cursor="")])
    ingest = _MappingDeniedIngest(deny={"denied"})
    store = InMemorySourceSyncStore()

    with pytest.raises(MappingDeniedError):
        await ConnectedDataCoordinator(source, ingest, store).run(
            _described(),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(retries=0, max_concurrency=1),
        )

    state = await store.get_state("did:a", "source")
    assert state.status is not SyncStatus.COMPLETE, "a denied mapping let the run complete"
    assert ingest.ingested == [], "healthy siblings were ingested past a denied mapping"


@pytest.mark.asyncio
async def test_denial_is_not_flattened_into_the_generic_all_failed_error() -> None:
    """The denial's reason must survive — an operator needs 'mapping_denied'.

    Flattened into ``SyncError("every object ... failed to ingest")`` the surface
    can only say something went wrong, never that the mapping itself was refused.
    """
    source = FakeSource([page("a", "b", cursor="")])
    ingest = _MappingDeniedIngest(deny=None)

    with pytest.raises(SyncError) as caught:
        await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
            _described(),
            agent_did="did:a",
            owner_id="worker",
            limits=SyncLimits(retries=0, max_concurrency=1),
        )

    assert caught.value.code == "mapping_denied", (
        f"the mapping-denied reason was lost; code was {caught.value.code!r}"
    )
