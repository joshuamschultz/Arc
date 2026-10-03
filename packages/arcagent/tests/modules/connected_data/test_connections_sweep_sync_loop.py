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

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    KnowledgeHome,
    LeaseLostError,
    MappingPlan,
    SyncError,
    SyncLimits,
    SyncStatus,
)
from arcagent.extension.connection_health import HealthSignal
from arcagent.extension.credentials import CredentialRenewalError
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
from arcagent.modules.connected_data import (
    ConnectedDataCoordinator,
    SourceRefusedError,
    SourceUnreachableError,
)
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
    before = (await store.get_state(_DID, "mail")).cursor
    lease = await store.acquire_lease(_DID, "mail", "dead-process", ttl_seconds=0.02)
    assert lease is not None
    assert await store.commit_page(
        _DID,
        "mail",
        expected_cursor=before,
        next_cursor=cursor,
        page_id="before-the-crash",
        page_count=1,
        page_bytes=1,
        owner_id="dead-process",
        fencing_token=lease.fencing_token,
    )
    await asyncio.sleep(0.05)
    assert (await store.get_state(_DID, "mail")).status == SyncStatus.RUNNING


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
            return (await store.get_state(_DID, "mail")).status == SyncStatus.COMPLETE

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


# --- D6: a run killed mid-flight is reconciled, then resumes from its cursor -------------


async def test_a_running_row_with_a_lapsed_lease_is_marked_interrupted_and_resumes() -> None:
    catalog, store, provider = SourceCatalog(), _RecordingStore(), _Provider()
    await _leave_a_dead_run(store, "c-before-crash")
    await catalog.register("mail", provider)
    service = _service(catalog, store)
    await service.start()
    try:

        async def complete() -> bool:
            return (await store.get_state(_DID, "mail")).status == SyncStatus.COMPLETE

        assert await _until(complete)
    finally:
        await service.close()

    assert store.statuses[0] == ("failed", "interrupted"), store.statuses
    assert provider.checkpoints[0] == "c-before-crash", "the resumed run lost its cursor"


async def test_a_run_that_dies_while_the_service_is_up_is_reconciled_on_the_next_tick() -> None:
    catalog, store, provider = SourceCatalog(), _RecordingStore(), _Provider()
    await catalog.register("mail", provider)
    service = _service(catalog, store)
    await service.start()
    try:

        async def complete() -> bool:
            return (await store.get_state(_DID, "mail")).status == SyncStatus.COMPLETE

        assert await _until(complete)
        await _leave_a_dead_run(store, "c-other-process")
        store.statuses.clear()
        await catalog.register("slack", _Provider())  # any change makes the monitor tick

        async def reconciled() -> bool:
            return ("failed", "interrupted") in store.statuses

        assert await _until(reconciled), store.statuses
    finally:
        await service.close()


async def test_a_live_run_in_another_process_is_never_marked_interrupted() -> None:
    catalog, store, provider = SourceCatalog(), _RecordingStore(), _Provider()
    held = await store.acquire_lease(_DID, "mail", "live-process", ttl_seconds=60)
    assert held is not None
    await catalog.register("mail", provider)
    service = _service(catalog, store)
    await service.start()
    try:
        await asyncio.sleep(0.1)
    finally:
        await service.close()
    assert ("failed", "interrupted") not in store.statuses
    assert (await store.get_state(_DID, "mail")).status == SyncStatus.RUNNING


# --- D8: a deadline crossed inside a page keeps that page --------------------------------


