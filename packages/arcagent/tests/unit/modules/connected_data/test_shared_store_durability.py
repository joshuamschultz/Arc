"""Only a provider-rebuildable store trades the per-commit fsync for loop time.

The connection-scoped shared store (P18-4) is re-derivable from the provider,
so its index commits skip the fsync (``synchronous=NORMAL``). An agent's own
workspace store also holds its episodic raw stream and keeps ``FULL``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend

from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter
from arcagent.modules.connected_data.shared import SharedKnowledge

_DID = "did:arc:test:agent-a"


def _synchronous(port: ArcMemoryIngestAdapter) -> int:
    conn = port._connected_service()._db.connect()
    return int(conn.execute("PRAGMA synchronous").fetchone()[0])


@pytest.mark.asyncio
async def test_shared_connection_store_commits_without_fsync(tmp_path: Path) -> None:
    backend = FakeBackend()

    async def opener() -> FakeBackend:
        return backend

    shared = SharedKnowledge(
        agent_did=_DID,
        arcstore_opener=opener,
        embedder=lambda: None,
        profile=lambda: "lexical",
        root=lambda: tmp_path / "shared",
    )
    port = await shared.reader("wiki")
    try:
        assert _synchronous(port) == 1  # NORMAL
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_agent_own_connected_store_keeps_full_durability(tmp_path: Path) -> None:
    port = ArcMemoryIngestAdapter(tmp_path / "workspace", _DID, approval_store=None)
    try:
        assert _synchronous(port) == 2  # FULL
    finally:
        await port.aclose()
