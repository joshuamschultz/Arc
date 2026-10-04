"""Doc-pool embed backfill: chunks written while the embedder was down get vectors later.

Production (2026-10): ``DocIndex.index_source`` embeds a source's chunks once, at
write time. A chunk written while the embedder was unavailable was stored
lexical-only (no ``vec0`` row, ``embedded_hash`` NULL) and nothing ever retried
it: one agent held 1.24M doc chunks and 44k vectors. The backfill finds every
doc-pool chunk whose vector is missing or stale, embeds it in bounded batches,
and writes ONLY the vector (+ ``embedded_hash``) through the backend, so the HNSW
sidecar is kept in step by the normal delta path.
"""

from __future__ import annotations

import asyncio
import fcntl
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from packages.arcmemory.tests.conftest import StubEmbedder

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.doc_index import DocIndex, doc_scope
from arcmemory.index import ann
from arcmemory.index.backend import (
    ChunkWrite,
    EmbeddingWrite,
    PendingEmbed,
    SqliteIndexBackend,
    open_index_backend,
)
from arcmemory.index.backfill import (
    BackfillPacer,
    BackfillTick,
    DocEmbedBackfill,
    backfill_writer_lock,
)
from arcmemory.index.rebuild import EmbeddingUnavailableError
from arcmemory.index.source import EMBED_TEXT_MAX_CHARS, SourceChunk
from arcmemory.security import content_hash

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_DIMS = 8
_AGENT = "did:arc:backfill-agent"
_OTHER = "did:arc:other-agent"


@pytest.fixture
def bdb(tmp_path: Path):
    memdb = MemoryDB(tmp_path / "ws", dims=_DIMS)
    memdb.connect()
    yield memdb
    ann.forget_loaded_indexes()
    memdb.close()


class _DownEmbedder:
    """A wired embedder whose backend is not serving (model not loaded yet)."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingUnavailableError("model not loaded yet")


class _RecordingEmbedder(StubEmbedder):
    """Deterministic vectors; records every batch it was handed."""

    def __init__(self) -> None:
        super().__init__(dims=_DIMS)
        self.batches: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return await super().embed_texts(texts)


class _FlakyEmbedder(_RecordingEmbedder):
    """Serves, then goes unavailable for ``down_calls`` calls, then serves again."""

    def __init__(self, *, fail_on: set[int]) -> None:
        super().__init__()
        self._fail_on = fail_on
        self.attempts = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.attempts += 1
        if self.attempts in self._fail_on:
            raise EmbeddingUnavailableError("embed queue full")
        return await super().embed_texts(texts)


class _WideEmbedder:
    """Deterministic vectors at 384 dims, the default ``MemoryDB`` width."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        narrow = await StubEmbedder(dims=_DIMS).embed_texts(texts)
        return [vector * 48 for vector in narrow]


def _chunks(prefix: str, count: int) -> list[SourceChunk]:
    return [
        SourceChunk(
            chunk_id=f"{prefix}:obj{i}#{i}",
            source_path=f"connected/{prefix}/doc{i}.md",
            text=f"{prefix} document {i} about topic{i % 7} and quarterly revenue",
            classification="unclassified",
            mtime=1000.0 + i,
        )
        for i in range(count)
    ]


async def _index_during_outage(db: MemoryDB, source_id: str, chunks: list[SourceChunk]) -> None:
    index = DocIndex(db, db.db_path.parent.parent, MemoryConfig(), embedder=_DownEmbedder())
    await index.index_source(source_id, _AGENT, chunks)


def _hashes(db: MemoryDB, scope: str) -> list[tuple[str, str, str | None]]:
    return (
        db.connect()
        .execute(
            "SELECT chunk_id, content_hash, embedded_hash FROM chunks WHERE scope=? ORDER BY chunk_id",
            (scope,),
        )
        .fetchall()
    )


def _vector_count(db: MemoryDB, scope: str) -> int:
    row = (
        db.connect()
        .execute(
            "SELECT COUNT(*) FROM vec0 WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE scope=?)",
            (scope,),
        )
        .fetchone()
    )
    return int(row[0])


def _backfill(db: MemoryDB, embedder, **kwargs) -> DocEmbedBackfill:
    return DocEmbedBackfill(
        db, MemoryConfig(), embedder, scope_prefix=doc_scope(_AGENT, "").key, **kwargs
    )


