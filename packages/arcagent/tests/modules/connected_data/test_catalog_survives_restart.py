"""J1 F2 / F3 -- the connections catalog survives a restart and never calls a provider.

F2: the staged mapping is restored from the durable store as plain strings, and
both the ``connected_sources`` tool and the prompt hook called ``.value`` on them:
``AttributeError: 'str' object has no attribute 'value'`` on 8 of 16 DGX calls, and
the module bus fails open, so the "connections" prompt section silently vanished.

F3: the prompt hook ran ``get_mapping_proposal``, which calls ``adapter.inspect_source``
-- Jira runs ``acli`` there -- on every turn of every granted agent.

These drive the real service, tool and hook. Only the provider and the durable
mapping row are doubles; the row is written the way ``stage_mapping`` writes it
(plain strings), which is the thing a restart reads back.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data.capabilities import inject_connections_catalog
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.memory.capabilities import connected_sources

_DID = "did:agent"


class _Provider:
    """A provider that counts every question it is asked and can start refusing."""

    def __init__(self) -> None:
        self.calls = 0
        self.refusing = False

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        self.calls += 1
        if self.refusing:
            raise RuntimeError("acli hung and was killed")
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="jira",
            account_id="jira",
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Jira",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.calls += 1
        return SyncSourcePage(next_checkpoint="end")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise AssertionError("nothing to fetch")

    async def list_source_resources(self, request: Any) -> tuple[Any, ...]:
        return ()

    async def select_source_resources(self, request: Any) -> None:
        return None

    async def close_source(self) -> None:
        return None


class _Ingest:
    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="m", homes=(KnowledgeHome.DOCUMENT,), revision="r", content_hash="h"
        )

    async def ingest(self, *args: Any) -> None:
        return None

    async def complete_snapshot(self, *args: Any) -> None:
        return None

    async def reset_source(self, source: Any) -> None:
        return None

    async def purge_source(self, source: Any) -> None:
        return None


class _MappingRows:
    """The durable row exactly as ``stage_mapping`` stored it: JSON-plain strings."""

    def __init__(self) -> None:
        self.rows = {
            "jira": {
                "source_id": "source-jira",
                "allowed_homes": ["memory", "document"],
                "homes": ["document"],
                "approval_id": "approval-1",
            }
        }

    async def get(self, connection_id: str) -> dict[str, Any] | None:
        return self.rows.get(connection_id)

    async def put(self, connection_id: str, proposal: dict[str, Any]) -> None:
        self.rows[connection_id] = proposal

    async def delete(self, connection_id: str) -> None:
        self.rows.pop(connection_id, None)


async def _service(provider: _Provider) -> ConnectedDataService:
    """A service as a freshly restarted process builds it: empty memory, durable rows."""
    catalog = SourceCatalog()
    await catalog.register("jira", provider)
    store, rows = InMemorySourceSyncStore(), _MappingRows()

    async def open_store() -> InMemorySourceSyncStore:
        return store

    async def open_rows() -> _MappingRows:
        return rows

    service = ConnectedDataService(
        catalog,
        agent_did=_DID,
        sync_store_opener=open_store,
        mapping_proposal_store_opener=open_rows,
        ingest_factory=lambda _: _Ingest(),
        limits=SyncLimits(retries=0),
        global_concurrency=1,
        interval_seconds=3600,
    )
    await service.start()
    return service


async def _described(service: ConnectedDataService) -> None:
    for _ in range(400):
        if any(status.description is not None for status in await service.list_sources()):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the source was never described")


@pytest.fixture
def runtime() -> Any:
    _runtime.configure(agent_did=_DID)
    yield _runtime.state()
    _runtime.reset()


async def test_connected_sources_works_after_a_restart(runtime: Any) -> None:
    service = await _service(_Provider())
    runtime.service = service
    try:
        await _described(service)
        answer = await connected_sources()
    finally:
        await service.close()

    assert "Error" not in answer
    assert "Jira" in answer and "homes=document" in answer


async def test_the_prompt_catalog_survives_a_restart(runtime: Any) -> None:
    service = await _service(_Provider())
    runtime.service = service
    try:
        await _described(service)
        sections: dict[str, str] = {}
        await inject_connections_catalog(SimpleNamespace(data={"sections": sections}))
    finally:
        await service.close()

    assert "Jira" in sections["connections"]
    assert "homes=document" in sections["connections"]


async def test_a_staged_mapping_row_nobody_can_read_is_not_mapped_rather_than_a_crash(
    runtime: Any,
) -> None:
    provider = _Provider()
    service = await _service(provider)
    runtime.service = service
    try:
        service._mapping_store.rows["jira"]["homes"] = ["not-a-home"]  # type: ignore[union-attr]
        await _described(service)
        entries = await service.catalog_entries()
    finally:
        await service.close()

    assert entries[0].homes_text == "not mapped"


async def test_assembling_the_prompt_never_asks_the_provider_anything(runtime: Any) -> None:
    """J1 gate G4: an adapter that refuses to answer must not slow a single turn."""
    provider = _Provider()
    service = await _service(provider)
    runtime.service = service
    try:
        await _described(service)
        await asyncio.sleep(0.05)  # let the startup sync settle
        provider.refusing = True
        provider.calls = 0
        started = time.perf_counter()
        for _ in range(10):
            sections: dict[str, str] = {}
            await inject_connections_catalog(SimpleNamespace(data={"sections": sections}))
            assert "Jira" in sections["connections"]
        elapsed = time.perf_counter() - started
    finally:
        await service.close()

    assert provider.calls == 0, f"the prompt hook called the provider {provider.calls} times"
    assert elapsed < 0.5
