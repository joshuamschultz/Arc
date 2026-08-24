from __future__ import annotations

import asyncio

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    LeaseLostError,
    MappingPendingError,
    MappingPlan,
    ObjectNotIngestibleError,
    SyncError,
    SyncLimits,
    SyncStatus,
    TransientSyncError,
)
from arcagent.extension.source import (
    FetchSourceObject,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
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
        self.snapshots: list[frozenset[str]] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        self.staged.append("mapping")
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
        if self.fail:
            raise RuntimeError("temporary ingest outage")
        self.ingested.append(source_object.object_id)

    async def complete_snapshot(
        self,
        source: SourceDescription,
        object_ids: frozenset[str],
        mapping: MappingPlan,
    ) -> None:
        del source, mapping
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
async def test_full_snapshot_reconciles_only_after_every_page_succeeds() -> None:
    source = FakeSource([page("a", "b", cursor="c1"), page("c", cursor="")])
    ingest = FakeIngest()
    await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(
            connection_id="source",
            source_kind="test",
            account_id="account",
            supports_incremental=False,
        ),
        agent_did="did:a",
        owner_id="worker",
    )
    assert ingest.snapshots == [frozenset({"a", "b", "c"})]


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
    assert ingest.snapshots == []


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
async def test_a_large_account_crawls_across_runs() -> None:
    """A big account is mapped gradually, a page at a time, run after run.

    The budget bounds one run's work; it is not a verdict on the account.
    Failing on it marked a large account permanently `failed` so nothing in it
    was ever indexed, and stopping mid-page left the cursor unmoved so the next
    run met the same wall forever. A run now always advances by at least one
    page and resumes exactly where it stopped.
    """
    source = FakeSource([page("a", cursor="c1"), page("b", cursor="")])
    ingest = FakeIngest()
    store = InMemorySourceSyncStore()
    described = SourceDescription(connection_id="source", source_kind="test", account_id="account")
    tiny = SyncLimits(max_bytes=1)

    first = await ConnectedDataCoordinator(source, ingest, store).run(
        described, agent_did="did:a", owner_id="worker", limits=tiny
    )

    assert first.status is not SyncStatus.FAILED
    assert ingest.ingested == ["a"]

    await ConnectedDataCoordinator(source, ingest, store).run(
        described, agent_did="did:a", owner_id="worker", limits=tiny
    )

    assert ingest.ingested == ["a", "b"]


@pytest.mark.asyncio
async def test_a_partial_run_reconciles_nothing() -> None:
    """A stopped run has not seen the account, so it cannot judge what is gone."""
    source = FakeSource([page("a", cursor="c1"), page("b", cursor="")])
    ingest = FakeIngest()

    await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(
            connection_id="source",
            source_kind="test",
            account_id="account",
            supports_incremental=False,
        ),
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_bytes=1),
    )

    assert ingest.snapshots == []


@pytest.mark.asyncio
async def test_one_oversized_object_is_skipped_not_fatal() -> None:
    """An object bigger than the whole budget can never be taken, on any run.

    Failing on it retried it forever and no other document in the account was
    ever indexed.
    """
    source = FakeSource(
        [
            SyncSourcePage(
                objects=(
                    SourceObject(
                        object_id="huge",
                        locator="/huge.bin",
                        kind=SourceObjectKind.FILE,
                        version="1",
                        size=10_000,
                    ),
                    SourceObject(
                        object_id="small",
                        locator="/small.txt",
                        kind=SourceObjectKind.FILE,
                        version="1",
                        size=1,
                    ),
                ),
                next_checkpoint="",
                has_more=False,
            )
        ]
    )
    ingest = FakeIngest()

    result = await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_bytes=100),
    )

    assert result.status is SyncStatus.COMPLETE
    assert ingest.ingested == ["small"]


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


def mixed_page(cursor: str) -> SyncSourcePage:
    """What a real document account returns: files sitting inside folders."""
    return SyncSourcePage(
        objects=(
            SourceObject(
                object_id="folder-1",
                locator="/Projects",
                kind=SourceObjectKind.FOLDER,
                version=None,
                size=0,
            ),
            SourceObject(
                object_id="report",
                locator="/Projects/report.txt",
                kind=SourceObjectKind.FILE,
                version="1",
                size=1,
            ),
        ),
        next_checkpoint=cursor,
        has_more=bool(cursor),
    )


@pytest.mark.asyncio
async def test_a_folder_does_not_fail_the_sync_that_contains_it() -> None:
    """A folder holds other entries; it has no content of its own.

    Handed to the ingest port with content of None it was refused, and one
    folder in a listing failed the whole sync — so no account with folders,
    which is every real one, could finish.
    """
    source = FakeSource([mixed_page(cursor="")])
    ingest = FakeIngest()

    result = await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
    )

    assert result.status.value == "complete"
    assert ingest.ingested == ["report"]


@pytest.mark.asyncio
async def test_a_folder_is_not_reconciled_as_a_missing_object() -> None:
    """Kept in the snapshot set, a never-ingested folder reads as deleted later."""
    source = FakeSource([mixed_page(cursor="")])
    ingest = FakeIngest()

    await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(
            connection_id="source",
            source_kind="test",
            account_id="account",
            supports_incremental=False,
        ),
        agent_did="did:a",
        owner_id="worker",
    )

    assert ingest.snapshots == [frozenset({"report"})]