async def _drain(backfill: DocEmbedBackfill, *, per_tick: int = 1000) -> int:
    embedded = 0
    for _ in range(100):
        tick = await backfill.run_batches(per_tick)
        embedded += tick.embedded
        if tick.exhausted:
            return embedded
    raise AssertionError("backfill never reported an exhausted backlog")


# -- the core regression ------------------------------------------------------


async def test_chunks_written_during_an_outage_are_embedded_by_the_backfill(
    bdb: MemoryDB,
) -> None:
    chunks = _chunks("dropbox", 40)
    await _index_during_outage(bdb, "dropbox", chunks)
    scope = doc_scope(_AGENT, "dropbox").key
    assert all(embedded is None for _, _, embedded in _hashes(bdb, scope))
    assert _vector_count(bdb, scope) == 0

    backfill = _backfill(bdb, _RecordingEmbedder(), batch_size=16)
    backlog = await backfill.backlog()
    assert backlog[scope].pending == 40
    assert backlog[scope].total == 40

    assert await _drain(backfill) == 40

    rows = _hashes(bdb, scope)
    assert all(embedded == stored for _, stored, embedded in rows)
    assert _vector_count(bdb, scope) == 40
    assert (await backfill.backlog())[scope].pending == 0

    # Searchable through the normal vector channel, i.e. the HNSW sidecar.
    backend = open_index_backend("sqlite", db=bdb)
    probe = (await StubEmbedder(dims=_DIMS).embed_texts([chunks[5].text]))[0]
    hits = await backend.vec_search(scope, probe, top_k=3)
    assert hits[0] == chunks[5].chunk_id
    index = ann.scope_ann(bdb, scope)
    assert await index.wait_built(timeout=30.0)
    assert index.mode == "hnsw"
    generation = (
        bdb.connect()
        .execute("SELECT generation FROM vec_generation WHERE scope=?", (scope,))
        .fetchone()[0]
    )
    keys = {key for key, _ in index.search(probe, 40, generation)}
    rowids = {
        int(row[0])
        for row in bdb.connect().execute("SELECT rowid FROM chunks WHERE scope=?", (scope,))
    }
    assert keys == rowids


async def test_backfill_embeds_the_capped_text_like_the_surface_path(bdb: MemoryDB) -> None:
    long = SourceChunk(
        chunk_id="dropbox:big#0",
        source_path="connected/dropbox/big.md",
        text="x" * (EMBED_TEXT_MAX_CHARS + 500),
        classification="unclassified",
        mtime=1.0,
    )
    await _index_during_outage(bdb, "dropbox", [long])
    embedder = _RecordingEmbedder()
    await _drain(_backfill(bdb, embedder))
    assert [len(text) for batch in embedder.batches for text in batch] == [EMBED_TEXT_MAX_CHARS]


# -- only what is missing or stale is embedded --------------------------------


async def test_only_missing_or_stale_vectors_are_embedded(bdb: MemoryDB) -> None:
    chunks = _chunks("github", 12)
    scope = doc_scope(_AGENT, "github").key
    warm = _RecordingEmbedder()
    await DocIndex(bdb, bdb.db_path.parent.parent, MemoryConfig(), embedder=warm).index_source(
        "github", _AGENT, chunks
    )
    # One chunk's content changes while the embedder is down: its vector is stale.
    changed = chunks[3].model_copy(update={"text": "rewritten body about pricing"})
    await _index_during_outage(bdb, "github", [changed])

    embedder = _RecordingEmbedder()
    assert await _drain(_backfill(bdb, embedder)) == 1
    assert embedder.calls == 1
    assert embedder.batches == [["rewritten body about pricing"]]
    row = dict((cid, (stored, emb)) for cid, stored, emb in _hashes(bdb, scope))[changed.chunk_id]
    assert row == (content_hash(changed.text), content_hash(changed.text))

    # A second pass over a clean backlog embeds nothing at all.
    again = _RecordingEmbedder()
    assert await _drain(_backfill(bdb, again)) == 0
    assert again.calls == 0


