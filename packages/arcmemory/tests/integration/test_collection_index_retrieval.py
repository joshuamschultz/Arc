from __future__ import annotations

from arcokf import validate_folder_index

from arcmemory.collection_index import memory_maintainer
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
    memory = workspace / "memory"
    await memory_maintainer(memory).drain()

    assert validate_folder_index(memory, root=True, deep=True).valid
    assert validate_folder_index(memory / "entities", deep=True).valid
    await IndexRebuilder(db, workspace, scope, embedder=embedder).rebuild()
    db.connect().close()

    restarted = MemoryDB(workspace, dims=8)
    result = await SurfaceIndex(restarted, workspace, scope, embedder=embedder).search(
        "where is the alpha document?", top_k=10
    )
    sources = {recall.source for recall in result.recalls}
    assert sources & {"file:memory/entities/alpha.md", "file:memory/entities/index.md"}
    # The folder's own routing chunk is searchable too, with no digest or frontmatter in it.
    routing = [r for r in result.recalls if r.source == "file:memory/entities/index.md"]
    assert routing and "sha256" not in routing[0].content


async def test_tampered_folder_index_is_regenerated_by_rebuild(workspace, db, embedder) -> None:
    scope = Scope(agent_did=_DID)
    SemanticStore(workspace, WeightedGraph(db), scope=scope.key).write_fact(
        "alpha", "summary", "The alpha document routes operations"
    )
    memory = workspace / "memory"
    await memory_maintainer(memory).drain()
    index_path = memory / "entities" / "index.md"
    index_path.write_text(index_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    assert not validate_folder_index(memory / "entities").valid

    await IndexRebuilder(db, workspace, scope, embedder=embedder).rebuild()

    assert validate_folder_index(memory / "entities", deep=True).valid
    assert validate_folder_index(memory, root=True).valid
