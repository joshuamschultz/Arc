"""Destination routing and lifecycle regressions for connected sources."""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcmemory.connected_data import (
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingDeniedError,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.profile import ProfileFactKind, ReviewStatus
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import MemoryHome, Scope

_DID = "did:arc:destination-test"


@pytest.mark.parametrize(
    "source_kind", ["confluence", "github", "jira", "readwise_reader"]
)
def test_collaboration_sources_are_document_mappable(
    workspace: Path, source_kind: str
) -> None:
    service = ConnectedDataService(workspace, _DID, approval_store=None)
    source = ConnectedSource(
        connection_id=source_kind,
        account_id="account",
        source_kind=source_kind,
        data_shape=ConnectedSourceShape.DOCUMENT,
    )

    assert service.allowed_homes(source) == (MemoryHome.DOCUMENT,)


async def _approve(
    service: ConnectedDataService, source: ConnectedSource, homes: tuple[str, ...]
) -> object:
    approval = service._approval
    assert approval is not None
    await service.propose_mapping(source, homes)
    pending = (await approval.list())[-1]
    await approval.resolve(pending.id, status="approved", actor_did="did:arc:operator")
    return await service.require_approved_mapping(source)


async def test_exact_approved_subset_is_preserved_for_email_memory(
    workspace: Path,
) -> None:
    """An approval for memory-only must not be revalidated as memory+document."""
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="mail",
        account_id="inbox",
        source_kind="email",
        data_shape=ConnectedSourceShape.MAIL,
    )
    mapping = await _approve(service, source, ("memory",))

    assert mapping.homes == [MemoryHome.MEMORY]
    await service.ingest(
        source,
        ConnectedObject(
            object_id="message-1",
            locator="mail://message-1",
            version="1",
            media_type="text/plain",
            classification="unclassified",
        ),
        SourceContent(object_id="message-1", version="1", content=b"The launch date is Friday."),
        mapping,
    )

    scope = Scope(agent_did=_DID)
    assert EpisodicStore(service._db, workspace).count(scope.key) == 1
    assert not (workspace / "memory" / "connected").exists()
    await service.reset_source(source)
    assert EpisodicStore(service._db, workspace).count(scope.key) == 0


async def test_blob_route_builds_ontology_and_document_route_is_searchable(
    workspace: Path,
) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="s3",
        account_id="bucket",
        source_kind="blob",
        data_shape=ConnectedSourceShape.BLOB,
    )
    mapping = await _approve(service, source, ("blob", "document"))

    await service.ingest(
        source,
        ConnectedObject(
            object_id="reports/q1.txt",
            locator="reports/q1.txt",
            version="1",
            media_type="text/plain",
            classification="unclassified",
        ),
        SourceContent(
            object_id="reports/q1.txt", version="1", content=b"quarterly revenue was strong"
        ),
        mapping,
    )

    assert await service.document_search("quarterly revenue", source)
    docs = await service.list_documents(source)
    assert len(docs) == 1 and docs[0].object_id == "reports/q1.txt"
    assert service.blob_folders(source)
    assert (await service.delete_document(source, "reports/q1.txt")).name == "MISSING"
    assert (await service.document_status(source, "reports/q1.txt")).name == "MISSING"
    await service.purge_source(source)
    assert service.blob_folders(source) == []
    assert await service.document_search("quarterly revenue", source) == []


async def test_blob_inventory_reconciles_counts_and_tombstones_stale_folders(
    workspace: Path,
) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="s3",
        account_id="bucket",
        source_kind="blob",
        data_shape=ConnectedSourceShape.BLOB,
    )
    mapping = await _approve(service, source, ("blob",))
    for object_id in ("reports/a.txt", "reports/b.txt", "archive/c.txt"):
        await service.ingest(
            source,
            ConnectedObject(
                object_id=object_id,
                locator=object_id,
                version="1",
                media_type="text/plain",
                classification="unclassified",
                revision=1,
            ),
            SourceContent(object_id=object_id, version="1", content=b"inventory"),
            mapping,
        )
    reports = next(slug for slug in service.blob_folders(source) if "reports" in slug)
    store = SemanticStore(workspace, WeightedGraph(service._db), _DID)
    entity = store.read(reports)
    assert entity is not None
    assert {fact.predicate: fact.value for fact in entity.facts}["file_count"] == "2"

    await service.ingest(
        source,
        ConnectedObject(
            object_id="archive/c.txt",
            locator="archive/c.txt",
            version="2",
            media_type="text/plain",
            classification="unclassified",
            revision=2,
            deleted=True,
        ),
        None,
        mapping,
    )
    assert all("archive" not in slug for slug in service.blob_folders(source))


