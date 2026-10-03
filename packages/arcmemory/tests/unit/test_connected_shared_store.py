"""A connection-scoped document store: one writer, authorized by a subscriber (P18-4).

Several agents granted one connection share one store. No agent owns it, so no
agent's approval row names it; a write is authorized by delegating to the
verified approval of the agent whose run is writing. Without one, nothing lands.
Existing per-agent stores are adopted into the shared one from their extracted
documents, so a migration re-keys what was already fetched instead of fetching
it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ApprovedMapping,
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingDeniedError,
    SourceMappingPendingError,
)
from arcmemory.types import MemoryHome

_PRINCIPAL = "did:arc:knowledge:wiki"
_CONFIG = MemoryConfig(doc_chunk_tokens=32)


class _Authority:
    """The verified approval a writing subscriber delegates; ``None`` once withdrawn."""

    def __init__(self, approval_id: str | None) -> None:
        self.approval_id = approval_id

    async def authorized_homes(self) -> tuple[str, tuple[MemoryHome, ...]] | None:
        if self.approval_id is None:
            return None
        return self.approval_id, (MemoryHome.DOCUMENT,)


def _source() -> ConnectedSource:
    return ConnectedSource(
        connection_id="wiki",
        account_id="wiki-account",
        source_kind="confluence",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


def _shared(root: Path, authority: _Authority) -> ConnectedDataService:
    return ConnectedDataService(
        root, _PRINCIPAL, approval_store=None, config=_CONFIG, authority=authority
    )


def _page(object_id: str, body: str) -> tuple[ConnectedObject, SourceContent]:
    return (
        ConnectedObject(
            object_id=object_id,
            locator=f"/pages/{object_id}.txt",
            version="1",
            media_type="text/plain",
            classification="unclassified",
            metadata={"title": object_id.title()},
        ),
        SourceContent(
            object_id=object_id, version="1", media_type="text/plain", content=body.encode()
        ),
    )


async def _ingest(service: ConnectedDataService, mapping: ApprovedMapping, *pages: str) -> None:
    for number, body in enumerate(pages):
        source_object, content = _page(f"page-{number}", body)
        await service.ingest(_source(), source_object, content, mapping)


@pytest.mark.asyncio
async def test_a_shared_store_writes_under_the_writers_delegated_approval(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared", _Authority("approval-a"))

    mapping = await shared.require_approved_mapping(_source())
    await _ingest(shared, mapping, "the billing portal ships in August")

    assert mapping.mapping_id == "approval-a"
    assert mapping.homes == [MemoryHome.DOCUMENT]
    hits = await shared.document_search("billing portal", _source())
    assert hits and "billing portal" in hits[0].text


@pytest.mark.asyncio
async def test_no_authorizing_subscriber_means_nothing_is_written(tmp_path: Path) -> None:
    authority = _Authority("approval-a")
    shared = _shared(tmp_path / "shared", authority)
    mapping = await shared.require_approved_mapping(_source())

    authority.approval_id = None

    with pytest.raises(SourceMappingDeniedError):
        await shared.require_approved_mapping(_source())
    with pytest.raises(SourceMappingDeniedError):
        await _ingest(shared, mapping, "written after the last approval was withdrawn")
    assert await shared.document_search("withdrawn", _source()) == []


@pytest.mark.asyncio
async def test_a_forged_mapping_id_is_refused(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared", _Authority("approval-a"))
    mapping = await shared.require_approved_mapping(_source())

    forged = mapping.model_copy(update={"mapping_id": "approval-of-someone-else"})

    with pytest.raises(SourceMappingDeniedError):
        await _ingest(shared, forged, "smuggled in under another approval")


@pytest.mark.asyncio
async def test_a_shared_store_is_never_staged_for_approval(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared", _Authority(None))

    with pytest.raises(SourceMappingDeniedError):
        await shared.propose_mapping(_source(), ("document",))


async def _agent_store(workspace: Path) -> tuple[ConnectedDataService, ApprovedMapping]:
    approvals = ApprovalStore(FakeBackend())
    agent = ConnectedDataService(
        workspace, "did:arc:agent-a", approval_store=approvals, config=_CONFIG
    )
    with pytest.raises(SourceMappingPendingError):
        await agent.require_approved_mapping(_source())
    pending = (await approvals.list())[0]
    await approvals.resolve(pending.id, status="approved", actor_did="did:arc:operator")
    return agent, await agent.require_approved_mapping(_source())


@pytest.mark.asyncio
async def test_adoption_rekeys_an_agents_documents_and_a_dry_run_writes_nothing(
    tmp_path: Path,
) -> None:
    agent, mapping = await _agent_store(tmp_path / "agent")
    await _ingest(agent, mapping, "the billing portal ships in August", "new hires get a buddy")
    shared = _shared(tmp_path / "shared", _Authority("approval-a"))

    preview = await shared.adopt_documents(_source(), agent, _source(), dry_run=True)

    assert (preview.documents, preview.adopted, preview.deduplicated) == (2, 2, 0)
    assert await shared.document_search("billing portal", _source()) == []
    assert not (tmp_path / "shared" / "memory" / "connected").exists()

    report = await shared.adopt_documents(_source(), agent, _source(), dry_run=False)

    assert (report.documents, report.adopted, report.deduplicated) == (2, 2, 0)
    hits = await shared.document_search("billing portal", _source())
    assert hits and "in August" in hits[0].text
    assert hits[0].title == "Page-0"
    assert hits[0].pointer.startswith("memory/connected/")
    inventory = await shared.list_documents(_source())
    assert sorted(doc.object_id for doc in inventory) == ["page-0", "page-1"]
    assert all(doc.version == "1" for doc in inventory)

    again = await shared.adopt_documents(_source(), agent, _source(), dry_run=False)
    assert (again.adopted, again.deduplicated) == (0, 2)


@pytest.mark.asyncio
async def test_an_adopted_object_is_not_fetched_again_by_the_next_sync(tmp_path: Path) -> None:
    """The shared store's object state carries the adopted version, so ingest no-ops."""
    agent, mapping = await _agent_store(tmp_path / "agent")
    await _ingest(agent, mapping, "the billing portal ships in August")
    shared = _shared(tmp_path / "shared", _Authority("approval-a"))
    await shared.adopt_documents(_source(), agent, _source(), dry_run=False)

    assert await shared.object_version(_source(), "page-0") == "1"


