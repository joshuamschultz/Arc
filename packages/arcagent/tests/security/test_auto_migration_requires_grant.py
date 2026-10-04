"""Battery (P18-4): the automatic move into a shared store needs the agent's own approval.

An agent's own copy joins a connection's shared store only while that agent holds
an approved mapping of exactly the shareable home. Without one (never approved,
approval revoked, or a mapping that also feeds the agent's private memory) the
automatic move changes nothing, writes nothing into the store, and leaves no
audit event claiming a move.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connected_data.shared import SharedKnowledge
from arcagent.modules.connected_data.sync_worker import RemoteIngestPort

_DID = "did:arc:test:agent"


class _OwnCopy(RemoteIngestPort):
    """An agent's own store holding documents, with whatever approval the test gives it."""

    def __init__(self, homes: tuple[KnowledgeHome, ...] | None) -> None:  # no real store
        self.homes = homes

    async def approved_mapping(self, source: SourceDescription) -> MappingPlan | None:
        del source
        if self.homes is None:
            return None
        return MappingPlan(
            mapping_id="approval-1", homes=self.homes, revision="r", content_hash="h"
        )

    async def documents_indexed(self, source: SourceDescription) -> int:
        del source
        return 5

    async def source_generation(self, source: SourceDescription) -> int:
        del source
        return 1


def _source() -> SourceDescription:
    return SourceDescription(connection_id="wiki", source_kind="confluence", account_id="acct")


@pytest.mark.parametrize(
    "homes",
    [None, (KnowledgeHome.MEMORY,), (KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)],
    ids=["no-approved-grant", "memory-only", "document-plus-private-memory"],
)
@pytest.mark.asyncio
async def test_an_agent_without_an_approved_shareable_grant_is_never_auto_migrated(
    tmp_path: Path,
    homes: tuple[KnowledgeHome, ...] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    backend = FakeBackend()
    events: list[str] = []
    writers: list[str] = []

    async def opener() -> FakeBackend:
        return backend

    shared = SharedKnowledge(
        agent_did=_DID,
        arcstore_opener=opener,
        embedder=lambda: None,
        profile=lambda: "lexical",
        embed=lambda: None,
    )

    async def spying_writer(connection_id: str, approval_id: str) -> Any:
        writers.append(connection_id)
        raise AssertionError("an ungranted agent reached the shared store's writer")

    shared.migration_writer = spying_writer  # type: ignore[method-assign]  # reason: spy

    def audit(action: str, payload: dict[str, Any]) -> None:
        del payload
        events.append(action)

    service = ConnectedDataService(
        SimpleNamespace(snapshot=None),  # type: ignore[arg-type]  # reason: unused here
        agent_did=_DID,
        sync_store_opener=None,
        ingest_factory=lambda description: _OwnCopy(homes),
        limits=SyncLimits(),
        global_concurrency=1,
        audit=audit,
        shared=shared,
    )
    service._store = InMemorySourceSyncStore()
    registration = SimpleNamespace(connection_id="wiki", adapter=None)
    service._descriptions["wiki"] = _source()

    await service._migrate_automatically(registration)  # type: ignore[arg-type]

    assert writers == []
    assert events == [], "a move that did not happen was audited as one"
    assert not shared.root("wiki").exists(), "the shared store was created for an ungranted agent"