class _FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class _SlowFetches(_Provider):
    """Every object fetch costs one second of working time."""

    def __init__(self, fake: _FakeTime) -> None:
        super().__init__()
        self.fake = fake
        self.pages = 0

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        self.pages += 1
        return SyncSourcePage(
            objects=tuple(_object(f"p{self.pages}-o{n}") for n in range(3)),
            next_checkpoint=f"c{self.pages}",
            has_more=True,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        self.fake.now += 1.0
        return await super().fetch_source(request)


async def test_a_deadline_crossed_mid_page_commits_the_page_and_stops_at_the_ceiling() -> None:
    fake = _FakeTime()
    ingest = _Ingest()
    coordinator = ConnectedDataCoordinator(
        _SlowFetches(fake), ingest, InMemorySourceSyncStore(), clock=fake.clock, sleep=fake.sleep
    )

    result = await coordinator.run(
        SourceDescription(connection_id="mail", source_kind="test", account_id="a"),
        agent_did=_DID,
        owner_id="worker",
        limits=SyncLimits(max_seconds=2.0, max_duty_fraction=1.0, retries=0),
    )

    assert result.status == SyncStatus.COMPLETE
    assert result.cursor == "c1", "the page in hand was thrown away"
    assert result.budget_reached and coordinator.stopped_at_ceiling
    assert ingest.ingested == ["p1-o0", "p1-o1", "p1-o2"]


# --- D9: one long unit of work keeps its lease -------------------------------------------


class _CountingStore(InMemorySourceSyncStore):
    def __init__(self, *, refuse_after: int | None = None) -> None:
        super().__init__()
        self.renewals = 0
        self.refuse_after = refuse_after

    async def renew_lease(self, *args: Any, **kwargs: Any) -> bool:
        self.renewals += 1
        if self.refuse_after is not None and self.renewals > self.refuse_after:
            return False  # another writer took the lease while this one was busy
        return await super().renew_lease(*args, **kwargs)


class _OneLongFetch(_Provider):
    def __init__(self, seconds: float | None) -> None:
        super().__init__()
        self.seconds = seconds

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if self.seconds is None:
            await asyncio.Event().wait()  # a provider that never answers
        await asyncio.sleep(self.seconds or 0)
        return SyncSourcePage(objects=(_object("doc"),), next_checkpoint="c1", has_more=False)


async def test_one_fetch_longer_than_the_lease_still_completes() -> None:
    store = _CountingStore()
    result = await ConnectedDataCoordinator(_OneLongFetch(0.45), _Ingest(), store).run(
        SourceDescription(connection_id="mail", source_kind="test", account_id="a"),
        agent_did=_DID,
        owner_id="worker",
        limits=SyncLimits(max_seconds=0.3, max_duty_fraction=1.0, retries=0),
    )

    assert result.status == SyncStatus.COMPLETE and result.cursor == "c1"
    assert store.renewals >= 2, "the lease was not renewed while the fetch ran"


async def test_a_lost_lease_stops_the_run_at_once_and_commits_nothing() -> None:
    store = _CountingStore(refuse_after=1)
    coordinator = ConnectedDataCoordinator(_OneLongFetch(None), _Ingest(), store)

    with pytest.raises(LeaseLostError):
        await asyncio.wait_for(
            coordinator.run(
                SourceDescription(connection_id="mail", source_kind="test", account_id="a"),
                agent_did=_DID,
                owner_id="worker",
                limits=SyncLimits(max_seconds=0.15, max_duty_fraction=1.0, retries=0),
            ),
            timeout=2.0,
        )
    assert (await store.get_state(_DID, "mail")).cursor is None


async def test_an_expired_lease_taken_by_another_writer_fences_the_old_one() -> None:
    """Abuse case: a writer whose lease lapsed can never commit over the new holder."""
    store = InMemorySourceSyncStore()
    old = await store.acquire_lease(_DID, "mail", "old-writer", ttl_seconds=0.02)
    assert old is not None
    await asyncio.sleep(0.05)
    new = await store.acquire_lease(_DID, "mail", "new-writer", ttl_seconds=60)
    assert new is not None

    assert not await store.renew_lease(
        _DID, "mail", owner_id="old-writer", fencing_token=old.fencing_token, ttl_seconds=60
    )
    assert not await store.commit_page(
        _DID,
        "mail",
        expected_cursor=None,
        next_cursor="stolen",
        page_id="p",
        page_count=1,
        page_bytes=1,
        owner_id="old-writer",
        fencing_token=old.fencing_token,
    )
    assert (await store.get_state(_DID, "mail")).cursor is None


# --- D10: a provider rate limit defers the next run; it is not a crash ------------------


def test_sync_error_carries_the_providers_retry_after() -> None:
    assert SyncError("slow down", code="rate_limited", retry_after=42.0).retry_after == 42.0
    assert SyncError("broken").retry_after is None


async def test_a_rate_limit_keeps_the_cursor_and_carries_retry_after() -> None:
    store = InMemorySourceSyncStore()
    provider = _Provider("more", "rate_limited", retry_after=42.0)

    with pytest.raises(SyncError) as raised:
        await ConnectedDataCoordinator(provider, _Ingest(), store).run(
            SourceDescription(connection_id="mail", source_kind="test", account_id="a"),
            agent_did=_DID,
            owner_id="worker",
            limits=SyncLimits(retries=0, max_duty_fraction=1.0),
        )

    assert raised.value.code == "rate_limited" and raised.value.retry_after == 42.0
    state = await store.get_state(_DID, "mail")
    assert state.cursor == "c1", "the committed page before the limit was lost"
    assert state.status == SyncStatus.IDLE and state.error_code == "rate_limited"


async def test_six_rate_limits_in_a_row_never_escalate_and_are_not_crashes() -> None:
    catalog, store, health = SourceCatalog(), InMemorySourceSyncStore(), _Health()
    provider = _Provider("rate_limited", retry_after=0.01)
    events: list[tuple[str, dict[str, Any]]] = []
    await catalog.register("mail", provider)
    service = _service(catalog, store, health=health, events=events, failure_ceiling=3)
    await service.start()
    try:

        async def six_runs() -> bool:
            return len(provider.checkpoints) >= 6

        assert await _until(six_runs)
        listed = (await service.list_sources())[0]
    finally:
        await service.close()

    actions = [action for action, _ in events]
    assert "connected_data.sync.crashed" not in actions
    assert actions.count("connected_data.sync.deferred") >= 6
    assert listed.status != "needs_attention"
    assert not [s for _, s in health.signals if not s.ok], "a rate limit was reported as a failure"


async def test_a_rate_limit_waits_out_the_providers_retry_after() -> None:
    catalog, store = SourceCatalog(), InMemorySourceSyncStore()
    provider = _Provider("rate_limited", retry_after=42.0)
    events: list[tuple[str, dict[str, Any]]] = []
    await catalog.register("mail", provider)
    service = _service(catalog, store, events=events)
    await service.start()
    try:

        async def deferred() -> bool:
            return any(action == "connected_data.sync.deferred" for action, _ in events)

        assert await _until(deferred)
        await asyncio.sleep(0.1)
    finally:
        await service.close()

    assert len(provider.checkpoints) == 1, "a rate-limited source was retried before its time"
    payload = next(p for a, p in events if a == "connected_data.sync.deferred")
    assert payload["retry_in_seconds"] == pytest.approx(42.0)


# --- D11: a resource listing says WHY it failed -------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        SourceError(SourceFailureCode.AUTH_REQUIRED, "oauth2: invalid_grant"),
        CredentialRenewalError(error_code="credential_missing", message="not connected"),
    ],
)
async def test_a_signed_out_account_asks_for_a_reconnect(failure: Exception) -> None:
    catalog, provider = SourceCatalog(), _Provider()
    provider.resource_failure = failure
    await catalog.register("systems", provider)
    service = _service(catalog, InMemorySourceSyncStore())

    with pytest.raises(SourceRefusedError) as refused:
        await service.list_resources("systems")
    assert refused.value.needs_reconnect


