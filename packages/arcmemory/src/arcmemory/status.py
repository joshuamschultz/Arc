"""Is semantic recall actually on? — the operator-facing readout (``arc memory status``).

The silent-degrade trap has two independent halves, and a deployment can lose either
one without anything appearing broken:

* the **sqlite-vec extension**, which makes the ``vec0`` table exist at all, and
* the **embedder**, which fills it (arcllm's ``local`` backend needs the
  ``arcmemory[local]`` extra; its ``provider`` backend needs an endpoint).

So the probe checks both, and it checks the embedder by *actually embedding* rather
than by guessing from an import — the same call recall makes, so a green readout
means the real path works. It never raises: a dead embedder is reported, not thrown.

Counts close the third gap: an embedder can be live while an agent's index was
never rebuilt, leaving chunks with no vectors. ``embedded_chunks`` vs
``indexed_chunks`` shows that at a glance.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.degrade import degraded_reasons
from arcmemory.index.rebuild import Embedder, EmbeddingUnavailableError

_PROBE_TEXT = "arcmemory semantic channel probe"


class WorkspaceVectors(BaseModel):
    """How much of one agent's index actually carries a vector."""

    workspace: str
    indexed_chunks: int = 0
    embedded_chunks: int = 0
    insight_triggers: int = 0


class SemanticStatus(BaseModel):
    """The full readout: both halves of the channel, plus per-agent coverage."""

    live: bool = False
    vec_extension: bool = False
    embedder_backend: str = "local"
    embedder_live: bool = False
    embedder_dims: int | None = None
    detail: str = ""
    degraded_reasons: list[str] = Field(default_factory=list)
    workspaces: list[WorkspaceVectors] = Field(default_factory=list)


async def semantic_status(
    workspaces: Sequence[Path] = (),
    *,
    embedder: Embedder | None,
    backend: str = "local",
) -> SemanticStatus:
    """Probe the semantic channel end to end and report it (never raises)."""
    vec_extension = sqlite_vec_loadable()
    live, dims, detail = await _probe(embedder, backend)
    return SemanticStatus(
        live=live and vec_extension,
        vec_extension=vec_extension,
        embedder_backend=backend,
        embedder_live=live,
        embedder_dims=dims,
        detail=detail,
        degraded_reasons=sorted(degraded_reasons()),
        workspaces=[v for w in workspaces if (v := _inspect(w)) is not None],
    )


async def _probe(embedder: Embedder | None, backend: str) -> tuple[bool, int | None, str]:
    """Run one real embed call; return ``(live, dims, human detail)``."""
    if embedder is None:
        if backend == "none":
            return False, None, "embed_backend = 'none' — semantic recall is off by choice."
        return False, None, "no embedder is wired for this agent."
    try:
        vectors = await embedder.embed_texts([_PROBE_TEXT])
    except EmbeddingUnavailableError as exc:
        return False, None, str(exc)
    if not vectors:
        return False, None, "the embedder returned no vector."
    return True, len(vectors[0]), "the embedder answered a live probe."


def _inspect(workspace: Path) -> WorkspaceVectors | None:
    """Count one workspace's chunks vs vectors, or ``None`` if it has no index.

    Read-only by construction: an absent index DB is skipped rather than created,
    so running the status command never leaves a stray ``memory/index.db`` behind.
    """
    db = MemoryDB(workspace)
    if not db.db_path.exists():
        return None
    try:
        conn = db.connect()
        return WorkspaceVectors(
            workspace=str(workspace),
            indexed_chunks=_count(conn, "chunks"),
            embedded_chunks=_count(conn, "vec0") if db.vec_available else 0,
            insight_triggers=_count(conn, "insight_trigger"),
        )
    finally:
        db.close()


def _count(conn: sqlite3.Connection, table: str) -> int:
    """Row count for ``table``, or 0 where the table does not exist."""
    try:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


__all__ = ["SemanticStatus", "WorkspaceVectors", "semantic_status"]
