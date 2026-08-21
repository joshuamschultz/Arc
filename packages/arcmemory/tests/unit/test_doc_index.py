"""COMP-006 — per-source hybrid doc index + document_search (T-1030 RED / T-1031 GREEN).

``DocIndex`` stores chunk text (never file bytes) under a per-source scope,
isolated from the agent's memory-recall scope, and ``document_search`` fuses
vec+bm25+graph+recency (via ``SurfaceIndex.search``) scoped to exactly one
source at a time. Cross-source leakage would be an LLM08 vector/embedding
weakness -- one Dropbox connection must never surface another's chunks.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocHit, DocIndex, doc_scope
from arcmemory.index.source import SourceChunk

_AGENT_DID = "did:arc:doc-test-agent"


def _chunk(chunk_id: str, source_path: str, text: str, *, mtime: float = 1000.0) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        source_path=source_path,
        text=text,
        classification="unclassified",
        mtime=mtime,
    )


# Both source pools mention "revenue" deliberately -- a real isolation test
# proves the scope join, not an accident of non-overlapping vocabulary.
_DROPBOX_CHUNKS = [
    _chunk(
        "dropbox:c0",
        "dropbox://reports/q3.txt",
        "revenue report for Q3 showed strong revenue growth this quarter.",
    ),
    _chunk(
        "dropbox:c1",
        "dropbox://reports/q3-notes.txt",
        "supplemental revenue notes for the finance team to review.",
    ),
]
_OTHER_CHUNKS = [
    _chunk(
        "other:c0",
        "other://notes/cats.txt",
        "revenue talk overheard at the pet store, unrelated revenue chatter.",
    ),
    _chunk(
        "other:c1",
        "other://notes/kitten.txt",
        "the kitten jumped on the keyboard and typed nonsense.",
    ),
]


# -- doc_scope ------------------------------------------------------------


def test_doc_scope_key_is_isolated_per_source_and_agent() -> None:
    dropbox_scope = doc_scope(_AGENT_DID, "dropbox")
    other_scope = doc_scope(_AGENT_DID, "other")

    assert dropbox_scope.agent_did == _AGENT_DID
    assert dropbox_scope.key == f"{_AGENT_DID}:doc:dropbox"
    assert dropbox_scope.key != other_scope.key
    # Never collides with the agent's plain memory-recall scope.
    assert dropbox_scope.key != _AGENT_DID


# -- index_source / document_search scope isolation ------------------------


async def test_index_source_returns_count_of_indexed_chunks(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)

    count = await index.index_source("dropbox", _AGENT_DID, _DROPBOX_CHUNKS)

    assert count == len(_DROPBOX_CHUNKS)


async def test_document_search_scoped_to_one_source_never_returns_another_sources_chunks(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    await index.index_source("dropbox", _AGENT_DID, _DROPBOX_CHUNKS)
    await index.index_source("other", _AGENT_DID, _OTHER_CHUNKS)

    hits = await index.document_search("revenue", _AGENT_DID, source_id="dropbox", top_k=10)

    assert hits, "expected at least one dropbox hit for a shared keyword"
    assert all(hit.source_id == "dropbox" for hit in hits)
    assert all(hit.chunk_id.startswith("dropbox:") for hit in hits)
    assert not any(hit.chunk_id.startswith("other:") for hit in hits), (
        "search scoped to dropbox leaked chunks from source 'other'"
    )


async def test_dochit_carries_pointer_and_provenance_and_only_chunk_text(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    await index.index_source("dropbox", _AGENT_DID, _DROPBOX_CHUNKS)

    hits = await index.document_search("revenue", _AGENT_DID, source_id="dropbox", top_k=10)

    assert hits
    top = hits[0]
    assert isinstance(top, DocHit)
    assert top.pointer, "pointer must resolve back to the original object"
    assert top.pointer.startswith("dropbox://")
    assert top.provenance, "provenance must record where this chunk came from"
    # The hit carries CHUNK text -- and the raw file bytes were never passed to
    # DocIndex in the first place (only SourceChunk.text), so there is nothing
    # to leak and no API exists to fetch it.
    assert top.text in {c.text for c in _DROPBOX_CHUNKS}
    assert not hasattr(DocIndex, "file_bytes")
    assert not hasattr(DocIndex, "get_bytes")
    assert not hasattr(DocIndex, "raw_bytes")


async def test_document_search_unknown_source_id_returns_empty(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    await index.index_source("dropbox", _AGENT_DID, _DROPBOX_CHUNKS)

    hits = await index.document_search("revenue", _AGENT_DID, source_id="never-indexed", top_k=10)

    assert hits == []


# -- doc_search_enabled off-switch ------------------------------------------


async def test_document_search_returns_empty_when_doc_search_disabled(
    workspace: Path, db: MemoryDB, embedder
) -> None:
    disabled_config = MemoryConfig(doc_search_enabled=False)
    index = DocIndex(db, workspace, disabled_config, embedder=embedder)
    await index.index_source("dropbox", _AGENT_DID, _DROPBOX_CHUNKS)

    hits = await index.document_search("revenue", _AGENT_DID, source_id="dropbox", top_k=10)

    assert hits == []
