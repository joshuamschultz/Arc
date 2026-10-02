"""P21 (J1 F1/F11/F12): no-source fan-out, per-document collapse, provenance.

``document_search`` with no source used to return ``[]``, so an agent that
omitted ``source`` (as the tool description tells it to) never saw a hit. These
tests drive the real ``DocIndex`` and the real ingest path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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
    source_instance_id,
)
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.source import SourceChunk

_DID = "did:arc:fanout-agent"


def _chunk(chunk_id: str, path: str, text: str) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        source_path=path,
        text=text,
        classification="unclassified",
        mtime=1000.0,
    )


async def test_no_source_fans_out_across_every_pool(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    await index.index_source("wiki", _DID, [_chunk("wiki:a#0", "p/a.md", "onboarding checklist")])
    await index.index_source(
        "mail", _DID, [_chunk("mail:b#0", "p/b.md", "onboarding email thread")]
    )

    hits = await index.document_search("onboarding", _DID, top_k=10)

    assert {hit.source_id for hit in hits} == {"wiki", "mail"}


async def test_no_source_never_reads_another_agents_pools(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    await index.index_source(
        "wiki", "did:arc:other", [_chunk("wiki:a#0", "p/a.md", "secret plan")]
    )

    assert await index.document_search("secret", _DID, top_k=10) == []


async def test_hits_collapse_to_the_best_chunk_per_document(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    long_doc = [_chunk(f"wiki:long#{n}", "p/long.md", f"budget budget line {n}") for n in range(6)]
    other = [_chunk("wiki:short#0", "p/short.md", "budget summary")]
    await index.index_source("wiki", _DID, long_doc + other)

    hits = await index.document_search("budget", _DID, top_k=3)

    pointers = [hit.pointer for hit in hits]
    assert len(pointers) == len(set(pointers))
    assert set(pointers) == {"p/long.md", "p/short.md"}


async def test_several_source_ids_search_exactly_those_pools(
    workspace: Path, db: MemoryDB, config: MemoryConfig, embedder
) -> None:
    index = DocIndex(db, workspace, config, embedder=embedder)
    await index.index_source("a", _DID, [_chunk("a:x#0", "p/a.md", "invoice data")])
    await index.index_source("b", _DID, [_chunk("b:x#0", "p/b.md", "invoice data")])
    await index.index_source("c", _DID, [_chunk("c:x#0", "p/c.md", "invoice data")])

    hits = await index.document_search("invoice", _DID, source_ids=["a", "c"], top_k=10)

    assert {hit.source_id for hit in hits} == {"a", "c"}


async def _approved_service(
    workspace: Path, source: ConnectedSource
) -> tuple[ConnectedDataService, object]:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(
        workspace, _DID, approval_store=approval, config=MemoryConfig(doc_chunk_tokens=32)
    )
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    return service, await service.require_approved_mapping(source)


async def test_provenance_is_persisted_and_survives_a_restart(tmp_path: Path) -> None:
    source = ConnectedSource(
        connection_id="confluence",
        account_id="acct",
        source_kind="confluence",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )
    service, mapping = await _approved_service(tmp_path, source)
    await service.ingest(
        source,
        ConnectedObject(
            object_id="page-1",
            locator="/spaces/ENG/pages/1",
            version="1",
            media_type="text/plain",
            classification="unclassified",
            metadata={
                "title": "Q3 Plan",
                "url": "https://wiki.example.com/pages/1",
                "modified_at": "2026-09-30T12:00:00Z",
            },
        ),
        SourceContent(
            object_id="page-1", version="1", media_type="text/plain", content=b"roadmap for Q3"
        ),
        mapping,  # type: ignore[arg-type]  # test helper returns the approved mapping
    )
    service.close()

    restarted = DocIndex(MemoryDB(tmp_path), tmp_path, MemoryConfig())
    hits = await restarted.document_search("roadmap", _DID, top_k=5)

    assert hits
    hit = hits[0]
    assert hit.source_id == source_instance_id(_DID, source)
    assert hit.title == "Q3 Plan"
    assert hit.source_kind == "confluence"
    assert hit.url == "https://wiki.example.com/pages/1"
    assert hit.updated_at == "2026-09-30T12:00:00Z"
