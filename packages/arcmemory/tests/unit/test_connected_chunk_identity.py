"""Two connected sources may share an object id without losing each other's chunks.

The SQLite ``chunks`` table is keyed by ``chunk_id`` alone, and a connected
document's chunks were named ``<object_id>#<n>``. Two Dropbox accounts with the
same path (or two repos with a ``README.md``) produced the same id: the second
ingest took over the first source's row, and the first source's document
silently vanished from its own search. Chunk ids in a source's pool are now
qualified by the source (``<source_id>:<object_id>#<n>``, as pushed-record
ingest already names them), so neither can overwrite the other.
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
    SourceMappingPendingError,
)
from arcmemory.doc_index import doc_scope
from arcmemory.index.backend import SqliteIndexBackend

_DID = "did:arc:identity"


def _source(connection_id: str) -> ConnectedSource:
    return ConnectedSource(
        connection_id=connection_id,
        account_id=f"{connection_id}-account",
        source_kind="drive",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


async def _approve(
    service: ConnectedDataService, approval: ApprovalStore, source: ConnectedSource
) -> ApprovedMapping:
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = [row for row in await approval.list() if row.status == "pending"][-1]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    return await service.require_approved_mapping(source)


def _object(version: str, *, deleted: bool = False) -> ConnectedObject:
    return ConnectedObject(
        object_id="README.md",
        locator="/README.md",
        version=version,
        media_type="text/plain",
        classification="unclassified",
        revision=int(version),
        deleted=deleted,
    )


def _content(version: str, text: str) -> SourceContent:
    return SourceContent(
        object_id="README.md", version=version, media_type="text/plain", content=text.encode()
    )


async def _two_sources(
    workspace: Path,
) -> tuple[ConnectedDataService, dict[str, tuple[ConnectedSource, ApprovedMapping]]]:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval, config=MemoryConfig())
    sources = {}
    for name in ("alpha", "beta"):
        source = _source(name)
        sources[name] = (source, await _approve(service, approval, source))
    alpha, alpha_map = sources["alpha"]
    beta, beta_map = sources["beta"]
    await service.ingest(alpha, _object("1"), _content("1", "orchid launch plan"), alpha_map)
    await service.ingest(beta, _object("1"), _content("1", "tulip budget review"), beta_map)
    return service, sources


async def test_two_sources_with_the_same_object_id_keep_their_own_chunks(
    tmp_path: Path,
) -> None:
    service, sources = await _two_sources(tmp_path)
    alpha, _ = sources["alpha"]
    beta, _ = sources["beta"]

    alpha_hits = await service.document_search("orchid", alpha)
    beta_hits = await service.document_search("tulip", beta)

    assert alpha_hits and "orchid" in alpha_hits[0].text
    assert beta_hits and "tulip" in beta_hits[0].text
    # Search fuses recency, so a pool may answer with its own unrelated chunk —
    # but never with the other source's text.
    assert all("tulip" not in hit.text for hit in await service.document_search("tulip", alpha))
    assert all("orchid" not in hit.text for hit in await service.document_search("orchid", beta))


async def test_deleting_one_source_object_leaves_the_other(tmp_path: Path) -> None:
    service, sources = await _two_sources(tmp_path)
    alpha, alpha_map = sources["alpha"]
    beta, _ = sources["beta"]

    await service.ingest(alpha, _object("2", deleted=True), None, alpha_map)

    assert await service.document_search("orchid", alpha) == []
    beta_hits = await service.document_search("tulip", beta)
    assert beta_hits and "tulip" in beta_hits[0].text


async def test_a_pre_fix_chunk_is_replaced_not_duplicated_when_its_object_changes(
    tmp_path: Path,
) -> None:
    """Rows written before the fix keep working and are cleaned up on change."""
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(tmp_path, _DID, approval_store=approval, config=MemoryConfig())
    source = _source("alpha")
    mapping = await _approve(service, approval, source)
    await service.ingest(source, _object("1"), _content("1", "orchid launch plan"), mapping)
    scope = doc_scope(_DID, mapping.source_id).key
    backend = SqliteIndexBackend(service._db)
    # Stand in for a row the old code wrote: the unqualified object id.
    await backend.delete_object(scope, f"{mapping.source_id}:README.md")
    await backend.upsert_chunk(
        scope=scope,
        chunk_id="README.md#0",
        source_path="memory/connected/x.md",
        mtime=1.0,
        classification="unclassified",
        content_hash="legacy",
        text="orchid launch plan",
        embedding=None,
    )
    assert await service.document_search("orchid", source)

    await service.ingest(source, _object("2"), _content("2", "orchid plan revised"), mapping)

    ids = [chunk_id for chunk_id in await backend.recency_order(scope)]
    assert "README.md#0" not in ids
    hits = await service.document_search("orchid", source)
    assert [hit.text for hit in hits] == ["orchid plan revised"]
