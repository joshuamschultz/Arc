"""Alpha-2 P18-0 "stop the bleeding" -- the sync loop, driven for real.

DGX evidence behind each test:

* Gmail ``blackarc`` was dead for 33 days: the history cursor aged out (404) and a
  dead cursor was retried forever. The coordinator now restarts from a snapshot.
* One Dropbox file answering 500 aborted its whole page, 307 times.
* Unknown failures defaulted to TRANSIENT and never escalated; the operator
  notifier was never wired, so a dead account stayed silent.
* Jira sat durably ``running`` for nine days because a stalled run only touched
  in-memory status, so the card could not tell "stuck" from "idle".

Nothing here fakes the service, the coordinator or the sync store; only the
provider (the source) and the operator's delivery channel are doubles.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    KnowledgeHome,
    MappingPlan,
    SyncError,
    SyncLimits,
    SyncStatus,
    TransientSyncError,
)
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
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data import ConnectedDataCoordinator
from arcagent.modules.connected_data.service import ConnectedDataService

_DID = "did:agent"


def _object(object_id: str) -> SourceObject:
    return SourceObject(
        object_id=object_id,
        locator=object_id,
        kind=SourceObjectKind.FILE,
        version="1",
        size=1,
    )


def _page(*ids: str, cursor: str, more: bool = False) -> SyncSourcePage:
    return SyncSourcePage(
        objects=tuple(_object(i) for i in ids), next_checkpoint=cursor, has_more=more
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


def _description(*, incremental: bool = True) -> SourceDescription:
    return SourceDescription(
        connection_id="source",
        source_kind="test",
        account_id="account",
        supports_incremental=incremental,
    )


async def _seed_cursor(store: InMemorySourceSyncStore, cursor: str) -> None:
    """Leave the durable state where a previous run's last commit left it."""
    lease = await store.acquire_lease(_DID, "source", "earlier", ttl_seconds=60)
    assert lease is not None
    assert await store.commit_page(
        _DID,
        "source",
        expected_cursor=None,
        next_cursor=cursor,
        page_id="seed",
        page_count=1,
        page_bytes=1,
        owner_id="earlier",
        fencing_token=lease.fencing_token,
    )
    await store.release_lease(
        _DID, "source", owner_id="earlier", fencing_token=lease.fencing_token
    )


# --- CHECKPOINT_INVALID: reset the cursor, resync from a snapshot ------------------


class _AgedOutCursor:
    """A history cursor the provider no longer honours; a snapshot always works.

    With ``always_invalid`` the provider keeps rejecting every cursor it hands out
    too, which is a provider that is simply broken rather than one with an old cursor.
    """

    def __init__(self, *, always_invalid: bool = False) -> None:
        self.calls: list[str | None] = []
        self._always_invalid = always_invalid

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.calls.append(request.checkpoint)
        if request.checkpoint is not None and (
            self._always_invalid or request.checkpoint == "stale-history"
        ):
            raise SourceError(SourceFailureCode.CHECKPOINT_INVALID, "history cursor expired")
        return _page("a", "b", cursor="fresh-history", more=self._always_invalid)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"x",
        )


async def test_a_dead_checkpoint_resets_to_a_snapshot_and_the_sync_completes() -> None:
    store = InMemorySourceSyncStore()
    await _seed_cursor(store, "stale-history")
    source, ingest, events = _AgedOutCursor(), _Ingest(), []

    async def audit(action: str, payload: Any) -> None:
        events.append(action)

    result = await ConnectedDataCoordinator(source, ingest, store, audit=audit).run(
        _description(), agent_did=_DID, owner_id="worker", limits=SyncLimits(retries=0)
    )

    assert source.calls == ["stale-history", None]
    assert result.status is SyncStatus.COMPLETE
    assert result.cursor == "fresh-history"
    assert ingest.ingested == ["a", "b"]
    assert "connected_data.sync.checkpoint_reset" in events


async def test_a_checkpoint_the_snapshot_also_rejects_is_a_failure_not_a_loop() -> None:
    store = InMemorySourceSyncStore()
    await _seed_cursor(store, "stale-history")
    source = _AgedOutCursor(always_invalid=True)

    with pytest.raises(SyncError) as raised:
        await ConnectedDataCoordinator(source, _Ingest(), store).run(
            _description(), agent_did=_DID, owner_id="worker", limits=SyncLimits(retries=0)
        )

    assert raised.value.code == SourceFailureCode.CHECKPOINT_INVALID.value
    assert source.calls == ["stale-history", None, "fresh-history"], (
        "the reset must happen once per run, not forever"
    )