class _RefusingFetchSource(FakeSource):
    """A source that answers the listing but refuses one object's content."""

    def __init__(self, pages: list[SyncSourcePage], code: SourceFailureCode) -> None:
        super().__init__(pages)
        self._code = code

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise SourceError(self._code, f"refused {request.object_id}")


@pytest.mark.asyncio
async def test_an_object_the_source_calls_too_large_is_skipped() -> None:
    """The source knows sizes this side only estimated."""
    source = _RefusingFetchSource([page("huge", cursor="")], SourceFailureCode.TOO_LARGE)
    ingest = FakeIngest()

    result = await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
    )

    assert result.status is SyncStatus.COMPLETE
    assert ingest.ingested == []


@pytest.mark.asyncio
async def test_any_other_refusal_still_ends_the_run() -> None:
    """A broken account must never be mistaken for a pile of big files.

    Deciding this from the exception's text skipped every object of every kind
    and reported a healthy, empty sync over a source that was refusing outright.
    """
    source = _RefusingFetchSource([page("thing", cursor="")], SourceFailureCode.NOT_FOUND)
    ingest = FakeIngest()

    store = InMemorySourceSyncStore()

    with pytest.raises(SyncError, match="refused"):
        await ConnectedDataCoordinator(source, ingest, store).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )

    assert (await store.get_state("did:a", "source")).status is SyncStatus.FAILED


class _CapRecordingSource(FakeSource):
    """Records the byte cap each fetch was given."""

    def __init__(self, pages: list[SyncSourcePage]) -> None:
        super().__init__(pages)
        self.caps: list[int] = []

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        self.caps.append(request.max_bytes)
        return await super().fetch_source(request)


@pytest.mark.asyncio
async def test_every_object_in_a_page_gets_the_same_byte_cap() -> None:
    """A cap that shrank as the page filled made a whole account unreadable.

    Later files in a page were handed a cap of a few bytes, the source refused
    them as too large, and the sync reported nothing but oversized files.
    """
    source = _CapRecordingSource([page("a", "b", "c", cursor="")])

    await ConnectedDataCoordinator(source, FakeIngest(), InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
        limits=SyncLimits(max_bytes=4096),
    )

    assert source.caps == [4096, 4096, 4096]


class _RefusingIngest(FakeIngest):
    """An ingest port that cannot take one particular object."""

    def __init__(self, refuse: str, error: Exception) -> None:
        super().__init__()
        self._refuse = refuse
        self._error = error

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        if source_object.object_id == self._refuse:
            raise self._error
        await super().ingest(source, source_object, content, mapping)


@pytest.mark.asyncio
async def test_one_unreadable_object_does_not_cost_the_whole_account() -> None:
    """A media type nothing can read is one skipped file, not a dead source.

    Left to propagate it ended the sync, so a single image in a folder made the
    entire account permanently `failed` with nothing in it indexed.
    """
    source = FakeSource([page("readable", "weird", "also-fine", cursor="")])
    ingest = _RefusingIngest("weird", ObjectNotIngestibleError("unsupported_media_type"))

    result = await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
        SourceDescription(connection_id="source", source_kind="test", account_id="account"),
        agent_did="did:a",
        owner_id="worker",
    )

    assert result.status is SyncStatus.COMPLETE
    assert sorted(ingest.ingested) == ["also-fine", "readable"]


@pytest.mark.asyncio
async def test_an_ingest_failure_that_is_not_about_one_object_still_fails() -> None:
    """A broken store must not be mistaken for a pile of unreadable files."""
    source = FakeSource([page("a", cursor="")])
    ingest = _RefusingIngest("a", RuntimeError("ingest store is down"))

    with pytest.raises(RuntimeError, match="store is down"):
        await ConnectedDataCoordinator(source, ingest, InMemorySourceSyncStore()).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )


class AuthRefusingSource:
    """A source whose credential has been revoked — only a person can fix it."""

    def __init__(self) -> None:
        self.attempts = 0

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.attempts += 1
        raise SourceError(
            SourceFailureCode.AUTH_REQUIRED,
            'oauth2: "invalid_grant" "Token has been expired or revoked."',
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise AssertionError("nothing should be fetched")


@pytest.mark.asyncio
async def test_a_revoked_credential_is_reported_as_auth_required_and_not_retried() -> None:
    """The typed reason must survive the wrap into SyncError.

    Flattened to the generic ``sync_error``, an operator with a dead Google
    token was told only that something failed — and every surface that could
    have said "reconnect this account" had nothing to say it with.
    """
    source = AuthRefusingSource()
    store = InMemorySourceSyncStore()

    with pytest.raises(SyncError) as caught:
        await ConnectedDataCoordinator(source, FakeIngest(), store).run(
            SourceDescription(connection_id="source", source_kind="test", account_id="account"),
            agent_did="did:a",
            owner_id="worker",
        )

    assert caught.value.code == "auth_required"
    assert not isinstance(caught.value, TransientSyncError)
    assert source.attempts == 1, "a revoked credential must not be retried"
    state = await store.get_state("did:a", "source")
    assert state.error_code == "auth_required"