async def test_a_vector_for_superseded_content_is_never_written(bdb: MemoryDB) -> None:
    """Content that changed between the read and the write stays pending."""
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 1))
    scope = doc_scope(_AGENT, "dropbox").key
    chunk_id = _chunks("dropbox", 1)[0].chunk_id
    backend = open_index_backend("sqlite", db=bdb)
    written = await backend.set_embeddings(
        scope, [EmbeddingWrite(chunk_id=chunk_id, content_hash="old-hash", embedding=[1.0] * 8)]
    )
    assert written == 0
    assert _vector_count(bdb, scope) == 0
    assert _hashes(bdb, scope)[0][2] is None


async def test_set_embeddings_never_touches_another_scopes_chunk(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 1))
    chunk = _chunks("dropbox", 1)[0]
    backend = open_index_backend("sqlite", db=bdb)
    written = await backend.set_embeddings(
        doc_scope(_OTHER, "dropbox").key,
        [
            EmbeddingWrite(
                chunk_id=chunk.chunk_id,
                content_hash=content_hash(chunk.text),
                embedding=[1.0] * 8,
            )
        ],
    )
    assert written == 0
    assert _vector_count(bdb, doc_scope(_AGENT, "dropbox").key) == 0


# -- bounded, streaming --------------------------------------------------------


class _SpyBackend(SqliteIndexBackend):
    """Records the size of every pending-chunk page the backfill reads."""

    def __init__(self, db: MemoryDB) -> None:
        super().__init__(db)
        self.pages: list[int] = []

    async def pending_embeds(self, scope: str, *, after: str, limit: int) -> list[PendingEmbed]:
        rows = await super().pending_embeds(scope, after=after, limit=limit)
        self.pages.append(len(rows))
        return rows


async def test_backfill_streams_in_bounded_batches(bdb: MemoryDB) -> None:
    total, batch = 1000, 64
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", total))
    spy = _SpyBackend(bdb)
    embedder = _RecordingEmbedder()
    assert await _drain(_backfill(bdb, embedder, batch_size=batch, backend=spy)) == total
    assert len(embedder.batches) == math.ceil(total / batch)
    assert max(len(b) for b in embedder.batches) <= batch
    # Peak chunk texts held at once is one page, never the whole backlog.
    assert max(spy.pages) <= batch


async def test_run_batches_is_bounded_per_tick(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 100))
    embedder = _RecordingEmbedder()
    backfill = _backfill(bdb, embedder, batch_size=10)
    tick = await backfill.run_batches(3)
    assert tick == BackfillTick(batches=3, embedded=30, embedder_down=False, exhausted=False)
    assert len(embedder.batches) == 3


async def test_pending_page_reads_the_partial_index(bdb: MemoryDB) -> None:
    plan = " ".join(
        str(row[-1])
        for row in bdb.connect().execute(
            "EXPLAIN QUERY PLAN SELECT chunk_id FROM chunks WHERE scope=? AND chunk_id>? "
            "AND (embedded_hash IS NULL OR embedded_hash <> content_hash) "
            "ORDER BY chunk_id LIMIT ?",
            ("s", "", 10),
        )
    )
    assert "idx_chunks_embed_pending" in plan


# -- outages, mid-run -----------------------------------------------------------


async def test_embedder_outage_mid_run_backs_off_and_resumes(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 50))
    scope = doc_scope(_AGENT, "dropbox").key
    embedder = _FlakyEmbedder(fail_on={3})
    backfill = _backfill(bdb, embedder, batch_size=10)

    first = await backfill.run_batches(10)
    assert first.embedder_down is True
    assert first.embedded == 20
    assert first.exhausted is False

    assert await _drain(backfill) == 30
    assert all(stored == emb for _, stored, emb in _hashes(bdb, scope))
    # Nothing was embedded twice: the failed batch was retried, not skipped.
    assert sum(len(b) for b in embedder.batches) == 50


async def test_unexpected_embedder_error_never_raises(bdb: MemoryDB) -> None:
    class _Broken:
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("CUDA error: device-side assert")

    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 5))
    tick = await _backfill(bdb, _Broken()).run_batches(5)
    assert tick.embedder_down is True
    assert tick.embedded == 0


async def test_no_embedder_or_no_vec_channel_is_a_clean_no_op(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 5))
    tick = await _backfill(bdb, None).run_batches(5)
    assert tick.exhausted is True
    assert tick.embedded == 0


