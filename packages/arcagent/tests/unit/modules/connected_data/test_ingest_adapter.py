from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcagent.connected_data import MappingPendingError
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data import ArcMemoryIngestAdapter


@pytest.mark.asyncio
async def test_arc_memory_adapter_maps_pending_approval_to_ingest_port(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    adapter = ArcMemoryIngestAdapter(
        tmp_path,
        "did:arc:adapter",
        approval_store=approval,
    )
    source = SourceDescription(
        connection_id="dropbox",
        source_kind="dropbox",
        account_id="account",
    )

    with pytest.raises(MappingPendingError):
        await adapter.require_approved_mapping(source)

    assert len(await approval.list()) == 1


@pytest.mark.asyncio
async def test_arc_memory_adapter_preserves_approval_id_separately(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    adapter = ArcMemoryIngestAdapter(
        tmp_path,
        "did:arc:adapter",
        approval_store=approval,
    )
    source = SourceDescription(
        connection_id="dropbox",
        source_kind="dropbox",
        account_id="account",
    )

    with pytest.raises(MappingPendingError):
        await adapter.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")

    mapping = await adapter.require_approved_mapping(source)

    assert mapping.mapping_id == pending.id