# --- one object's failure is that object's, never the page's -----------------------


class _OneBadFile:
    """A provider where file ``b`` answers 5xx every time and the rest are fine."""

    def __init__(self, *, every_file_fails: bool = False) -> None:
        self._every_file_fails = every_file_fails

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return _page("a", "b", "c", cursor="end")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        if self._every_file_fails or request.object_id == "b":
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox service remained unavailable")
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"x",
        )


async def test_one_file_that_keeps_failing_is_skipped_and_the_page_still_lands() -> None:
    store, ingest, events = InMemorySourceSyncStore(), _Ingest(), []

    async def audit(action: str, payload: Any) -> None:
        events.append((action, dict(payload)))

    result = await ConnectedDataCoordinator(_OneBadFile(), ingest, store, audit=audit).run(
        _description(), agent_did=_DID, owner_id="worker", limits=SyncLimits(retries=0)
    )

    assert result.status is SyncStatus.COMPLETE
    assert sorted(ingest.ingested) == ["a", "c"]
    skipped = [payload for action, payload in events if action.endswith("object_skipped")]
    assert [payload["reason"] for payload in skipped] == ["object_transient"]


async def test_every_file_failing_is_an_outage_and_the_cursor_does_not_advance() -> None:
    store, ingest = InMemorySourceSyncStore(), _Ingest()

    with pytest.raises(TransientSyncError):
        await ConnectedDataCoordinator(_OneBadFile(every_file_fails=True), ingest, store).run(
            _description(), agent_did=_DID, owner_id="worker", limits=SyncLimits(retries=0)
        )

    assert (await store.get_state(_DID, "source")).cursor is None
    assert ingest.ingested == []


# --- the service: escalation, notification, durable stall --------------------------


class _Provider:
    """A source that behaves per script: crash, hang, auth-fail, or sync."""

    def __init__(self, *script: str) -> None:
        self._script = list(script)
        self.syncs = 0
        self.inspections = 0

    def _next(self) -> str:
        return self._script.pop(0) if len(self._script) > 1 else self._script[0]

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        self.inspections += 1
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="test",
            account_id=request.connection_id,
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Test source",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.syncs += 1
        behaviour = self._next()
        if behaviour == "weird":
            raise RuntimeError("something nobody classified")
        if behaviour == "transient":
            raise SourceError(SourceFailureCode.TRANSIENT, "gog exited 5: upstream hiccup")
        if behaviour == "auth":
            raise SourceError(SourceFailureCode.AUTH_REQUIRED, 'oauth2: "invalid_grant"')
        if behaviour == "hang":
            await asyncio.Event().wait()
        return _page("doc", cursor=f"c{self.syncs}")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"x",
        )

    async def list_source_resources(self, request: Any) -> tuple[Any, ...]:
        return ()

    async def select_source_resources(self, request: Any) -> None:
        return None

    async def close_source(self) -> None:
        return None


class _Notifications:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, connection_id: str, reason: str) -> None:
        self.sent.append((connection_id, reason))


async def _service(
    provider: _Provider,
    store: InMemorySourceSyncStore,
    notifier: _Notifications,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    **options: Any,
) -> ConnectedDataService:
    catalog = SourceCatalog()
    await catalog.register("mail", provider)

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
    service = ConnectedDataService(
        catalog,
        agent_did=_DID,
        sync_store_opener=open_store,
        ingest_factory=lambda _: _Ingest(),
        global_concurrency=2,
        operator_notifier=notifier,
        audit=audit,
        **settings,
    )
    await service.start()
    return service


