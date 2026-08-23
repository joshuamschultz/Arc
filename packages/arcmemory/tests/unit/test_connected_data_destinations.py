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
    SourceContent,
    SourceMappingDeniedError,
)
from arcmemory.profile import ProfileFactKind, ReviewStatus
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import MemoryHome, Scope

_DID = "did:arc:destination-test"


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
    source = ConnectedSource(connection_id="mail", account_id="inbox", source_kind="email")
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


async def test_blob_route_builds_ontology_and_document_route_is_searchable(
    workspace: Path,
) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(connection_id="s3", account_id="bucket", source_kind="blob")
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


async def test_delete_removes_all_destination_state_including_memory(workspace: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(connection_id="mail", account_id="inbox", source_kind="email")
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
    source = ConnectedSource(connection_id="db", account_id="erp", source_kind="database")

    with pytest.raises(SourceMappingDeniedError):
        await service.propose_mapping(source, ("document",))


async def test_profile_destination_stages_provenance_fact_for_review(workspace: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval)
    source = ConnectedSource(connection_id="crm", account_id="workspace", source_kind="profile")
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