async def test_delete_removes_all_destination_state_including_memory(workspace: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="mail",
        account_id="inbox",
        source_kind="email",
        data_shape=ConnectedSourceShape.MAIL,
    )
    mapping = await _approve(service, source, ("memory", "document"))
    current = ConnectedObject(
        object_id="message-1",
        locator="mail://message-1",
        version="1",
        media_type="text/plain",
        classification="unclassified",
        revision=1,
    )
    await service.ingest(
        source,
        current,
        SourceContent(object_id="message-1", version="1", content=b"delete this launch note"),
        mapping,
    )
    await service.ingest(
        source,
        current.model_copy(update={"version": "2", "revision": 2, "deleted": True}),
        None,
        mapping,
    )

    assert EpisodicStore(service._db, workspace).count(Scope(agent_did=_DID).key) == 0
    assert await service.document_search("launch note", source) == []
    assert await service.list_documents(source) == []


async def test_invalid_mapping_home_is_refused_before_any_write(workspace: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="db",
        account_id="erp",
        source_kind="database",
        data_shape=ConnectedSourceShape.DATASTORE,
    )

    with pytest.raises(SourceMappingDeniedError):
        await service.propose_mapping(source, ("document",))


async def test_profile_destination_stages_provenance_fact_for_review(workspace: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="crm",
        account_id="workspace",
        source_kind="profile",
        data_shape=ConnectedSourceShape.PROFILE,
    )
    mapping = await _approve(service, source, ("profile",))

    await service.ingest(
        source,
        ConnectedObject(
            object_id="olivia-role",
            locator="crm://olivia/role",
            version="1",
            media_type="text/plain",
            classification="unclassified",
            metadata={"profile_id": "olivia", "profile_field": "role", "profile_kind": "static"},
        ),
        SourceContent(object_id="olivia-role", version="1", content=b"product designer"),
        mapping,
    )

    pending = await service.review_port.list(status=ReviewStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].kind is ProfileFactKind.STATIC
    assert pending[0].provenance.external_id == "olivia-role"
    assert (await service.review_port.context("olivia")).static == {}
    await service.review_port.approve(pending[0].fact_id)
    assert (await service.review_port.context("olivia")).static["role"] == "product designer"


async def test_profile_preflight_refuses_all_writes_when_metadata_is_invalid(
    workspace: Path,
) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="crm",
        account_id="workspace",
        source_kind="profile",
        data_shape=ConnectedSourceShape.PROFILE,
    )
    mapping = await _approve(service, source, ("document", "profile"))

    with pytest.raises(Exception, match="profile"):
        await service.ingest(
            source,
            ConnectedObject(
                object_id="bad-profile",
                locator="crm://bad-profile",
                version="1",
                media_type="text/plain",
                classification="unclassified",
            ),
            SourceContent(object_id="bad-profile", version="1", content=b"should not persist"),
            mapping,
        )

    assert await service.list_documents(source) == []
    assert EpisodicStore(service._db, workspace).count(Scope(agent_did=_DID).key) == 0


async def test_tombstone_revokes_profile_fact_and_document_status_is_missing(
    workspace: Path,
) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(
        connection_id="crm",
        account_id="workspace",
        source_kind="profile",
        data_shape=ConnectedSourceShape.PROFILE,
    )
    mapping = await _approve(service, source, ("profile",))
    current = ConnectedObject(
        object_id="olivia-role",
        locator="crm://olivia/role",
        version="1",
        media_type="text/plain",
        classification="unclassified",
        revision=1,
        metadata={"profile_id": "olivia", "profile_field": "role"},
    )
    await service.ingest(
        source,
        current,
        SourceContent(object_id="olivia-role", version="1", content=b"designer"),
        mapping,
    )
    fact = (await service.review_port.list(status=ReviewStatus.PENDING))[0]
    await service.review_port.approve(fact.fact_id)
    await service.ingest(
        source,
        current.model_copy(update={"version": "2", "revision": 2, "deleted": True}),
        None,
        mapping,
    )

    assert (await service.review_port.context("olivia")).inferred == {}
    assert (await service.review_port.get(fact.fact_id)).status.name == "UNDONE"
    assert (await service.document_status(source, "olivia-role")).name == "MISSING"