async def _until(predicate: Any, *, seconds: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def _status(service: ConnectedDataService) -> str:
    return (await service.list_sources())[0].status


@pytest.mark.parametrize("behaviour", ["weird", "transient"])
async def test_an_unknown_failure_that_never_clears_escalates_and_tells_the_operator_once(
    behaviour: str,
) -> None:
    provider, store, notes = _Provider(behaviour), InMemorySourceSyncStore(), _Notifications()
    service = await _service(provider, store, notes)
    try:

        async def escalated() -> bool:
            return await _status(service) == "needs_attention"

        assert await _until(escalated), "an endlessly failing source never escalated"
        syncs_at_escalation = provider.syncs
        await asyncio.sleep(0.15)
    finally:
        await service.close()

    assert provider.syncs == syncs_at_escalation, "an escalated source must stop being hammered"
    assert provider.syncs >= 3
    assert len(notes.sent) == 1
    assert notes.sent[0][0] == "mail"


async def test_a_blip_that_clears_never_escalates() -> None:
    provider = _Provider("weird", "weird", "ok")
    store, notes = InMemorySourceSyncStore(), _Notifications()
    service = await _service(provider, store, notes)
    try:

        async def synced() -> bool:
            return (await service.list_sources())[0].status == "complete"

        assert await _until(synced)
    finally:
        await service.close()

    assert notes.sent == []


async def test_transient_then_invalid_grant_escalates_once_and_survives_a_restart() -> None:
    """J1 gate G5: a dead source escalates once, and a restart does not forget it."""
    provider = _Provider("transient", "transient", "auth")
    store, notes = InMemorySourceSyncStore(), _Notifications()
    first = await _service(provider, store, notes)
    try:

        async def escalated() -> bool:
            return await _status(first) == "needs_attention"

        assert await _until(escalated)
    finally:
        await first.close()
    assert [reason for _, reason in notes.sent] == ["auth_required"]

    second = await _service(provider, store, notes)
    try:

        async def still_escalated() -> bool:
            return await _status(second) == "needs_attention"

        assert await _until(still_escalated), "a restart forgot the dead source"
        syncs = provider.syncs
        await asyncio.sleep(0.1)
    finally:
        await second.close()

    assert provider.syncs == syncs, "a restarted service re-hammered a dead credential"
    assert len(notes.sent) == 1, "a restart must not page the operator again"


async def test_an_escalation_by_repeated_failure_also_survives_a_restart() -> None:
    provider = _Provider("weird")
    store, notes = InMemorySourceSyncStore(), _Notifications()
    first = await _service(provider, store, notes)
    try:

        async def escalated() -> bool:
            return await _status(first) == "needs_attention"

        assert await _until(escalated)
    finally:
        await first.close()

    second = await _service(provider, store, notes)
    try:

        async def still_escalated() -> bool:
            return await _status(second) == "needs_attention"

        assert await _until(still_escalated)
    finally:
        await second.close()
    assert len(notes.sent) == 1


async def test_a_stalled_run_leaves_a_durable_failure_and_keeps_the_last_good_sync() -> None:
    """J1 gate G6 / F5: Jira sat ``running`` for nine days after a stall."""
    provider = _Provider("ok", "hang")
    store, notes, events = InMemorySourceSyncStore(), _Notifications(), []
    service = await _service(
        provider,
        store,
        notes,
        events,
        limits=SyncLimits(retries=0, max_seconds=0.05, max_duty_fraction=1.0),
        stall_grace_seconds=0.05,
        restart_backoff_seconds=30.0,
        restart_backoff_max_seconds=30.0,
        failure_ceiling=10,
    )
    try:

        async def first_sync_done() -> bool:
            return (await store.get_state(_DID, "mail")).status is SyncStatus.COMPLETE

        assert await _until(first_sync_done)
        good_sync = (await store.get_state(_DID, "mail")).last_synced_at
        assert good_sync is not None

        await service.sync_now("mail")

        async def stalled() -> bool:
            return any(action.endswith(".stalled") for action, _ in events)

        assert await _until(stalled)

        async def durable_failure() -> bool:
            return (await store.get_state(_DID, "mail")).status is SyncStatus.FAILED

        assert await _until(durable_failure), "a stall left the durable row 'running'"
        durable = await store.get_state(_DID, "mail")
        listed = (await service.list_sources())[0]
    finally:
        await service.close()

    assert durable.error_code == "sync_stalled"
    assert durable.last_synced_at == good_sync, "a failure erased the last good sync"
    assert listed.status == "failed" and listed.detail == "sync_stalled"
    assert listed.state is not None and listed.state.last_synced_at == good_sync
