"""Which store an agent reads a connection from, and what happens when that changes (P18-4).

An agent reads a connection's shared store only while its own approved mapping
is the shareable DOCUMENT home, its own copy has been migrated and it embeds the
way the store was embedded. When any of that stops being true it leaves, and a
store that nobody reads any more is purged rather than left on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.knowledge_subscriptions import KnowledgeSubscriptions
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connected_data.shared import SharedKnowledge

_DID = "did:arc:test:agent"


class _Private:
    """The agent's own port: an approved plan and how many documents it holds."""

    def __init__(self, homes: tuple[KnowledgeHome, ...] | None, documents: int = 0) -> None:
        self.homes = homes
        self.documents = documents

    async def approved_mapping(self, source: SourceDescription) -> MappingPlan | None:
        del source
        if self.homes is None:
            return None
        return MappingPlan(
            mapping_id="approval-1", homes=self.homes, revision="r", content_hash="h"
        )

    async def documents_indexed(self, source: SourceDescription) -> int:
        del source
        return self.documents


class _Catalog:
    async def snapshot(self) -> tuple[Any, ...]:
        return ()


def _source() -> SourceDescription:
    return SourceDescription(connection_id="wiki", source_kind="confluence", account_id="acct")


def _service(tmp_path: Path, backend: FakeBackend, *, profile: str = "lexical") -> Any:
    async def opener() -> FakeBackend:
        return backend

    shared = SharedKnowledge(
        agent_did=_DID,
        arcstore_opener=opener,
        embedder=lambda: None,
        profile=lambda: profile,
        root=lambda: tmp_path,
    )
    service = ConnectedDataService(
        _Catalog(),  # type: ignore[arg-type]  # reason: snapshot is all a lane decision reads
        agent_did=_DID,
        sync_store_opener=None,
        ingest_factory=None,
        limits=SyncLimits(),
        global_concurrency=1,
        shared=shared,
    )
    service._store = InMemorySourceSyncStore()
    return service


async def _subscribers(backend: FakeBackend) -> list[str]:
    rows = await KnowledgeSubscriptions(backend, actor_did=_DID).for_connection("wiki")
    return [row.agent_did for row in rows]


@pytest.mark.asyncio
async def test_an_approved_document_mapping_subscribes(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)

    lane = await service._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))

    assert lane is not None and lane.approval_id == "approval-1"
    assert await _subscribers(backend) == [_DID]
    assert service._sync_key("wiki") == lane.principal != _DID


@pytest.mark.parametrize(
    ("private", "profile"),
    [
        (_Private(None), "lexical"),
        (_Private((KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)), "lexical"),
        (_Private((KnowledgeHome.DOCUMENT,), documents=3), "lexical"),
    ],
    ids=["approval-gone", "memory-is-the-agents-own", "own-copy-not-migrated"],
)
@pytest.mark.asyncio
async def test_an_agent_that_can_no_longer_share_leaves_and_its_unread_store_goes(
    tmp_path: Path, private: _Private, profile: str
) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend, profile=profile)
    lane = await service._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    assert lane is not None
    store_root = tmp_path / lane.principal.rsplit(":", 1)[1]
    assert store_root.is_dir()

    after = await service._lane_for("wiki", _source(), private)

    assert after is None
    assert await _subscribers(backend) == []
    assert service._sync_key("wiki") == _DID
    assert not store_root.exists(), "a store nobody reads was left behind"


@pytest.mark.asyncio
async def test_a_store_still_read_by_another_agent_is_kept(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)
    other = _service(tmp_path, backend)
    other._agent_did = "did:arc:test:other"
    other._shared.agent_did = "did:arc:test:other"
    lane = await service._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    assert await other._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    assert lane is not None

    await service._lane_for("wiki", _source(), _Private(None))

    assert await _subscribers(backend) == ["did:arc:test:other"]
    assert (tmp_path / lane.principal.rsplit(":", 1)[1]).is_dir()


@pytest.mark.asyncio
async def test_an_agent_that_embeds_differently_keeps_its_own_store(tmp_path: Path) -> None:
    backend = FakeBackend()
    first = _service(tmp_path, backend, profile="model-a")
    assert await first._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    second = _service(tmp_path, backend, profile="model-b")
    second._agent_did = "did:arc:test:other"

    assert await second._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,))) is None
    assert await _subscribers(backend) == [_DID]
