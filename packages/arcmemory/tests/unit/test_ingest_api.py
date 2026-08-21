"""COMP-002 — brain-port ingestion API (SPEC-073 Phase 1, T-1018/T-1019).

Drives the REAL ``ArcMemoryBrain`` (not a helper) — ``ingest_batch``'s
dedup/staleness/cap semantics are the acceptance criteria on the brain-port seam
itself, and episodic counts are the durable side effect the boundary is trusted
to produce. Asserted via ``EpisodicStore.count`` against the brain's own DB, the
same pattern ``tests/integration/test_wired_brain.py`` uses to inspect state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import IngestResult, Scope, SourceRecord

_DID = "did:arc:test-agent"


def _scope() -> Scope:
    return Scope(agent_did=_DID)


def _brain(workspace: Path, *, config: MemoryConfig | None = None) -> ArcMemoryBrain:
    return ArcMemoryBrain(workspace, _DID, config=config)


async def test_ingest_batch_duplicate_batch_upserts_once(workspace: Path) -> None:
    brain = _brain(workspace)
    records = [
        SourceRecord(
            external_id="r1", text="hello world", source_updated_at="2026-01-01T00:00:00+00:00"
        ),
        SourceRecord(
            external_id="r2",
            text="goodnight moon",
            source_updated_at="2026-01-01T00:00:01+00:00",
        ),
    ]

    first = await brain.ingest_batch("source-a", records)
    second = await brain.ingest_batch("source-a", records)  # identical batch, re-ingested

    episodic = EpisodicStore(brain._db, workspace)
    assert episodic.count(_scope().key) == 2  # stable across the duplicate batch
    assert isinstance(first, IngestResult)
    assert first.ingested == 2
    assert second.ingested == 0
    assert second.deduped == 2


async def test_ingest_batch_out_of_order_record_is_skipped_as_stale(workspace: Path) -> None:
    brain = _brain(workspace)
    newer = SourceRecord(
        external_id="r1", text="new state", source_updated_at="2026-01-02T00:00:00+00:00"
    )
    older = SourceRecord(
        external_id="r1", text="old state", source_updated_at="2026-01-01T00:00:00+00:00"
    )

    await brain.ingest_batch("source-a", [newer])
    result = await brain.ingest_batch("source-a", [older])

    episodic = EpisodicStore(brain._db, workspace)
    assert episodic.count(_scope().key) == 1  # the stale write never happened
    assert result.ingested == 0
    assert result.skipped_stale == 1


async def test_ingest_batch_over_cap_raises_and_writes_nothing(workspace: Path) -> None:
    # MemoryConfig is frozen — construct a fresh one with a tiny cap for this test.
    config = MemoryConfig(ingest_max_batch=2)
    brain = _brain(workspace, config=config)
    records = [SourceRecord(external_id=f"r{i}", text=f"text {i}") for i in range(3)]

    with pytest.raises(ValueError):
        await brain.ingest_batch("source-a", records)

    episodic = EpisodicStore(brain._db, workspace)
    assert episodic.count(_scope().key) == 0  # rejected at the boundary; nothing written