@pytest.mark.asyncio
async def test_a_reader_searches_and_lists_a_pool_by_its_id_below_its_clearance(
    tmp_path: Path,
) -> None:
    """A subscriber holds the pool id, not the provider's description; no read-up."""
    writer = _shared(tmp_path / "shared", _Authority("approval-a"))
    mapping = await writer.require_approved_mapping(_source())
    await _ingest(writer, mapping, "the billing portal ships in August")
    secret, content = _page("page-secret", "the billing portal secret launch code")
    await writer.ingest(
        _source(), secret.model_copy(update={"classification": "SECRET"}), content, mapping
    )
    reader = ConnectedDataService(
        tmp_path / "shared", _PRINCIPAL, approval_store=None, config=_CONFIG
    )

    hits = await reader.search_pool("billing portal", mapping.source_id)
    secret_hits = await reader.search_pool("billing portal", mapping.source_id, clearance="SECRET")
    listed = await reader.list_pool(mapping.source_id)

    assert [hit.classification.upper() for hit in hits] == ["UNCLASSIFIED"]
    assert len(secret_hits) == 2
    assert len(listed) == 2


@pytest.mark.asyncio
async def test_adoption_needs_an_authorizing_subscriber(tmp_path: Path) -> None:
    agent, mapping = await _agent_store(tmp_path / "agent")
    await _ingest(agent, mapping, "the billing portal ships in August")
    shared = _shared(tmp_path / "shared", _Authority(None))

    with pytest.raises(SourceMappingDeniedError):
        await shared.adopt_documents(_source(), agent, _source(), dry_run=False)