def test_pacer_backs_off_on_outage_and_idles_when_clear() -> None:
    pacer = BackfillPacer(busy_s=0.5, idle_s=300.0, backoff_initial_s=10.0, backoff_max_s=40.0)
    down = BackfillTick(embedder_down=True)
    assert [pacer.next_delay(down) for _ in range(4)] == [10.0, 20.0, 40.0, 40.0]
    assert pacer.next_delay(BackfillTick(batches=2, embedded=20)) == 0.5
    assert pacer.next_delay(down) == 10.0  # a working tick resets the backoff
    assert pacer.next_delay(BackfillTick(exhausted=True)) == 300.0


# -- isolation ------------------------------------------------------------------


async def test_backfill_touches_only_its_own_doc_pools(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 6))
    other_scope = doc_scope(_OTHER, "dropbox").key
    memory_scope = _AGENT
    backend = open_index_backend("sqlite", db=bdb)
    for scope, prefix in ((other_scope, "theirs"), (memory_scope, "card")):
        await backend.upsert_chunks(
            scope,
            [
                ChunkWrite(
                    chunk_id=f"{prefix}:{i}",
                    source_path=f"{prefix}/{i}.md",
                    mtime=1.0,
                    classification="unclassified",
                    content_hash=content_hash(f"{prefix} {i}"),
                    text=f"{prefix} {i} quarterly revenue",
                    embedding=None,
                )
                for i in range(4)
            ],
        )

    backfill = _backfill(bdb, _RecordingEmbedder())
    assert set(await backfill.backlog()) == {doc_scope(_AGENT, "dropbox").key}
    assert await _drain(backfill) == 6
    assert _vector_count(bdb, other_scope) == 0
    assert _vector_count(bdb, memory_scope) == 0

    probe = (await StubEmbedder(dims=_DIMS).embed_texts(["theirs 1 quarterly revenue"]))[0]
    hits = await backend.vec_search(doc_scope(_AGENT, "dropbox").key, probe, top_k=20)
    assert hits and all(hit.startswith("dropbox:") for hit in hits)


async def test_a_whole_store_backfill_covers_every_doc_pool_but_no_memory_scope(
    bdb: MemoryDB,
) -> None:
    """A shared store (or an operator run) selects by the doc-pool marker alone."""
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 3))
    principal = "did:arc:knowledge:conn-1"
    backend = open_index_backend("sqlite", db=bdb)
    await backend.upsert_chunks(
        doc_scope(principal, "src").key,
        [
            ChunkWrite(
                chunk_id="src:a#0",
                source_path="a.md",
                mtime=1.0,
                classification="unclassified",
                content_hash=content_hash("shared text"),
                text="shared text",
                embedding=None,
            )
        ],
    )
    await backend.upsert_chunks(
        _AGENT,
        [
            ChunkWrite(
                chunk_id="card:x",
                source_path="x.md",
                mtime=1.0,
                classification="unclassified",
                content_hash=content_hash("card"),
                text="card",
                embedding=None,
            )
        ],
    )
    backfill = DocEmbedBackfill(bdb, MemoryConfig(), _RecordingEmbedder(), scope_prefix="")
    assert set(await backfill.backlog()) == {
        doc_scope(_AGENT, "dropbox").key,
        doc_scope(principal, "src").key,
    }
    assert await _drain(backfill) == 4
    assert _vector_count(bdb, _AGENT) == 0


# -- one writer per index file --------------------------------------------------


def test_writer_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "ws" / "memory" / "index.db"
    db_path.parent.mkdir(parents=True)
    held = backfill_writer_lock(db_path)
    assert held.acquire() is True
    assert backfill_writer_lock(db_path).acquire() is True  # same process: shared hold
    probe = (
        "import sys; from pathlib import Path\n"
        "from arcmemory.index.backfill import backfill_writer_lock\n"
        "sys.exit(0 if backfill_writer_lock(Path(sys.argv[1])).acquire() else 3)\n"
    )
    other = subprocess.run([sys.executable, "-c", probe, str(db_path)], check=False)
    assert other.returncode == 3
    held.release()
    other = subprocess.run([sys.executable, "-c", probe, str(db_path)], check=False)
    assert other.returncode == 0


