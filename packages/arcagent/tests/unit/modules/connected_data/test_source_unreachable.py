"""A provider that will not answer is a typed refusal, not an escaping exception.

The background sync loop already degraded a source it could not read. The
operator-facing reads did not: `stage_mapping`, `get_mapping_proposal` and
`list_resources` let the adapter's own exception through, so an expired token or
a read timeout reached the browser as an opaque HTTP 500 on the mapping screen.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcagent.connected_data import SyncLimits
from arcagent.extension.source import SourceError, SourceFailureCode
from arcagent.modules.connected_data import SourceRefusedError, SourceUnreachableError
from arcagent.modules.connected_data.service import ConnectedDataService


class _DeadAdapter:
    """An adapter whose provider is unreachable, as a third-party one may be."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def inspect_source(self, _request: Any) -> Any:
        raise self._error

    async def list_source_resources(self, _request: Any) -> Any:
        raise self._error


class _Registration:
    def __init__(self, connection_id: str, adapter: Any) -> None:
        self.connection_id = connection_id
        self.adapter = adapter


class _Catalog:
    def __init__(self, registrations: tuple[Any, ...]) -> None:
        self._registrations = registrations

    async def snapshot(self) -> tuple[Any, ...]:
        return self._registrations


def _service(error: Exception) -> ConnectedDataService:
    registration = _Registration("personal_dropbox", _DeadAdapter(error))
    return ConnectedDataService(
        agent_did="did:arc:test:agent",
        catalog=_Catalog((registration,)),
        ingest_factory=lambda description: object(),
        sync_store_opener=None,
        resource_selection_store_opener=None,
        limits=SyncLimits(),
        global_concurrency=1,
    )


@pytest.mark.parametrize(
    "error",
    [RuntimeError("gh exited 1: error connecting to api.github.com"), OSError("read timeout")],
    ids=["cli-transport", "socket"],
)
@pytest.mark.asyncio
async def test_stage_mapping_refuses_typed_when_the_provider_is_unreachable(
    error: Exception,
) -> None:
    service = _service(error)

    with pytest.raises(SourceUnreachableError) as raised:
        await service.stage_mapping("personal_dropbox", homes=("document",))

    assert raised.value.connection_id == "personal_dropbox"


@pytest.mark.asyncio
async def test_get_mapping_proposal_refuses_typed_when_the_provider_is_unreachable() -> None:
    service = _service(RuntimeError("provider down"))

    with pytest.raises(SourceUnreachableError):
        await service.get_mapping_proposal("personal_dropbox")


@pytest.mark.asyncio
async def test_list_resources_refuses_typed_when_the_provider_is_unreachable() -> None:
    service = _service(RuntimeError("provider down"))

    with pytest.raises(SourceUnreachableError):
        await service.list_resources("personal_dropbox")


@pytest.mark.asyncio
async def test_the_typed_refusal_names_the_source_and_keeps_the_cause() -> None:
    cause = RuntimeError("error connecting to api.github.com")
    service = _service(cause)

    with pytest.raises(SourceUnreachableError) as raised:
        await service.list_resources("personal_dropbox")

    assert "personal_dropbox" in str(raised.value)
    assert raised.value.__cause__ is cause


