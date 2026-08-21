"""COMP-007 — vec0 must be scope-isolated (T-1020 RED / T-1021 GREEN).

Today's ``SurfaceIndex._vec_search`` (``index/surface.py``) runs
``SELECT chunk_id, embedding FROM vec0`` with NO scope join, so a vector search
scoped to agent A can return agent B's chunk ids — a cross-scope leak (LLM08).
This test drives the REAL ``SurfaceIndex`` over two scopes sharing one DB and
MUST fail today for that reason; it passes once the scope-join fix (via the new
``IndexBackend.vec_search``) lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope

requires_vec = pytest.mark.skipif(
    not sqlite_vec_loadable(),
    reason="sqlite-vec extension not loadable in this Python/SQLite build",
)


@requires_vec
async def test_vec_search_never_returns_another_scopes_chunks(
    workspace: Path, db: MemoryDB, embedder
) -> None:
    scope_a = Scope(agent_did="did:arc:agent-a")
    scope_b = Scope(agent_did="did:arc:agent-b")
    episodic = EpisodicStore(db, workspace)
    episodic.append(
        Event(
            event_id="a0",
            scope=scope_a.key,
            kind="obs",
            text="agent a private note about the launch",
            ts="2026-01-01T00:00:00+00:00",
        )
    )
    episodic.append(
        Event(
            event_id="b0",
            scope=scope_b.key,
            kind="obs",
            text="agent b private note about the launch",
            ts="2026-01-01T00:00:00+00:00",
        )
    )

    surface_a = SurfaceIndex(db, workspace, scope_a, embedder=embedder)
    surface_b = SurfaceIndex(db, workspace, scope_b, embedder=embedder)
    await surface_a.index_if_needed()
    await surface_b.index_if_needed()

    result_ids = await surface_a._vec_search("launch note")

    assert result_ids is not None
    assert "event:b0" not in result_ids, "scope A's vec search leaked scope B's chunk"
