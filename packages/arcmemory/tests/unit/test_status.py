"""``semantic_status`` — the readout that answers "is semantic recall actually on?".

An operator must be able to find out that recall lost its vector channel WITHOUT
reading an audit log. This is the probe behind ``arc memory status``: it exercises
the real embedder seam (not a capability guess), reports the sqlite-vec extension,
and counts how many indexed chunks actually carry a vector.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.index.rebuild import EmbeddingUnavailableError
from arcmemory.index.surface import SurfaceIndex
from arcmemory.status import semantic_status
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope


class _LiveEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class _DeadEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("the arcllm[local] extra is not installed")


async def test_status_reports_a_live_semantic_channel() -> None:
    status = await semantic_status(embedder=_LiveEmbedder(), backend="local")

    assert status.embedder_live is True
    assert status.live is status.vec_extension  # the channel needs BOTH halves


async def test_status_reports_a_dead_embedder_without_raising() -> None:
    status = await semantic_status(embedder=_DeadEmbedder(), backend="local")

    assert status.embedder_live is False
    assert status.live is False
    assert "extra" in status.detail


async def test_status_reports_an_explicitly_disabled_backend() -> None:
    status = await semantic_status(embedder=None, backend="none")

    assert status.embedder_live is False
    assert "none" in status.detail


async def test_status_counts_vectors_actually_written(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """Counts catch the second failure mode: embedder live, index never rebuilt."""
    episodic = EpisodicStore(db, workspace)
    episodic.append(
        Event(
            event_id="e1",
            ts="2026-01-01T00:00:00+00:00",
            scope=scope.key,
            kind="obs",
            text="a note worth indexing",
        )
    )
    index = SurfaceIndex(db, workspace, scope, embedder=_LiveEmbedder())
    await index.index_if_needed()
    db.close()

    status = await semantic_status([workspace], embedder=_LiveEmbedder())

    assert len(status.workspaces) == 1
    assert status.workspaces[0].indexed_chunks == 1
    assert status.workspaces[0].embedded_chunks == 1


async def test_status_skips_a_directory_with_no_memory_index(tmp_path: Path) -> None:
    """A status probe must never create an index DB as a side effect."""
    status = await semantic_status([tmp_path], embedder=_LiveEmbedder())

    assert status.workspaces == []
    assert not (tmp_path / "memory" / "index.db").exists()
