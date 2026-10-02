"""The sync run's end reports to the shared connection-health record (P18-1, step 6).

A real :class:`ConnectedDataService`, a real coordinator, a real in-memory sync
store and a real in-memory arcstore holding the ``connections`` record. Only the
provider is a double. The service holds no notifier: it reports what it found, and
the health authority decides what that means and who is told.
"""

from __future__ import annotations

import asyncio
from typing import Any

from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.connection_health import (
    ConnectionHealthAuthority,
    HealthSignal,
    PendingNotice,
    StoreHealthReporter,
)
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
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore
from arcagent.modules.connected_data.service import ConnectedDataService

_ACTOR = "did:arc:test:operator"


class _Source:
    """A mail source: the first ``fail_first`` syncs are a revoked credential."""

    def __init__(self, *, fail_first: int = 1_000_000, barrier: asyncio.Barrier | None = None):
        self.sync_attempts = 0
        self._fail_first = fail_first
        self._barrier = barrier

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="gmail",
            account_id="mailbox",
            data_shape=SourceDataShape.MAIL,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.sync_attempts += 1
        if self._barrier is not None:
            await self._barrier.wait()
        if self.sync_attempts <= self._fail_first:
            raise SourceError(
                SourceFailureCode.AUTH_REQUIRED,
                'oauth2: "invalid_grant" Token expired or revoked.',
            )
        return SyncSourcePage(objects=(), next_checkpoint="c1")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise AssertionError("no content in these tests")

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


async def _ready(value: InMemorySourceSyncStore) -> InMemorySourceSyncStore:
    return value


async def _seed(backend: FakeBackend, *names: str) -> None:
    store = ConnectionStateStore(backend)
    for name in names:
        await store.create(ConnectionRecord(connection=name, status="healthy"), actor_did=_ACTOR)


async def _service(
    backend: FakeBackend, source: _Source, *, source_id: str = "mail", agent: str = "did:agent"
) -> ConnectedDataService:
    catalog = SourceCatalog()
    await catalog.register(source_id, source)

    async def opener() -> FakeBackend:
        return backend

    service = ConnectedDataService(
        catalog,
        agent_did=agent,
        sync_store_opener=lambda: _ready(InMemorySourceSyncStore()),
        ingest_factory=lambda _: _Ingest(),
        limits=SyncLimits(retries=0),
        global_concurrency=1,
        interval_seconds=0.01,
        health=StoreHealthReporter(opener),
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


async def _record(backend: FakeBackend, name: str = "mail") -> ConnectionRecord | None:
    return await ConnectionStateStore(backend).get(name)


async def test_sync_failure_writes_shared_record_not_memory() -> None:
    backend = FakeBackend()
    await _seed(backend, "mail")
    service = await _service(backend, _Source())
    try:

        async def needs_you() -> bool:
            record = await _record(backend)
            return record is not None and record.status == "needs_you"

        assert await _until(needs_you), "the dead credential never reached the shared record"
    finally:
        await service.close()

    record = await _record(backend)
    assert record is not None
    assert (record.action, record.reason_code) == ("reconnect", "auth_required")
    assert record.checked_by == "did:agent"
    assert not hasattr(service, "_operator_notifier"), "the service must not notify anyone"


async def test_multi_source_suffix_reports_the_instance() -> None:
    backend = FakeBackend()
    await _seed(backend, "mail")
    service = await _service(backend, _Source(), source_id="mail:inbox")
    try:

        async def needs_you() -> bool:
            record = await _record(backend)
            return record is not None and record.status == "needs_you"

        assert await _until(needs_you)
    finally:
        await service.close()

    assert await _record(backend, "mail:inbox") is None
    assert set(await ConnectionStateStore(backend).statuses()) == {"mail"}


async def test_reconnect_clears_agent_backoff_without_restart() -> None:
    backend = FakeBackend()
    await _seed(backend, "mail")
    source = _Source(fail_first=1)
    service = await _service(backend, source)
    try:

        async def backed_off() -> bool:
            record = await _record(backend)
            return record is not None and record.status == "needs_you"

        assert await _until(backed_off)
        await asyncio.sleep(0.1)
        assert source.sync_attempts == 1, "a dead credential was re-synced while backed off"

        await ConnectionHealthAuthority(ConnectionStateStore(backend)).record(
            "mail", HealthSignal(ok=True, source="operator", checked_by=_ACTOR)
        )

        async def resumed() -> bool:
            return source.sync_attempts >= 2

        assert await _until(resumed), "the operator reconnected; the agent kept waiting"
    finally:
        await service.close()


async def test_five_agents_failing_send_one_notice() -> None:
    backend = FakeBackend()
    await _seed(backend, "mail")
    rendezvous = asyncio.Barrier(5)
    services = []
    for index in range(5):
        source = _Source(barrier=rendezvous)
        services.append(await _service(backend, source, agent=f"did:arc:agent:{index}"))
    try:

        async def all_reported() -> bool:
            record = await _record(backend)
            return record is not None and record.consecutive_failures >= 5

        assert await _until(all_reported), "not every agent reported its failure"
    finally:
        for service in services:
            await service.close()

    record = await _record(backend)
    assert record is not None
    assert (record.status, record.transition_seq, record.notice_seq) == ("needs_you", 1, 1)
    sent: list[str] = []

    async def deliver(pending: PendingNotice, text: str) -> str | None:
        sent.append(pending.idempotency_key)
        return "telegram"

    from datetime import UTC, datetime

    authority = ConnectionHealthAuthority(ConnectionStateStore(backend))
    await authority.dispatch_notices(deliver, owner="monitor", now=datetime.now(UTC))
    await authority.dispatch_notices(deliver, owner="monitor", now=datetime.now(UTC))

    assert sent == ["connection-health:mail:1"]