async def test_the_backfill_never_rebuilds_the_sidecar_from_scratch(bdb: MemoryDB) -> None:
    """Vectors land through the delta path: the loaded index is extended, not rebuilt."""
    scope = doc_scope(_AGENT, "dropbox").key
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 30))
    backend = open_index_backend("sqlite", db=bdb)
    probe = [1.0] * _DIMS
    await backend.vec_search(scope, probe, top_k=1)  # load the (empty) index first
    index = ann.scope_ann(bdb, scope)
    assert await index.wait_built(timeout=30.0)
    built_from = index.source
    await _drain(_backfill(bdb, _RecordingEmbedder(), batch_size=7))
    assert index.source == built_from
    assert index.mode == "hnsw"
    assert len(await backend.vec_search(scope, probe, top_k=50)) == 30


def test_pending_index_exists_on_a_fresh_db(bdb: MemoryDB) -> None:
    names = {
        row[0]
        for row in bdb.connect().execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_chunks_embed_pending" in names
    assert isinstance(bdb.connect(), sqlite3.Connection)


async def test_backfill_yields_between_batches(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 40))
    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(heartbeat())
    await _drain(_backfill(bdb, _RecordingEmbedder(), batch_size=5))
    task.cancel()
    assert beats >= 8


async def test_a_backfill_refuses_while_another_process_owns_the_file(bdb: MemoryDB) -> None:
    """Another process (the running service) holds the claim: write nothing, report it."""
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 5))
    lock_path = bdb.db_path.parent / ".embed-backfill.lock"
    with lock_path.open("a+") as other:  # a second open file = another process's lock
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        embedder = _RecordingEmbedder()
        tick = await _backfill(bdb, embedder).run_batches(5)
        assert tick == BackfillTick(blocked=True)
        assert embedder.calls == 0
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
    backfill = _backfill(bdb, _RecordingEmbedder())
    assert await _drain(backfill) == 5
    backfill_writer_lock(bdb.db_path).release()


def test_pacer_idles_while_blocked() -> None:
    assert BackfillPacer(idle_s=300.0).next_delay(BackfillTick(blocked=True)) == 300.0


async def test_the_brain_backfills_its_own_doc_pools(tmp_path: Path) -> None:
    from arcmemory.brain import ArcMemoryBrain

    workspace = tmp_path / "brain-ws"
    brain = ArcMemoryBrain(workspace, _AGENT, embedder=_WideEmbedder())
    outage_db = MemoryDB(workspace)
    try:
        await DocIndex(
            outage_db, workspace, MemoryConfig(), embedder=_DownEmbedder()
        ).index_source("dropbox", _AGENT, _chunks("dropbox", 30))
        scope = doc_scope(_AGENT, "dropbox").key
        assert (await brain.doc_embed_backlog())[scope].pending == 30
        tick = await brain.backfill_doc_embeddings(max_batches=10)
        assert tick.embedded == 30
        assert (await brain.doc_embed_backlog())[scope].pending == 0
        assert (await brain.backfill_doc_embeddings(max_batches=10)).exhausted
        assert await brain.maintain_doc_embeddings() == BackfillPacer().idle_s
    finally:
        outage_db.close()
        ann.forget_loaded_indexes()


async def test_a_connection_store_backfills_its_own_principals_pools(tmp_path: Path) -> None:
    """A shared knowledge store's port backfills the store it writes, nothing else."""
    from arcmemory.connected_data import ConnectedDataService

    principal = "did:arc:knowledge:conn-7"
    root = tmp_path / "shared" / "store-key"
    store_db = MemoryDB(root)
    try:
        await DocIndex(store_db, root, MemoryConfig(), embedder=_DownEmbedder()).index_source(
            "src", principal, _chunks("src", 12)
        )
    finally:
        store_db.close()
    service = ConnectedDataService(root, principal, approval_store=None, embedder=_WideEmbedder())
    try:
        scope = doc_scope(principal, "src").key
        assert (await service.embed_backlog())[scope].pending == 12
        tick = await service.backfill_embeddings(max_batches=4)
        assert tick.embedded == 12
        assert (await service.embed_backlog())[scope].pending == 0
    finally:
        service.close()
        ann.forget_loaded_indexes()


