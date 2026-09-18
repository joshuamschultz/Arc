"""SPEC-082 COMP-009 (T-1093 RED) — mapping approval is verified once per run.

REQ-428: while ingesting a sync run, the system SHALL verify mapping approval
once per run (cached, keyed by the mapping ``call_hash``) rather than once per
object.

Today ``ConnectedDataService.ingest`` calls ``_mapping_is_approved`` for EVERY
object, and each call does ``approval.start()`` + ``approval.list(status=...)``.
A run over N objects therefore hits the approval spine N times — pure churn that
also widens the window in which a concurrent operator action can make two
objects in the same run disagree about whether their shared mapping is approved.

The load-bearing invariant this must not break: an approved mapping is durable —
it never lapses on a timer within a run (only a structural change to the mapping,
which re-derives a new ``call_hash``, re-triggers approval).

These tests fail RED because the per-run cache does not exist yet: the approval
store is consulted once per object, not once per mapping ``call_hash``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
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


class _CountingApproval(ApprovalStore):
    """The real approval spine, counting how often it is consulted."""

    def __init__(self, backend: FakeBackend) -> None:
        super().__init__(backend)
        self.start_calls = 0
        self.list_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        await super().start()

    async def list(self, *, status: str | None = None) -> list[PendingApproval]:
        self.list_calls += 1
        return await super().list(status=status)


def _source(*, connection_id: str = "dropbox", account_id: str = "account") -> ConnectedSource:
    return ConnectedSource(
        connection_id=connection_id,
        account_id=account_id,
        source_kind="dropbox",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


def _service(workspace: Path, approval: ApprovalStore) -> ConnectedDataService:
    return ConnectedDataService(
        workspace,
        "did:arc:agent",
        approval_store=approval,
        config=MemoryConfig(doc_chunk_tokens=32, tier="personal"),
    )


def _object(object_id: str) -> ConnectedObject:
    return ConnectedObject(
        object_id=object_id,
        locator=f"/reports/{object_id}.txt",
        version="1",
        media_type="text/plain",
        classification="unclassified",
    )


def _content(object_id: str) -> SourceContent:
    return SourceContent(
        object_id=object_id,
        version="1",
        media_type="text/plain",
        content=f"quarterly revenue report {object_id}".encode(),
    )


async def _approved_mapping(service: ConnectedDataService, approval: ApprovalStore, source):  # type: ignore[no-untyped-def]
    """Stage and approve one mapping, returning the durable ApprovedMapping."""
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    return await service.require_approved_mapping(source)


@pytest.mark.asyncio
async def test_mapping_approval_is_verified_once_per_run_not_per_object(tmp_path: Path) -> None:
    """Five objects in one run must verify their shared mapping exactly once."""
    approval = _CountingApproval(FakeBackend())
    service = _service(tmp_path, approval)
    source = _source()
    mapping = await _approved_mapping(service, approval, source)

    # Count only the ingest run, not the staging/approval setup above.
    approval.start_calls = 0
    approval.list_calls = 0

    for index in range(5):
        await service.ingest(source, _object(f"report-{index}"), _content(f"report-{index}"), mapping)

    assert approval.list_calls == 1, (
        "mapping approval was re-verified per object instead of once per run; "
        f"approval.list was called {approval.list_calls} times for 5 objects"
    )
    assert approval.start_calls == 1, (
        f"approval.start was called {approval.start_calls} times for 5 objects (expected once)"
    )


@pytest.mark.asyncio
async def test_run_cache_is_keyed_by_mapping_call_hash(tmp_path: Path) -> None:
    """Two distinct mappings each get their own verification — not one shared blanket.

    Guards against an over-broad "verify once ever" cache: a cache keyed by the
    mapping ``call_hash`` must miss for a second, structurally different mapping
    and verify it on its own, so a denial of one mapping can never authorize
    another.
    """
    approval = _CountingApproval(FakeBackend())
    service = _service(tmp_path, approval)
    source_a = _source(connection_id="dropbox", account_id="account-a")
    source_b = _source(connection_id="dropbox", account_id="account-b")
    mapping_a = await _approved_mapping(service, approval, source_a)
    mapping_b = await _approved_mapping(service, approval, source_b)

    approval.start_calls = 0
    approval.list_calls = 0

    await service.ingest(source_a, _object("a-1"), _content("a-1"), mapping_a)
    await service.ingest(source_a, _object("a-2"), _content("a-2"), mapping_a)
    await service.ingest(source_b, _object("b-1"), _content("b-1"), mapping_b)

    assert approval.list_calls == 2, (
        "the per-run cache is not keyed by mapping call_hash: expected exactly one "
        f"verification per distinct mapping (2), saw {approval.list_calls}"
    )
