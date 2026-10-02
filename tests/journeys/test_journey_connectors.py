"""Journey: a connection dies, the operator is told once, and a restart changes nothing.

The real connector routes, the real health authority, the real probe monitor, the real
connected-data sync service and a real in-memory arcstore. The only doubles are the
provider wire (what the account answers) and the operator's channel (where the notice
lands). "Restart" tears the app, the service and the monitor down and builds new ones on
the same arcstore and filesystem, which is exactly what a restarted process finds.

Alpha-2 P18-1, gates G5 (a dead source escalates once and survives a restart) and G13
(a page view makes no secret read).
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import arcagent
import pytest
from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.service import ConnectedDataService
from arcstore.source_sync import InMemorySourceSyncStore
from arcui.connection_health import ConnectionHealthMonitor

from packages.arcui.tests.connection_fleet import INSTANCE, Fleet

T0 = datetime.now(UTC).replace(microsecond=0)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class _Wire:
    """What the provider answers, and how many times it was asked."""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | invalid_grant | unavailable
        self.probes = 0

    def detail(self) -> str:
        return {
            "invalid_grant": 'oauth2: "invalid_grant" Token has been expired or revoked.',
            "unavailable": "503 Service Unavailable",
        }[self.mode]


class _Attachment:
    def __init__(self, wire: _Wire) -> None:
        self._wire = wire

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        self._wire.probes += 1
        ok = self._wire.mode == "ok"
        return ProbeResult(
            reachable=ok,
            tools=await self.describe_tools(),
            detail="" if ok else self._wire.detail(),
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Ping Acme.", classification="read_only")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="pong")


class _Channel:
    """The operator's phone: every notice that reached it, in order."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify_operator(self, text: str, *, idempotency_key: str) -> str | None:
        self.messages.append(text)
        return "telegram"


class _Source:
    """A Gmail-shaped source: dead (revoked) or healthy, optionally with an aged-out cursor."""

    def __init__(self, *, dead: bool = False, stale: str | None = None) -> None:
        self.dead, self.stale = dead, stale
        self.sync_attempts = 0
        self.checkpoints: list[str | None] = []

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="acme",
            account_id="mailbox",
            data_shape=SourceDataShape.MAIL,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.sync_attempts += 1
        self.checkpoints.append(request.checkpoint)
        if self.dead:
            raise SourceError(SourceFailureCode.AUTH_REQUIRED, 'oauth2: "invalid_grant" revoked')
        if self.stale is not None and request.checkpoint == self.stale:
            raise SourceError(SourceFailureCode.CHECKPOINT_INVALID, "404 history not found")
        return SyncSourcePage(objects=(), next_checkpoint="fresh", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise AssertionError("no objects in these journeys")

    async def close_source(self) -> None:
        return None


class _Ingest:
    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="m", homes=(KnowledgeHome.DOCUMENT,), revision="r", content_hash="h"
        )

    async def ingest(self, source: Any, source_object: Any, content: Any, mapping: Any) -> None:
        return None

    def allowed_homes(self, source: SourceDescription) -> tuple[KnowledgeHome, ...]:
        return (KnowledgeHome.DOCUMENT,)

    def canonical_source_id(self, source: SourceDescription) -> str:
        return "source-mailbox"

    async def complete_snapshot(self, source: Any, object_ids: Any, mapping: Any) -> None:
        return None

    async def reset_source(self, source: SourceDescription) -> None:
        return None

    async def purge_source(self, source: SourceDescription) -> None:
        return None


