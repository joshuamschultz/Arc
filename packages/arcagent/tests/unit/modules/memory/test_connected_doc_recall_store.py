"""Per-turn connected-document retrieval finds the agent's own pools on a Postgres Brain.

Production (2026-10-04): the fleet runs the Brain's memory on Postgres, while every
connected doc pool sits in the workspace SQLite index. Recall searched Postgres
and found nothing, so an agent never saw its own Dropbox/GitHub documents. Doc
pools now live in one store whatever the memory backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.memory import _runtime
from arcagent.modules.memory import capabilities as cap

pytestmark = pytest.mark.anyio

_DID = "did:arc:test:recall-store"
#: A Postgres DSN nobody listens on: any doc read sent there fails loudly.
_DEAD_DSN = "postgresql://arc:arc@127.0.0.1:1/arc_memory"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _forget_vector_indexes() -> Any:
    yield
    from arcmemory.index import ann

    ann.forget_loaded_indexes()


async def _connected_data_writes(workspace: Path) -> None:
    from arcmemory.config import MemoryConfig
    from arcmemory.db import MemoryDB
    from arcmemory.doc_index import DocIndex
    from arcmemory.index.source import SourceChunk

    db = MemoryDB(workspace)
    try:
        await DocIndex(db, workspace, MemoryConfig(), embedder=None).index_source(
            "dropbox",
            _DID,
            [
                SourceChunk(
                    chunk_id="dropbox:board-minutes#0",
                    source_path="memory/connected/dropbox/board-minutes.md",
                    text="board minutes approving the zephyr datacenter lease",
                    classification="unclassified",
                    mtime=1.0,
                )
            ],
        )
    finally:
        db.close()


async def test_recall_surfaces_own_connected_documents_on_a_postgres_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcmemory.brain import ArcMemoryBrain
    from arcmemory.config import MemoryConfig as BrainConfig

    from arcagent.modules.memory.config import MemoryConfig

    monkeypatch.setenv("ARC_MEMORY_PG_DSN", _DEAD_DSN)
    workspace = tmp_path / "workspace"
    await _connected_data_writes(workspace)
    brain = ArcMemoryBrain(workspace, _DID, config=BrainConfig(index_backend="postgres"))
    state = _runtime._State(
        config=MemoryConfig(),
        brain=brain,
        workspace=workspace,
        telemetry=None,
        bus=None,
        agent_did=_DID,
        active=True,
    )

    _runtime.bind(state)
    try:
        found = await cap.ContextRetrieval().retrieve(
            "zephyr datacenter lease", memory_top_k=0, docs_top_k=3
        )
    finally:
        _runtime.reset()

    documents = [c for c in found["candidates"] if c["source_kind"] == "connection"]
    assert documents and "zephyr datacenter lease" in documents[0]["text"]
