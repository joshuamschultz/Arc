from __future__ import annotations

from arcokf import validate_collection_index

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import IndexRebuilder
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Scope

_DID = "did:arc:test:index-retrieval"


async def test_verified_memory_index_is_retrievable_after_restart(workspace, db, embedder) -> None:
    scope = Scope(agent_did=_DID)
    SemanticStore(workspace, WeightedGraph(db), scope=scope.key).write_fact(
        "alpha", "summary", "The alpha document routes operations"
    )

    index_path = workspace / "memory" / "index.md"
    assert validate_collection_index(index_path, workspace / "memory").valid
    await IndexRebuilder(db, workspace, scope, embedder=embedder).rebuild()
    db.connect().close()

    restarted = MemoryDB(workspace, dims=8)
    result = await SurfaceIndex(restarted, workspace, scope, embedder=embedder).search(
        "where is the alpha document?", top_k=10
    )
    assert any("entities/alpha.md" in recall.content for recall in result.recalls)


async def test_tampered_memory_index_is_regenerated_by_rebuild(workspace, db, embedder) -> None:
    scope = Scope(agent_did=_DID)
    SemanticStore(workspace, WeightedGraph(db), scope=scope.key).write_fact(
        "alpha", "summary", "The alpha document routes operations"
    )
    index_path = workspace / "memory" / "index.md"
    index_path.write_text(index_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    assert not validate_collection_index(index_path, workspace / "memory").valid

    await IndexRebuilder(db, workspace, scope, embedder=embedder).rebuild()

    assert validate_collection_index(index_path, workspace / "memory").valid