class _Store:
    """A durable store, standing in for the arcstore-backed one."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get(self, connection_id: str) -> dict[str, Any] | None:
        return self.rows.get(connection_id)

    async def put(self, connection_id: str, proposal: dict[str, Any]) -> None:
        self.rows[connection_id] = dict(proposal)

    async def delete(self, connection_id: str) -> None:
        self.rows.pop(connection_id, None)


@pytest.mark.asyncio
async def test_a_staged_mapping_choice_outlives_the_process() -> None:
    """The operator picks homes, then waits days for a signed approval.

    Held only in memory that choice died with the process, and the card read
    ``not_staged`` again while the approval was still pending.
    """
    store = _Store()
    store.rows["personal_dropbox"] = {
        "source_id": "abc123",
        "allowed_homes": ["document"],
        "homes": ["document"],
        "approval_id": "approval-1",
    }
    service = ConnectedDataService(
        agent_did="did:arc:test:agent",
        catalog=_Catalog(()),
        ingest_factory=lambda description: object(),
        sync_store_opener=None,
        resource_selection_store_opener=None,
        mapping_proposal_store_opener=lambda: _ready(store),
        limits=SyncLimits(),
        global_concurrency=1,
    )
    await service.start()
    try:
        restored = await service._staged_proposal("personal_dropbox")
    finally:
        await service.close()

    assert restored is not None
    assert restored.homes == ("document",)
    assert restored.approval_id == "approval-1"


async def _ready(store: _Store) -> _Store:
    return store


@pytest.mark.asyncio
async def test_an_unstaged_source_stays_unstaged() -> None:
    service = ConnectedDataService(
        agent_did="did:arc:test:agent",
        catalog=_Catalog(()),
        ingest_factory=lambda description: object(),
        sync_store_opener=None,
        resource_selection_store_opener=None,
        mapping_proposal_store_opener=lambda: _ready(_Store()),
        limits=SyncLimits(),
        global_concurrency=1,
    )
    await service.start()
    try:
        assert await service._staged_proposal("personal_dropbox") is None
    finally:
        await service.close()


class _RefusingAdapter:
    """An adapter that understood the request and said no."""

    def __init__(self, error: SourceError) -> None:
        self._error = error

    async def inspect_source(self, _request: Any) -> Any:
        raise self._error

    async def list_source_resources(self, _request: Any) -> Any:
        raise self._error

    async def select_source_resources(self, _request: Any) -> None:
        raise self._error


def _refusing_service() -> ConnectedDataService:
    error = SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "select one folder")
    registration = _Registration("personal_dropbox", _RefusingAdapter(error))
    return ConnectedDataService(
        agent_did="did:arc:test:agent",
        catalog=_Catalog((registration,)),
        ingest_factory=lambda description: object(),
        sync_store_opener=None,
        resource_selection_store_opener=None,
        limits=SyncLimits(),
        global_concurrency=1,
    )


@pytest.mark.asyncio
async def test_a_refusal_keeps_the_adapters_own_words() -> None:
    """ "Select one folder" is the remedy; flattened to an outage it is lost."""
    service = _refusing_service()

    with pytest.raises(SourceRefusedError) as raised:
        await service.list_resources("personal_dropbox")

    assert raised.value.detail == "select one folder"
    assert raised.value.code == SourceFailureCode.UNSUPPORTED_CONTENT
    assert "select one folder" in str(raised.value)


@pytest.mark.asyncio
async def test_a_refusal_is_not_reported_as_unreachable() -> None:
    """Retrying a refusal changes nothing, so the two must not share a message."""
    service = _refusing_service()

    with pytest.raises(SourceRefusedError):
        await service.stage_mapping("personal_dropbox", homes=("document",))
    assert not issubclass(SourceRefusedError, SourceUnreachableError)


class _HangingAdapter:
    """A connector whose vendor binary never answers."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def inspect_source(self, _request: Any) -> Any:
        await self._gate.wait()
        raise AssertionError("unreachable in this test")


@pytest.mark.asyncio
async def test_one_hanging_connector_does_not_hold_the_listing() -> None:
    """The connections page must render whatever the slowest source is doing.

    Inspecting inline meant a vendor CLI that hangs rather than answering held
    the whole page for minutes, taking every healthy source down with it.
    """
    gate = asyncio.Event()
    registration = _Registration("jira", _HangingAdapter(gate))
    service = ConnectedDataService(
        agent_did="did:arc:test:agent",
        catalog=_Catalog((registration,)),
        ingest_factory=lambda description: object(),
        sync_store_opener=None,
        resource_selection_store_opener=None,
        limits=SyncLimits(),
        global_concurrency=1,
    )

    try:
        listed = await asyncio.wait_for(service.list_sources(), timeout=2.0)
    finally:
        gate.set()
        await service.close()

    assert [item.connection_id for item in listed] == ["jira"]
    assert listed[0].detail == "inspecting"


class _DescribingAdapter:
    """A healthy adapter that describes itself."""

    async def inspect_source(self, request: Any) -> Any:
        from arcagent.extension.source import SourceDescription

        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="test",
            account_id="account",
        )


@pytest.mark.asyncio
async def test_a_completed_source_still_reads_completed_after_a_restart() -> None:
    """The sync record is durable; the runtime status was not.

    Every restart wiped a finished source back to "awaiting_mapping" with no
    pages, no bytes and no source id — which also left its documents
    unaddressable, so searching them returned nothing.
    """
    from arcstore.source_sync import InMemorySourceSyncStore

    from arcagent.connected_data import SyncStatus

    class _Ingest:
        def canonical_source_id(self, description: Any) -> str:
            return "canonical-dropbox"

    store = InMemorySourceSyncStore()
    registration = _Registration("dropbox", _DescribingAdapter())
    service = ConnectedDataService(
        agent_did="did:arc:test:agent",
        catalog=_Catalog((registration,)),
        ingest_factory=lambda description: _Ingest(),
        sync_store_opener=lambda: _ready_store(store),
        resource_selection_store_opener=None,
        limits=SyncLimits(),
        global_concurrency=1,
    )
    await service.start()
    try:
        lease = await store.acquire_lease(
            "did:arc:test:agent", "canonical-dropbox", "worker", ttl_seconds=60
        )
        assert lease is not None
        await store.set_status(
            "did:arc:test:agent",
            "canonical-dropbox",
            SyncStatus.COMPLETE,
            owner_id="worker",
            fencing_token=lease.fencing_token,
        )

        await service._inspect_registration(registration)
        status = service._statuses["dropbox"]
    finally:
        await service.close()

    assert status.status == "complete"


async def _ready_store(store: Any) -> Any:
    return store
