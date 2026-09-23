"""A sync run releases the SQLite connection it opened.

The ingest port is built per run, and each build opened its own connection to
the agent's ``index.db`` that nothing ever closed: one leaked connection (and
one pinned WAL reader) per run, forever. The service now releases the port when
a run ends, and on revoke, reindex and shutdown.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import arcmemory.db as memory_db
import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, SyncLimits
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter, ArcStoreObjectState
from arcagent.modules.connected_data.service import ConnectedDataService

_DID = "did:arc:lifecycle"


class Source:
    def __init__(self) -> None:
        self.version = 0

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="drive",
            account_id="account",
            data_shape=SourceDataShape.DOCUMENT,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.version += 1
        return SyncSourcePage(
            objects=(
                SourceObject(
                    object_id="notes.txt",
                    locator="/notes.txt",
                    kind=SourceObjectKind.FILE,
                    version=str(self.version),
                    media_type="text/plain",
                    metadata={"classification": "unclassified", "revision": self.version},
                ),
            ),
            next_checkpoint=f"c{self.version}",
            has_more=False,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=f"notes revision {request.version}".encode(),
        )

    async def close_source(self) -> None:
        return None


class Connections:
    """Every connection MemoryDB opens, and which of them are still open."""

    def __init__(self) -> None:
        self.opened: list[sqlite3.Connection] = []
        self._real = sqlite3.connect

    def connect(self, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = self._real(*args, **kwargs)
        self.opened.append(conn)
        return conn

    def still_open(self) -> int:
        count = 0
        for conn in self.opened:
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                continue
            count += 1
        return count


async def test_many_sync_runs_do_not_accumulate_open_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections = Connections()
    monkeypatch.setattr(memory_db.sqlite3, "connect", connections.connect)
    backend = FakeBackend()
    approval = ApprovalStore(backend)
    catalog = SourceCatalog()
    source = Source()
    await catalog.register("drive", source)
    store = InMemorySourceSyncStore()

    def factory(_: SourceDescription) -> ArcMemoryIngestAdapter:
        # A fresh port per use, exactly as the production factory builds one.
        return ArcMemoryIngestAdapter(
            tmp_path,
            _DID,
            approval_store=approval,
            object_state=ArcStoreObjectState(backend, actor_did=_DID),
        )

    async def open_store() -> InMemorySourceSyncStore:
        return store

    service = ConnectedDataService(
        catalog,
        agent_did=_DID,
        sync_store_opener=open_store,
        ingest_factory=factory,
        limits=SyncLimits(max_duty_fraction=1.0),
        global_concurrency=1,
        interval_seconds=3600,
    )
    await service.start()
    proposal = await service.stage_mapping("drive", homes=(KnowledgeHome.DOCUMENT,))
    assert proposal is not None
    await approval.resolve(proposal.approval_id, status="approved", actor_did="did:operator")

    for run in range(6):
        await _wait_until(lambda: "drive" not in service._tasks)
        await service.sync_now("drive")
        await _wait_until(lambda run=run: source.version > run)
        await _wait_until(lambda: "drive" not in service._tasks)
    statuses = await service.list_sources()
    runs_connections = len(connections.opened)
    open_between_runs = connections.still_open()
    await service.close()

    assert statuses[0].status == "complete", statuses
    assert runs_connections >= 6, "each run really used the database"
    assert open_between_runs == 0, f"{open_between_runs} connections left open by finished runs"
    assert connections.still_open() == 0


async def _wait_until(check: Any) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")