@pytest.fixture
def fleet(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Fleet:
    return Fleet(tmp_path, monkeypatch)


def _monitor(
    fleet: Fleet, wire: _Wire, channel: _Channel, clock: _Clock
) -> ConnectionHealthMonitor:
    async def opener() -> Any:
        return fleet.backend

    def connections() -> arcagent.Connections:
        return arcagent.Connections.for_deployment(
            audit=arcagent.AuditChain.held(fleet.sink),
            state_opener=opener,
            attachment_factory=lambda _manifest, _bundle, _secrets, **_kw: _Attachment(wire),
            clock=clock,
        )

    return ConnectionHealthMonitor(
        connections,
        store_opener=opener,
        agents_resolver=lambda instance: [channel],
        sink=fleet.sink,
        clock=clock,
        rng=lambda: 0.0,
        initial_delay_seconds=0,
    )


async def _service(
    fleet: Fleet,
    source: _Source,
    store: InMemorySourceSyncStore,
    *,
    agent: str | None = None,
) -> ConnectedDataService:
    catalog = SourceCatalog()
    await catalog.register(INSTANCE, source)

    async def opener() -> Any:
        return fleet.backend

    async def sync_store() -> InMemorySourceSyncStore:
        return store

    service = ConnectedDataService(
        catalog,
        agent_did=agent or fleet.did,
        sync_store_opener=sync_store,
        ingest_factory=lambda _: _Ingest(),
        limits=SyncLimits(retries=0),
        global_concurrency=1,
        interval_seconds=0.01,
        health=StoreHealthReporter(opener),
    )
    await service.start()
    return service


async def _until(check: Callable[[], Awaitable[bool]], *, seconds: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.005)
    return False


async def _record(fleet: Fleet) -> arcagent.ConnectionRecord:
    record = await arcagent.ConnectionStateStore(fleet.backend).get(INSTANCE)
    assert record is not None
    return record


async def _after_install(fleet: Fleet) -> None:
    """Install through the real route, in a worker thread (the TestClient is synchronous)."""
    await asyncio.to_thread(fleet.install)


async def test_invalid_grant_needs_you_one_notice_restart_still_needs_you(fleet: Fleet) -> None:
    """J1 G5: a dead credential escalates once and a restart does not forget or repeat it."""
    await _after_install(fleet)
    wire, channel, clock = _Wire(), _Channel(), _Clock()
    wire.mode = "invalid_grant"
    store = InMemorySourceSyncStore()

    # One sync tick finds the credential dead.
    source = _Source(dead=True)
    service = await _service(fleet, source, store)
    try:

        async def needs_you() -> bool:
            return (await _record(fleet)).status == "needs_you"

        assert await _until(needs_you), "the dead credential never reached the health record"
    finally:
        await service.close()

    (card,) = await asyncio.to_thread(fleet.listing)
    assert (card["status"], card["action"]) == ("needs_you", "reconnect")
    assert card["action_label"].startswith("Reconnect")

    # The monitor tells the operator, once.
    monitor = _monitor(fleet, wire, channel, clock)
    await monitor.tick()
    assert len(channel.messages) == 1
    assert channel.messages[0].startswith(f"Connection '{INSTANCE}' needs you:")
    probes_before_restart = wire.probes
    attempts_before_restart = source.sync_attempts

    # Restart: new app, new service, new monitor, same arcstore and filesystem.
    await asyncio.to_thread(fleet.restart)
    restarted = _monitor(fleet, wire, channel, clock)
    for _ in range(3):
        clock.now += timedelta(seconds=20)
        await restarted.tick()
    reborn = _Source(dead=True)
    service = await _service(fleet, reborn, store)
    try:
        await asyncio.sleep(0.2)
    finally:
        await service.close()

    record = await _record(fleet)
    assert record.status == "needs_you"
    assert len(channel.messages) == 1, "a restart must not page the operator again"
    assert wire.probes - probes_before_restart <= 1, "a dead credential was re-probed in a loop"
    assert reborn.sync_attempts == 0, "a restarted sync hammered the dead credential"
    assert attempts_before_restart == 1
    (card,) = await asyncio.to_thread(fleet.listing)
    assert card["status"] == "needs_you"


async def test_history_404_resets_cursor_and_connection_stays_healthy(fleet: Fleet) -> None:
    await _after_install(fleet)
    store = InMemorySourceSyncStore()
    lease = await store.acquire_lease(fleet.did, INSTANCE, "earlier", ttl_seconds=60)
    assert lease is not None
    assert await store.commit_page(
        fleet.did,
        INSTANCE,
        expected_cursor=None,
        next_cursor="stale-history",
        page_id="seed",
        page_count=1,
        page_bytes=1,
        owner_id="earlier",
        fencing_token=lease.fencing_token,
    )
    await store.release_lease(
        fleet.did, INSTANCE, owner_id="earlier", fencing_token=lease.fencing_token
    )
    before = await _record(fleet)
    source = _Source(stale="stale-history")
    service = await _service(fleet, source, store)
    try:

        async def reset_and_completed() -> bool:
            return source.checkpoints[:2] == ["stale-history", None] and (
                (await store.get_state(fleet.did, INSTANCE)).status.value == "complete"
            )

        assert await _until(reset_and_completed), f"cursor was not reset: {source.checkpoints}"
        await asyncio.sleep(0.05)
    finally:
        await service.close()

    after = await _record(fleet)
    assert after.status == "healthy"
    assert after.transition_seq == before.transition_seq, "an aged-out cursor is not an outage"
    assert after.notice_seq == 0


async def test_unknown_error_three_times_is_error_then_recovers_with_one_note(
    fleet: Fleet,
) -> None:
    await _after_install(fleet)
    wire, channel, clock = _Wire(), _Channel(), _Clock()
    monitor = _monitor(fleet, wire, channel, clock)
    store = arcagent.ConnectionStateStore(fleet.backend)
    # Probes here are driven by hand so the minutes are exact; the loop only delivers.
    await store.schedule_check(INSTANCE, "2099-01-01T00:00:00+00:00", actor_did="did:arc:test")

    def connections() -> arcagent.Connections:
        return arcagent.Connections.for_deployment(
            audit=arcagent.AuditChain.held(fleet.sink),
            state_opener=_opener(fleet),
            attachment_factory=lambda _m, _b, _s, **_kw: _Attachment(wire),
            clock=clock,
        )

    wire.mode = "unavailable"
    for minutes in (0, 5, 11):
        clock.now = T0 + timedelta(minutes=minutes)
        await connections().check_health(INSTANCE, checked_by=arcagent.PROBE_DID)
        if minutes < 11:
            assert (await _record(fleet)).status == "healthy", "a blip is not an outage yet"
    assert (await _record(fleet)).status == "error"
    await monitor.tick()
    assert len(channel.messages) == 1
    assert "is failing" in channel.messages[0]

    wire.mode = "ok"
    clock.now = T0 + timedelta(minutes=12)
    await connections().check_health(INSTANCE, checked_by=arcagent.PROBE_DID)
    assert (await _record(fleet)).status == "healthy"
    await monitor.tick()
    await monitor.tick()
    assert [m for m in channel.messages if "working again" in m] == [
        f"Connection '{INSTANCE}' is working again."
    ]
    assert len(channel.messages) == 2


def _opener(fleet: Fleet) -> Callable[[], Awaitable[Any]]:
    async def open_backend() -> Any:
        return fleet.backend

    return open_backend


async def test_page_view_never_touches_a_secret(fleet: Fleet) -> None:
    """J1 G13: ten card loads, zero credential reads."""
    await _after_install(fleet)
    fleet.sink.events.clear()

    for _ in range(10):
        await asyncio.to_thread(fleet.listing)
        response = await asyncio.to_thread(
            fleet.client.get, "/api/agents/acme/connectors", headers=fleet.headers("viewer")
        )
        assert response.status_code == 200

    assert [e for e in fleet.sink.events if e.action == "secret.read"] == []
