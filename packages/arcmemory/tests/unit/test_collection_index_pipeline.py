"""H-026 VERIFY — the OKF collection-index pipeline, driven through real ingest.

These tests do not hand-build index rows. They drive the canonical connected-source
path (approve mapping -> ``ConnectedDataService.ingest`` / ``reindex_source``) and
assert the four properties H-026 requires of the per-source ``index.md``:

1. ingest writes a real, arcokf-VALID OKF ``index.md`` and indexes it as the
   ``index:<source_id>`` routing chunk;
2. every reindex refreshes it with no expiry/TTL gate that could skip the refresh;
3. it is hosted under the agent WORKSPACE, never the remote origin;
4. a reader fails CLOSED on a tampered index or a tampered listed document — the
   unverified artifact is never rendered.

Property 4 also covers the SURFACE seam ``MemoryOperator.read_collection_index``,
the one arcui consumes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcokf import validate_collection_index
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingPendingError,
)
from arcmemory.operator import CollectionIndexView, MemoryOperator

_AGENT_DID = "did:arc:agent"


def _source(*, account_id: str = "account") -> ConnectedSource:
    return ConnectedSource(
        connection_id="dropbox",
        account_id=account_id,
        source_kind="dropbox",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


def _service(workspace: Path, approval: ApprovalStore) -> ConnectedDataService:
    return ConnectedDataService(
        workspace,
        _AGENT_DID,
        approval_store=approval,
        config=MemoryConfig(doc_chunk_tokens=32),
    )


async def _granted(workspace: Path):  # type: ignore[no-untyped-def]
    """A service with one approved DOCUMENT mapping, plus the mapping + source id."""
    approval = ApprovalStore(FakeBackend())
    service = _service(workspace, approval)
    source = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)
    return service, source, mapping


def _obj(object_id: str, *, revision: int = 1) -> ConnectedObject:
    return ConnectedObject(
        object_id=object_id,
        locator=f"/reports/{object_id}.txt",
        version=str(revision),
        media_type="text/plain",
        classification="unclassified",
        revision=revision,
    )


def _content(object_id: str, text: str, *, revision: int = 1) -> SourceContent:
    return SourceContent(
        object_id=object_id,
        version=str(revision),
        media_type="text/plain",
        content=text.encode("utf-8"),
    )


# -- Property 1: writes a real, valid OKF index.md, indexed as index:<id> --------


@pytest.mark.asyncio
async def test_ingest_writes_valid_okf_collection_index(tmp_path: Path) -> None:
    service, source, mapping = await _granted(tmp_path)

    await service.ingest(source, _obj("q3"), _content("q3", "quarterly revenue report"), mapping)

    index_path = tmp_path / "memory" / "connected" / mapping.source_id / "index.md"
    assert index_path.is_file(), "ingest must render a real index.md on disk"

    # arcokf-valid, and valid against every listed document's digest.
    validation = validate_collection_index(index_path, index_path.parent)
    assert validation.valid, validation.error
    assert validation.entries, "the ingested document must appear in the inventory"

    # And it is indexed as the routing chunk a scoped search can surface.
    hits = await service.document_search("quarterly revenue", source, clearance="unclassified")
    assert any(h.chunk_id == f"index:{mapping.source_id}" for h in hits)


# -- Property 2: runs on EVERY reindex, no expiry/TTL gate -----------------------


@pytest.mark.asyncio
async def test_every_reindex_refreshes_index_with_no_expiry_gate(tmp_path: Path) -> None:
    service, source, mapping = await _granted(tmp_path)
    index_path = tmp_path / "memory" / "connected" / mapping.source_id / "index.md"

    await service.ingest(source, _obj("a"), _content("a", "alpha revenue notes"), mapping)
    first = validate_collection_index(index_path, index_path.parent)
    assert first.valid and len(first.entries) == 1

    # A second ingest refreshes the SAME index to two documents — unconditionally.
    await service.ingest(source, _obj("b"), _content("b", "beta revenue notes"), mapping)
    second = validate_collection_index(index_path, index_path.parent)
    assert second.valid and len(second.entries) == 2, "reindex must refresh, never skip"

    # A full source reindex regenerates a valid index every time (no TTL short-circuit).
    reindexed = await service.reindex_source(source)
    assert reindexed == 2
    after = validate_collection_index(index_path, index_path.parent)
    assert after.valid and len(after.entries) == 2


# -- Property 3: hosted under ~/arc workspace, never the remote store ------------


@pytest.mark.asyncio
async def test_index_is_hosted_under_workspace_never_remote(tmp_path: Path) -> None:
    service, source, mapping = await _granted(tmp_path)

    await service.ingest(source, _obj("q3"), _content("q3", "revenue report body"), mapping)

    doc_root = (tmp_path / "memory" / "connected" / mapping.source_id).resolve()
    index_path = doc_root / "index.md"
    # The index and every listed document resolve strictly under the workspace.
    assert index_path.resolve().is_relative_to(tmp_path.resolve())
    entries = validate_collection_index(index_path, doc_root).entries
    assert entries
    for entry in entries:
        target = (doc_root / entry.path).resolve()
        assert target.is_relative_to(tmp_path.resolve())
    # The remote origin path (the object locator) was never materialized on disk.
    assert not Path("/reports/q3.txt").exists()


# -- Property 4: fail-closed on a tampered index / tampered document -------------


def _operator(workspace: Path) -> MemoryOperator:
    return MemoryOperator(workspace, _AGENT_DID, config=MemoryConfig(doc_chunk_tokens=32))


@pytest.mark.asyncio
async def test_reader_returns_verified_view_on_a_clean_index(tmp_path: Path) -> None:
    service, source, mapping = await _granted(tmp_path)
    await service.ingest(source, _obj("q3"), _content("q3", "clean revenue body"), mapping)

    view = _operator(tmp_path).read_collection_index(mapping.source_id)

    assert isinstance(view, CollectionIndexView)
    assert view.present and view.verified
    assert view.document_count == 1 and view.entries
    assert view.markdown.startswith("# Collection Index")
    assert view.error is None


@pytest.mark.asyncio
async def test_reader_fails_closed_on_a_tampered_index(tmp_path: Path) -> None:
    service, source, mapping = await _granted(tmp_path)
    await service.ingest(source, _obj("q3"), _content("q3", "revenue body"), mapping)
    index_path = tmp_path / "memory" / "connected" / mapping.source_id / "index.md"

    # An operator hand-edits the reserved artifact (ASI06 memory poisoning).
    index_path.write_text(
        index_path.read_text(encoding="utf-8") + "\n- [evil](evil.md) — injected\n",
        encoding="utf-8",
    )

    view = _operator(tmp_path).read_collection_index(mapping.source_id)

    assert view.present and not view.verified, "a tampered index must not verify"
    assert view.markdown == "", "the unverified artifact must never be rendered"
    assert not view.entries
    assert view.error
    # The operator is told HOW to fix it, not left with a purely-technical banner.
    assert view.guidance and "re-sync" in view.guidance.lower()


@pytest.mark.asyncio
async def test_reader_fails_closed_on_a_tampered_listed_document(tmp_path: Path) -> None:
    service, source, mapping = await _granted(tmp_path)
    await service.ingest(source, _obj("q3"), _content("q3", "original revenue body"), mapping)
    doc_root = tmp_path / "memory" / "connected" / mapping.source_id

    # Swap the bytes of a listed document so its committed digest no longer matches.
    listed = next(p for p in doc_root.glob("*.md") if p.name != "index.md")
    listed.write_text(listed.read_text(encoding="utf-8") + "\ntampered line\n", encoding="utf-8")

    view = _operator(tmp_path).read_collection_index(mapping.source_id)

    assert view.present and not view.verified
    assert view.markdown == "" and not view.entries and view.error
    assert view.guidance and "re-sync" in view.guidance.lower()


# -- Self-heal: a re-sync restores a fail-closed index ---------------------------


@pytest.mark.asyncio
async def test_resync_restores_a_fail_closed_index(tmp_path: Path) -> None:
    """Fail-closed must be recoverable, not a dead end (the guidance's promise).

    Start verified -> a local out-of-band edit makes the reader fail closed ->
    the real re-sync path (the same ``sync_collection_index`` ingest uses)
    rebuilds the index from the on-disk documents -> the reader verifies again
    and renders the body.
    """
    service, source, mapping = await _granted(tmp_path)
    await service.ingest(source, _obj("q3"), _content("q3", "quarterly revenue body"), mapping)
    operator = _operator(tmp_path)

    # Verified to begin with.
    assert operator.read_collection_index(mapping.source_id).verified

    # A local edit to a listed document breaks the committed digest -> fail closed.
    doc_root = tmp_path / "memory" / "connected" / mapping.source_id
    listed = next(p for p in doc_root.glob("*.md") if p.name != "index.md")
    listed.write_text(listed.read_text(encoding="utf-8") + "\nlocal drift\n", encoding="utf-8")
    broken = operator.read_collection_index(mapping.source_id)
    assert broken.present and not broken.verified and broken.guidance

    # The re-sync the operator is told to run: reindex_source rewrites the index
    # from the actual on-disk documents (same path ingest drives).
    restored_count = await service.reindex_source(source)
    assert restored_count == 1

    # The reader now verifies and renders the body — recovery, not a dead end.
    healed = operator.read_collection_index(mapping.source_id)
    assert healed.present and healed.verified
    assert healed.document_count == 1 and healed.entries
    assert healed.markdown.startswith("# Collection Index")
    assert healed.error is None and healed.guidance is None


# -- Abuse cases: ungranted source, path traversal -------------------------------


def test_reader_ungranted_source_returns_empty(tmp_path: Path) -> None:
    view = _operator(tmp_path).read_collection_index("never-granted")
    assert not view.present and not view.verified
    assert view.markdown == "" and view.error is None


def test_reader_rejects_path_traversal_source_id(tmp_path: Path) -> None:
    # A source id that tries to escape the connected root never touches the FS.
    for evil in ("../../etc", "..", "a/b", "with/slash"):
        view = _operator(tmp_path).read_collection_index(evil)
        assert not view.present and not view.verified
        assert view.error == "invalid source id"
