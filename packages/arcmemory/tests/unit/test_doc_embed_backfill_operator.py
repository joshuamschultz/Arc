"""Operator side of the doc-pool embed backfill: watch it, or run one store to completion.

The serving process backfills in the background. An operator needs to see the
backlog drain (a read-only readout that is safe beside the running service) and,
with the service stopped, to run one store to completion. A run must refuse
while another process owns the store: two processes writing one ``index.db``
make the service rebuild its whole vector sidecar from ``vec0`` over and over.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest
from packages.arcmemory.tests.conftest import StubEmbedder

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.doc_index import DocIndex, doc_scope
from arcmemory.index import ann
from arcmemory.index.backfill import (
    backfill_owner,
    backfill_store,
    read_embed_backlog,
)
from arcmemory.index.rebuild import EmbeddingUnavailableError
from arcmemory.index.source import SourceChunk

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_AGENT = "did:arc:operator-test"


class _Wide:
    """Deterministic 384-dim vectors (the default index width)."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        narrow = await StubEmbedder(dims=8).embed_texts(texts)
        return [vector * 48 for vector in narrow]


class _Down:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("model not loaded")


@pytest.fixture(autouse=True)
def _forget_indexes():
    yield
    ann.forget_loaded_indexes()


async def _seed(workspace: Path, count: int, source: str = "dropbox") -> str:
    db = MemoryDB(workspace)
    try:
        await DocIndex(db, workspace, MemoryConfig(), embedder=None).index_source(
            source,
            _AGENT,
            [
                SourceChunk(
                    chunk_id=f"{source}:o{i}#0",
                    source_path=f"connected/{source}/{i}.md",
                    text=f"{source} doc {i}",
                    classification="unclassified",
                    mtime=float(i),
                )
                for i in range(count)
            ],
        )
    finally:
        db.close()
    return doc_scope(_AGENT, source).key


async def test_the_readout_reports_each_pools_coverage_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    scope = await _seed(workspace, 7)
    db_path = workspace / "memory" / "index.db"
    before = db_path.stat().st_mtime_ns
    backlog = read_embed_backlog(db_path)
    assert backlog[scope].total == 7
    assert backlog[scope].pending == 7
    assert backlog[scope].embedded == 0
    assert db_path.stat().st_mtime_ns == before


def test_the_readout_of_a_missing_store_is_empty(tmp_path: Path) -> None:
    assert read_embed_backlog(tmp_path / "nowhere" / "memory" / "index.db") == {}


async def test_a_store_runs_to_completion(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    scope = await _seed(workspace, 40)
    seen: list[int] = []
    outcome = await backfill_store(
        workspace, MemoryConfig(), _Wide(), batch_size=16, on_progress=seen.append
    )
    assert outcome.status == "done"
    assert outcome.embedded == 40
    assert read_embed_backlog(workspace / "memory" / "index.db")[scope].pending == 0
    assert seen and seen[-1] == 40
    assert backfill_owner(workspace / "memory" / "index.db") is None  # released


async def test_a_run_refuses_while_another_process_owns_the_store(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    await _seed(workspace, 3)
    lock_path = workspace / "memory" / ".embed-backfill.lock"
    with lock_path.open("a+") as other:  # a second open file = another process's claim
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        other.write("4242\n")
        other.flush()
        assert backfill_owner(workspace / "memory" / "index.db") == 4242
        embedder = _Wide()
        outcome = await backfill_store(workspace, MemoryConfig(), embedder)
        assert outcome.status == "blocked"
        assert embedder.calls == 0
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
    assert backfill_owner(workspace / "memory" / "index.db") is None


async def test_a_run_gives_up_after_the_embedder_stays_down(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    await _seed(workspace, 3)
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    outcome = await backfill_store(workspace, MemoryConfig(), _Down(), retries=3, sleep=no_wait)
    assert outcome.status == "embedder_down"
    assert outcome.embedded == 0
    assert len(waits) == 3


def test_a_claim_names_the_owning_process(tmp_path: Path) -> None:
    from arcmemory.index.backfill import backfill_writer_lock

    db_path = tmp_path / "ws" / "memory" / "index.db"
    claim = backfill_writer_lock(db_path)
    assert claim.acquire()
    try:
        assert (db_path.parent / ".embed-backfill.lock").read_text().strip() == str(os.getpid())
    finally:
        claim.release()


async def test_a_store_run_drains_the_file_it_names_whatever_the_agents_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet runs the Brain on Postgres; doc pools still live in the SQLite file.

    An operator's run is over one ``index.db``: an agent config carrying
    ``index_backend = "postgres"`` must not send it to look somewhere else.
    """
    monkeypatch.delenv("ARC_MEMORY_PG_DSN", raising=False)
    workspace = tmp_path / "ws"
    scope = await _seed(workspace, 9)
    outcome = await backfill_store(workspace, MemoryConfig(index_backend="postgres"), _Wide())
    assert outcome.status == "done"
    assert outcome.embedded == 9
    assert read_embed_backlog(workspace / "memory" / "index.db")[scope].pending == 0
