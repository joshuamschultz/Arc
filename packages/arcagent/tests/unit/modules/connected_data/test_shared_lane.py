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
from arctrust.paths import connected_knowledge_dir

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.knowledge_subscriptions import KnowledgeSubscriptions
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connected_data.shared import SharedKnowledge
from packages.arcagent.tests.sync_worker_fakes import approve_document_mapping, serve_from

_DID = "did:arc:test:agent"
_OTHER = "did:arc:test:other"


@pytest.fixture(autouse=True)
def _fleet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))


async def _approved(backend: FakeBackend) -> FakeBackend:
    """Both agents' document mappings are approved; the worker re-reads them."""
    serve_from(backend, _DID, _OTHER)
    await approve_document_mapping(backend, _DID, approval_id="approval-1")
    await approve_document_mapping(backend, _OTHER, approval_id="approval-2")
    return backend


def _store_root(lane: Any) -> Path:
    return connected_knowledge_dir() / lane.principal.rsplit(":", 1)[1]


class _Private:
    """The agent's own port: an approved plan and how many documents it holds."""

    def __init__(
        self,
        homes: tuple[KnowledgeHome, ...] | None,
        documents: int = 0,
        approval_id: str = "approval-1",
    ) -> None:
        self.homes = homes
        self.documents = documents
        self.approval_id = approval_id

    async def approved_mapping(self, source: SourceDescription) -> MappingPlan | None:
        del source
        if self.homes is None:
            return None
        return MappingPlan(
            mapping_id=self.approval_id, homes=self.homes, revision="r", content_hash="h"
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
        embed=lambda: None,
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
    backend = await _approved(FakeBackend())
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
    backend = await _approved(FakeBackend())
    service = _service(tmp_path, backend, profile=profile)
    lane = await service._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    assert lane is not None
    store_root = _store_root(lane)
    assert store_root.is_dir()

    after = await service._lane_for("wiki", _source(), private)

    assert after is None
    assert await _subscribers(backend) == []
    assert service._sync_key("wiki") == _DID
    assert not store_root.exists(), "a store nobody reads was left behind"


@pytest.mark.asyncio
async def test_a_store_still_read_by_another_agent_is_kept(tmp_path: Path) -> None:
    backend = await _approved(FakeBackend())
    service = _service(tmp_path, backend)
    other = _service(tmp_path, backend)
    other._agent_did = _OTHER
    other._shared.agent_did = _OTHER
    lane = await service._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    assert await other._lane_for(
        "wiki", _source(), _Private((KnowledgeHome.DOCUMENT,), approval_id="approval-2")
    )
    assert lane is not None

    await service._lane_for("wiki", _source(), _Private(None))

    assert await _subscribers(backend) == [_OTHER]
    assert _store_root(lane).is_dir()


@pytest.mark.asyncio
async def test_an_agent_that_embeds_differently_keeps_its_own_store(tmp_path: Path) -> None:
    backend = await _approved(FakeBackend())
    first = _service(tmp_path, backend, profile="model-a")
    assert await first._lane_for("wiki", _source(), _Private((KnowledgeHome.DOCUMENT,)))
    second = _service(tmp_path, backend, profile="model-b")
    second._agent_did = _OTHER
    second._shared.agent_did = _OTHER
    own = _Private((KnowledgeHome.DOCUMENT,), approval_id="approval-2")

    assert await second._lane_for("wiki", _source(), own) is None
    assert await _subscribers(backend) == [_DID]


@pytest.mark.parametrize(
    ("private", "expected"),
    [
        (_Private((KnowledgeHome.DOCUMENT,)), "shared"),
        (_Private((KnowledgeHome.DOCUMENT,), documents=3), "migrating"),
        (_Private((KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)), "own"),
    ],
    ids=["shared", "own-copy-waiting-to-move", "memory-stays-own"],
)
@pytest.mark.asyncio
async def test_each_connection_names_the_store_it_reads_from(
    tmp_path: Path, private: _Private, expected: str
) -> None:
    """J-K3: the card shows own copy, waiting to move, or shared, per connection."""
    service = _service(tmp_path, await _approved(FakeBackend()))
    assert service.lane("wiki") == "own", "nothing decided yet reads its own store"

    await service._lane_for("wiki", _source(), private)

    assert service.lane("wiki") == expected