@pytest.mark.parametrize(
    "failure",
    [
        SourceError(SourceFailureCode.TRANSIENT, "upstream 502"),
        SourceError(SourceFailureCode.RATE_LIMITED, "slow down", retry_after=5),
        CredentialRenewalError(error_code="server_error", message="token endpoint down"),
    ],
)
async def test_a_temporary_failure_is_unreachable_not_refused(failure: Exception) -> None:
    catalog, provider = SourceCatalog(), _Provider()
    provider.resource_failure = failure
    await catalog.register("systems", provider)
    service = _service(catalog, InMemorySourceSyncStore())

    with pytest.raises(SourceUnreachableError):
        await service.list_resources("systems")


async def test_a_real_refusal_stays_a_refusal_without_a_reconnect() -> None:
    catalog, provider = SourceCatalog(), _Provider()
    provider.resource_failure = SourceError(SourceFailureCode.NOT_FOUND, "select one folder")
    await catalog.register("systems", provider)
    service = _service(catalog, InMemorySourceSyncStore())

    with pytest.raises(SourceRefusedError) as refused:
        await service.list_resources("systems")
    assert not refused.value.needs_reconnect
    assert refused.value.detail == "select one folder"


# --- D7 seam: closing the service releases the shared stores' pinned keys ---------------


class _SharedSpy:
    """Only the teardown seam of SharedKnowledge: close() releases every pinned key."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


async def test_closing_the_service_releases_the_shared_store_keys() -> None:
    shared = _SharedSpy()
    service = _service(SourceCatalog(), InMemorySourceSyncStore(), shared=shared)
    await service.start()

    await service.close()

    assert shared.closed == 1