async def test_one_unembeddable_text_never_wedges_a_fresh_backfill(bdb: MemoryDB) -> None:
    """A bad text fails its batch; its neighbours still get vectors, every pass.

    A shared store's backfill is rebuilt per tick and so restarts its pass each
    time: skipping the whole failing batch would hit the same batch forever.
    """

    class _OneBadText(_RecordingEmbedder):
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            if any("unembeddable" in text for text in texts):
                raise RuntimeError("tokenizer crashed")
            return await super().embed_texts(texts)

    chunks = _chunks("dropbox", 10)
    chunks[2] = chunks[2].model_copy(update={"text": "unembeddable body"})
    await _index_during_outage(bdb, "dropbox", chunks)
    scope = doc_scope(_AGENT, "dropbox").key
    tick = await _backfill(bdb, _OneBadText(), batch_size=4).run_batches(10)
    assert tick.embedder_down is False
    assert tick.embedded == 9
    pending = [cid for cid, stored, emb in _hashes(bdb, scope) if stored != emb]
    assert pending == [chunks[2].chunk_id]


async def test_a_new_instance_continues_the_pass_past_a_failing_tail(bdb: MemoryDB) -> None:
    """Per-tick instances (a shared store's port) share the pass, so a pool whose
    last page cannot be embedded does not hold back the pools after it."""

    class _FailsOn(_RecordingEmbedder):
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            if any("unembeddable" in text for text in texts):
                raise RuntimeError("tokenizer crashed")
            return await super().embed_texts(texts)

    first = _chunks("aaa", 1)[0].model_copy(update={"text": "unembeddable body"})
    await _index_during_outage(bdb, "aaa", [first])
    await _index_during_outage(bdb, "bbb", _chunks("bbb", 3))
    down = await _backfill(bdb, _FailsOn()).run_batches(5)
    assert down.embedder_down is True
    resumed = await _backfill(bdb, _FailsOn()).run_batches(5)
    assert resumed.embedded == 3
    assert resumed.exhausted is True
    pending = (await _backfill(bdb, _FailsOn()).backlog())[doc_scope(_AGENT, "aaa").key]
    assert pending.pending == 1


async def test_one_backfill_per_index_file_runs_at_a_time_in_a_process(bdb: MemoryDB) -> None:
    """Two agents reading one shared store must not both embed its backlog."""
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 20))
    release = asyncio.Event()

    class _Slow(_RecordingEmbedder):
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            await release.wait()
            return await super().embed_texts(texts)

    first = asyncio.ensure_future(_backfill(bdb, _Slow(), batch_size=5).run_batches(10))
    await asyncio.sleep(0.05)
    second_embedder = _RecordingEmbedder()
    second = await _backfill(bdb, second_embedder, batch_size=5).run_batches(10)
    assert second == BackfillTick(blocked=True)
    assert second_embedder.calls == 0
    release.set()
    assert (await first).embedded == 20


async def test_maintain_runs_one_tick_and_returns_the_paced_delay(bdb: MemoryDB) -> None:
    await _index_during_outage(bdb, "dropbox", _chunks("dropbox", 30))
    backfill = _backfill(bdb, _RecordingEmbedder(), batch_size=10)
    assert await backfill.maintain(max_batches=1) == BackfillPacer().busy_s
    assert await backfill.maintain(max_batches=10) == BackfillPacer().idle_s
    assert (await backfill.backlog())[doc_scope(_AGENT, "dropbox").key].pending == 0
    await _index_during_outage(bdb, "github", _chunks("github", 3))
    assert await _backfill(bdb, _DownEmbedder()).maintain() == BackfillPacer().backoff_initial_s


async def test_a_store_port_without_write_authority_never_backfills(tmp_path: Path) -> None:
    """A reader's port (or a revoked subscriber's) writes no vector into a shared store."""
    from arcmemory.connected_data import ConnectedDataService

    class _NoGrant:
        async def authorized_homes(self) -> None:
            return None

    principal = "did:arc:knowledge:conn-9"
    root = tmp_path / "shared" / "ro"
    store_db = MemoryDB(root)
    try:
        await DocIndex(store_db, root, MemoryConfig(), embedder=_DownEmbedder()).index_source(
            "src", principal, _chunks("src", 4)
        )
    finally:
        store_db.close()
    service = ConnectedDataService(
        root, principal, approval_store=None, embedder=_WideEmbedder(), authority=_NoGrant()
    )
    try:
        assert await service.backfill_embeddings(max_batches=4) == BackfillTick(blocked=True)
        assert (await service.embed_backlog())[doc_scope(principal, "src").key].pending == 4
        assert await service.maintain_embeddings() == BackfillPacer().idle_s
    finally:
        service.close()
        ann.forget_loaded_indexes()
