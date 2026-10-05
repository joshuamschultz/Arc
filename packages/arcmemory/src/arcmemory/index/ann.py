"""Per-scope HNSW sidecar for the SQLite vector channel (usearch).

``vec0`` stays the source of truth; this module keeps a derived, disposable
approximate-nearest-neighbour index beside ``index.db`` so a top-k vector search
over a million vectors costs about a millisecond instead of a Python scan of
every vector (which stalled the event loop past the 60 s watchdog in production).

Design, in the order a reader needs it:

* **One index per scope.** Each scope gets its own usearch index file
  (``.index-ann/<sha256(scope)>.usearch``), so a search for scope A cannot
  structurally reach scope B's vectors (LLM08). The id map back to chunks also
  filters on ``scope``, as defence in depth.
* **Keys are ``chunks.rowid``.** usearch needs integer keys. The ``chunks`` row id
  is already stable for a chunk's lifetime (its upsert is ``ON CONFLICT DO
  UPDATE``, which keeps the row id), is a B-tree point lookup to map back, and
  needs no second mapping table to keep in sync. ``vec0`` hides its own row id.
* **Version stamp.** Every transaction that changes a scope's vectors bumps
  ``vec_generation`` for that scope (:func:`mark_scope_written`). The in-memory
  index records the generation it reflects; the saved sidecar records it too.
  Writers made through ``SqliteIndexBackend`` hand their exact change (a
  :class:`VectorDelta`) to the index; anything else (a rebuild, another process)
  shows up only as a generation mismatch, which schedules a rebuild.
* **Self-heal.** A missing, unreadable, mis-sized or stale sidecar is rebuilt
  from ``vec0`` on a background thread. While that runs, searches use an exact
  numpy scan of the snapshot (bounded, off the loop); nothing here ever raises
  into a search.

All blocking work runs on threads: callers on the event loop wrap
:meth:`ScopeAnn.search` in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from arcmemory.db import open_db_connection

if TYPE_CHECKING:
    from usearch.index import Index

    from arcmemory.db import MemoryDB

_logger = logging.getLogger(__name__)

#: Bump when the on-disk layout or index parameters change: old sidecars rebuild.
_FORMAT_VERSION = 1
#: f16 storage: recall@10 matched f32 (0.98 at ef=128 on a 200k x 384 fixture) at
#: half the disk and build time; i8 topped out at 0.94, below the 0.95 floor.
_DTYPE = "f16"
_CONNECTIVITY = 16
_EXPANSION_ADD = 128
_EXPANSION_SEARCH = 128
#: Rows fetched per ``fetchmany`` while snapshotting ``vec0``: small batches keep
#: the GIL hand-offs frequent so the event loop thread is never starved.
_SCAN_BATCH = 2048
#: Bytes per write call when saving a sidecar.
_WRITE_SLICE = 8 << 20
#: How long a search waits for a cold scope's snapshot before answering ``[]``.
FALLBACK_WAIT_S = 2.0
#: A generation mismatch younger than this may be an in-process write still
#: handing over its delta; older than this, the index is stale and rebuilds.
STALE_GRACE_S = 2.0
#: Quiet period after the last change before the sidecar is written to disk.
SAVE_DELAY_S = 30.0
#: After a failed build, searches answer ``[]`` this long before retrying.
_RETRY_AFTER_S = 30.0


@dataclass
class VectorDelta:
    """One committed change to a scope's vectors, stamped ``gen_before -> gen_after``."""

    gen_before: int
    gen_after: int
    removed: list[int] = field(default_factory=list)
    added: dict[int, bytes] = field(default_factory=dict)
    reset: bool = False


def read_generation(conn: sqlite3.Connection, scope: str) -> int:
    """The scope's committed vector generation (0 before its first stamped write)."""
    row = conn.execute("SELECT generation FROM vec_generation WHERE scope=?", (scope,)).fetchone()
    return int(row[0]) if row else 0


def mark_scope_written(conn: sqlite3.Connection, scope: str) -> int:
    """Bump ``scope``'s generation inside the caller's transaction; return the new one.

    Call this in every transaction that inserts or deletes ``vec0`` rows for the
    scope (or moves a vector-bearing chunk into or out of it).
    """
    row = conn.execute(
        "INSERT INTO vec_generation (scope, generation) VALUES (?, 1) "
        "ON CONFLICT(scope) DO UPDATE SET generation = generation + 1 RETURNING generation",
        (scope,),
    ).fetchone()
    return int(row[0])


def sidecar_dir(db: MemoryDB) -> Path:
    """Directory holding every scope's sidecar, next to ``index.db``.

    Dot-prefixed: ``memory/`` is also the agent's knowledge folder, and its
    folder indexes skip hidden entries, so the sidecar never shows up as content.
    """
    return db.db_path.parent / ".index-ann"


def sidecar_paths(db: MemoryDB, scope: str) -> tuple[Path, Path]:
    """``(index file, meta file)`` for one scope. The name is a hash, never the scope."""
    stem = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    base = sidecar_dir(db) / stem
    return base.with_suffix(".usearch"), base.with_suffix(".json")


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    """Rows scaled to unit length (zero rows left at zero, so they score 0)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    unit: np.ndarray = (matrix / norms).astype(np.float32, copy=False)
    return unit


def _blobs_to_matrix(blobs: Sequence[bytes], dims: int) -> np.ndarray:
    if not blobs:
        return np.zeros((0, dims), dtype=np.float32)
    return _unit_rows(np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(-1, dims))


@functools.cache
def _usearch_index_type() -> type[Index]:
    """usearch's ``Index``, loaded only after torch's OpenMP runtime.

    usearch's ``__init__`` loads NumKong's extension with ``RTLD_GLOBAL``, which
    puts NumKong's bundled ``libgomp`` into the process-wide ELF symbol scope.
    torch loads its own ``libgomp`` the same way (``libtorch_global_deps``).
    Whichever arrives first answers every later OpenMP lookup, and torch's
    ``libgomp``/``libtorch_cpu`` bound to NumKong's copy segfault inside torch's
    import (the Azure ``arc ui`` crash loop, x86_64 glibc). Loaded the other way
    round, NumKong uses torch's runtime and both work. So when torch is
    installed it loads first, here, the single place usearch is imported
    (enforced by ``tests/architecture/test_usearch_single_loader.py``).

    Lazy, so importing arcmemory never pays for usearch or torch; a process
    that builds an ANN index also embeds its queries, which loads torch anyway.
    """
    if importlib.util.find_spec("torch") is not None:
        importlib.import_module("torch")
    from usearch.index import Index

    return Index


def _new_index(dims: int) -> Index:
    return _usearch_index_type()(
        ndim=dims,
        metric="cos",
        dtype=_DTYPE,
        connectivity=_CONNECTIVITY,
        expansion_add=_EXPANSION_ADD,
        expansion_search=_EXPANSION_SEARCH,
    )


def _build_threads() -> int:
    """Half the cores: a first-open build must not starve the serving process."""
    return max(1, (os.cpu_count() or 2) // 2)


@dataclass
class _Snapshot:
    generation: int
    keys: np.ndarray
    vectors: np.ndarray


class ScopeAnn:
    """The HNSW index (or its exact fallback) for one scope of one DB file."""

    def __init__(self, db: MemoryDB, scope: str) -> None:
        # Only the file path is kept, never the MemoryDB: a cached index must not
        # pin a caller's connection open for the life of the process.
        self._db_path = db.db_path
        self._scope = scope
        self._dims = db.dims
        self._instance_id = db.instance_id
        #: A reader's index (the main process over a store the sync worker writes):
        #: built and searched in memory, never saved; the writer keeps the sidecar.
        self._read_only = db.read_only
        self._index_path, self._meta_path = sidecar_paths(db, scope)
        self._lock = threading.Lock()
        self._index: Index | None = None
        self._generation = -1
        self._exact: _Snapshot | None = None
        self._pending: dict[int, VectorDelta] = {}
        #: Deltas seen while a rebuild runs, replayed onto the new index at install.
        self._replay: dict[int, VectorDelta] = {}
        self._loader: threading.Thread | None = None
        self._ready = threading.Event()
        self._built = threading.Event()
        self._failed_at: float | None = None
        self._behind_since: float | None = None
        self._dirty = False
        self._save_timer: threading.Timer | None = None
        #: ``"sidecar"`` when loaded from disk, ``"rebuild"`` when rebuilt from vec0.
        self.source = ""

    # -- state -------------------------------------------------------------

    @property
    def mode(self) -> str:
        """``"hnsw"``, ``"exact"`` (fallback while building) or ``"cold"``."""
        if self._index is not None:
            return "hnsw"
        return "exact" if self._exact is not None else "cold"

    async def wait_built(self, timeout: float) -> bool:
        """Await (off the loop) the HNSW index being installed. For tests/ops."""
        self.ensure_loaded()
        return await asyncio.to_thread(self._built.wait, timeout)

    def ensure_loaded(self, *, force_rebuild: bool = False) -> None:
        """Start the background load/rebuild unless one is running or built."""
        with self._lock:
            if self._loader is not None:
                return
            if self._index is not None and not force_rebuild:
                return
            if self._recently_failed() and not force_rebuild:
                return
            self._loader = threading.Thread(
                target=self._load_or_rebuild,
                args=(force_rebuild,),
                name=f"arcmemory-ann-{self._index_path.stem[:8]}",
                daemon=True,
            )
            self._loader.start()

    # -- search ------------------------------------------------------------

    def search(
        self, query: Sequence[float], top_k: int, db_generation: int
    ) -> list[tuple[int, float]]:
        """``(key, cosine similarity)`` nearest first; ``[]`` when nothing is ready.

        Blocking (waits up to :data:`FALLBACK_WAIT_S` on a cold scope): call it
        from a worker thread, never the event loop.
        """
        vector = np.asarray(query, dtype=np.float32)
        if vector.shape != (self._dims,):
            _logger.warning(
                "vector search skipped: query has %d dims, index %d", vector.size, self._dims
            )
            return []
        if not np.any(vector):
            return []  # a zero query is similar to nothing (cosine 0 everywhere)
        if self._recently_failed():
            return []
        self._check_generation(db_generation)
        deadline = time.monotonic() + FALLBACK_WAIT_S
        if not self._ready.is_set():
            self.ensure_loaded()
            self._ready.wait(FALLBACK_WAIT_S)
        exact = self._exact
        if self._index is None and exact is not None and exact.generation != db_generation:
            # The snapshot predates writes the build will fold in: prefer the
            # finished index if it lands in time, else answer from the snapshot.
            self._built.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            index = self._index
            if index is not None:
                return self._search_index(index, vector, top_k)
            exact = self._exact
        return [] if exact is None else _search_exact(exact, vector, top_k)

    def _recently_failed(self) -> bool:
        failed_at = self._failed_at
        return failed_at is not None and time.monotonic() - failed_at < _RETRY_AFTER_S

    def _search_index(
        self, index: Index, vector: np.ndarray, top_k: int
    ) -> list[tuple[int, float]]:
        if len(index) == 0 or top_k <= 0:
            return []
        matches = index.search(vector, min(top_k, len(index)))
        return [
            (int(key), 1.0 - float(dist))
            for key, dist in zip(matches.keys, matches.distances, strict=True)
        ]

    def _check_generation(self, db_generation: int) -> None:
        """Schedule a rebuild when the index no longer reflects the DB."""
        rebuild = False
        with self._lock:
            if self._index is None or db_generation == self._generation:
                self._behind_since = None
                return
            now = time.monotonic()
            if db_generation < self._generation:
                rebuild = True  # the DB went backwards: never an in-flight write
            elif self._behind_since is None:
                self._behind_since = now
            elif now - self._behind_since > STALE_GRACE_S:
                rebuild = True
        if rebuild:
            self.ensure_loaded(force_rebuild=True)

    # -- write path --------------------------------------------------------

    def apply(self, delta: VectorDelta) -> None:
        """Fold one committed write into the index (or queue it while loading)."""
        with self._lock:
            if delta.gen_after <= self._generation:
                return
            self._pending[delta.gen_before] = delta
            if self._loader is not None:
                self._replay[delta.gen_before] = delta
            if self._index is not None:
                self._drain_locked()
                self._schedule_save_locked()
                return
        # Not loaded yet: load now so the persisted sidecar keeps up with writes.
        self.ensure_loaded()

    def _drain_locked(self) -> None:
        assert self._index is not None  # noqa: S101 - caller holds the installed index
        while self._generation in self._pending:
            delta = self._pending.pop(self._generation)
            if delta.reset:
                self._index = _new_index(self._dims)
            self._apply_to_index(self._index, delta)
            self._generation = delta.gen_after
            self._dirty = True
        self._pending = {g: d for g, d in self._pending.items() if g > self._generation}

    def _apply_to_index(self, index: Index, delta: VectorDelta) -> None:
        stale = [key for key in [*delta.removed, *delta.added] if key in index]
        if stale:
            index.remove(np.asarray(stale, dtype=np.uint64))
        if delta.added:
            keys = np.fromiter(delta.added.keys(), dtype=np.uint64, count=len(delta.added))
            _add_nonzero(index, keys, _blobs_to_matrix(list(delta.added.values()), self._dims))

    # -- load / rebuild (background thread) ---------------------------------

    def _load_or_rebuild(self, force_rebuild: bool) -> None:
        try:
            conn = open_db_connection(self._db_path, read_only=self._read_only)
            try:
                loaded = None if force_rebuild else self._load_sidecar(conn)
                if loaded is not None:
                    self._install(*loaded, source="sidecar")
                    return
                snapshot = self._snapshot(conn)
            finally:
                conn.close()
            with self._lock:
                if self._index is None:
                    self._exact = snapshot
            self._ready.set()
            index = _new_index(self._dims)
            _add_nonzero(index, snapshot.keys, snapshot.vectors, threads=_build_threads())
            if not self._read_only:
                self._save(index, snapshot.generation)
            self._install(index, snapshot.generation, source="rebuild")
        except Exception:  # reason: a bad sidecar/DB must degrade search, never crash it
            _logger.exception("vector sidecar build failed for one scope; retrying later")
            with self._lock:
                self._failed_at = time.monotonic()
                self._loader = None
            self._ready.set()

    def _install(self, index: Index, generation: int, *, source: str) -> None:
        with self._lock:
            self._index = index
            self._generation = generation
            self._exact = None
            self._failed_at = None
            self._behind_since = None
            self._loader = None
            self.source = source
            self._pending = {**self._replay, **self._pending}
            self._replay = {}
            self._drain_locked()
            if self._dirty:
                self._schedule_save_locked()
        self._ready.set()
        self._built.set()

    def _snapshot(self, conn: sqlite3.Connection) -> _Snapshot:
        """Generation + every vector of this scope, read in ONE read transaction."""
        conn.execute("BEGIN")
        try:
            generation = read_generation(conn, self._scope)
            key_of: dict[str, int] = dict(
                conn.execute("SELECT chunk_id, rowid FROM chunks WHERE scope=?", (self._scope,))
            )
            # Preallocated to the scope's chunk count (an upper bound): peak memory
            # is one float32 matrix, not a list of a million blobs beside it.
            keys = np.empty(len(key_of), dtype=np.uint64)
            vectors = np.empty((len(key_of), self._dims), dtype=np.float32)
            filled = 0
            cursor = conn.execute("SELECT chunk_id, embedding FROM vec0") if key_of else None
            while cursor is not None and (rows := cursor.fetchmany(_SCAN_BATCH)):
                hits = [(key_of[cid], blob) for cid, blob in rows if cid in key_of and blob]
                if not hits:
                    continue
                end = filled + len(hits)
                keys[filled:end] = [key for key, _ in hits]
                vectors[filled:end] = np.frombuffer(
                    b"".join(blob for _, blob in hits), dtype=np.float32
                ).reshape(-1, self._dims)
                filled = end
        finally:
            conn.rollback()
        return _Snapshot(
            generation=generation, keys=keys[:filled], vectors=_unit_rows(vectors[:filled])
        )

    def _load_sidecar(self, conn: sqlite3.Connection) -> tuple[Index, int] | None:
        """The saved index when it provably reflects the DB right now, else ``None``."""
        try:
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        expected = {
            "format": _FORMAT_VERSION,
            "dims": self._dims,
            "dtype": _DTYPE,
            "instance_id": self._instance_id,
            "generation": read_generation(conn, self._scope),
        }
        if any(meta.get(name) != value for name, value in expected.items()):
            return None
        index = _new_index(self._dims)
        try:
            # Read with Python I/O (the GIL is free during the syscalls), then
            # parse the buffer: usearch's own path loader holds the GIL throughout.
            index.load(self._index_path.read_bytes())
        except Exception:  # reason: any unreadable/corrupt file means rebuild
            _logger.warning("vector sidecar unreadable; rebuilding from vec0")
            return None
        if len(index) != meta.get("count") or index.ndim != self._dims:
            return None
        return index, int(meta["generation"])

    # -- persistence -------------------------------------------------------

    def _schedule_save_locked(self) -> None:
        if self._read_only:
            return
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(SAVE_DELAY_S, self.flush)
        self._save_timer.daemon = True
        self._save_timer.start()

    def flush(self) -> None:
        """Write the index to disk now if it changed since the last save."""
        with self._lock:
            if self._read_only or self._index is None or not self._dirty:
                return
            self._save(self._index, self._generation)
            self._dirty = False

    def _save(self, index: Index, generation: int) -> None:
        """Atomically replace the sidecar pair. Failure is logged, never raised."""
        meta = {
            "format": _FORMAT_VERSION,
            "dims": self._dims,
            "dtype": _DTYPE,
            "instance_id": self._instance_id,
            "generation": generation,
            "count": len(index),
        }
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_index = self._index_path.with_suffix(".usearch.tmp")
            tmp_meta = self._meta_path.with_suffix(".json.tmp")
            buffer = index.save()  # no path: serialize to memory, write below
            if buffer is None:
                raise OSError("usearch returned no buffer to save")
            _write_buffer(tmp_index, buffer)
            tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp_index, self._index_path)
            os.replace(tmp_meta, self._meta_path)
        except OSError:
            _logger.warning("could not save vector sidecar %s", self._index_path, exc_info=True)


def _add_nonzero(index: Index, keys: np.ndarray, vectors: np.ndarray, threads: int = 0) -> None:
    """Add every non-zero vector. A zero vector has cosine 0 with any query, so
    it can never be a hit, but usearch would report it at a small distance."""
    keep = np.any(vectors, axis=1)
    if np.any(keep):
        index.add(keys[keep], vectors[keep], threads=threads)


def _write_buffer(path: Path, buffer: bytearray | bytes) -> None:
    """Write a serialized index in slices, so the GIL is released between syscalls.

    ``Index.save(path)`` writes from C++ while holding the GIL (~180 ms per 100k
    vectors measured), which would stall the event loop on every save.
    """
    view = memoryview(buffer)
    with path.open("wb") as handle:
        for offset in range(0, len(view), _WRITE_SLICE):
            handle.write(view[offset : offset + _WRITE_SLICE])
        handle.flush()
        os.fsync(handle.fileno())


def _search_exact(snapshot: _Snapshot, vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
    """Exact cosine top-k over a snapshot: one matrix-vector product, no Python loop."""
    count = len(snapshot.keys)
    if count == 0 or top_k <= 0:
        return []
    norm = float(np.linalg.norm(vector))
    sims = snapshot.vectors @ (vector / norm if norm else vector)
    k = min(top_k, count)
    top = np.argpartition(-sims, k - 1)[:k] if k < count else np.arange(count)
    top = top[np.argsort(-sims[top], kind="stable")]
    return [(int(snapshot.keys[i]), float(sims[i])) for i in top]


# Process-wide cache: every MemoryDB/backend instance over the same DB file must
# share ONE in-memory index per scope, or a write through one instance would leave
# another's copy stale. Keyed by the file's path AND its creation-time instance id,
# and by whether the handle may write: a reader's index never saves a sidecar.
_REGISTRY: dict[tuple[str, str, str, bool], ScopeAnn] = {}
_REGISTRY_LOCK = threading.Lock()


def scope_ann(db: MemoryDB, scope: str) -> ScopeAnn:
    """The shared :class:`ScopeAnn` for ``scope`` in ``db``'s file."""
    key = (str(db.db_path.resolve()), db.instance_id, scope, db.read_only)
    with _REGISTRY_LOCK:
        found = _REGISTRY.get(key)
        if found is None:
            found = _REGISTRY[key] = ScopeAnn(db, scope)
        return found


def apply_deltas(db: MemoryDB, deltas: dict[str, VectorDelta]) -> None:
    """Hand committed per-scope deltas to their indexes (blocking: off-loop only)."""
    for scope, delta in deltas.items():
        scope_ann(db, scope).apply(delta)


def mark_stale(db: MemoryDB, scope: str) -> None:
    """Rebuild ``scope``'s index now: its vectors changed outside the delta path."""
    scope_ann(db, scope).ensure_loaded(force_rebuild=True)


def forget_loaded_indexes() -> None:
    """Drop every in-memory index (as a process restart would). Unsaved changes are lost."""
    with _REGISTRY_LOCK:
        for found in _REGISTRY.values():
            if found._save_timer is not None:
                found._save_timer.cancel()
        _REGISTRY.clear()


@atexit.register
def _flush_on_exit() -> None:
    """Persist changed indexes on a clean shutdown so the next start needs no rebuild."""
    with _REGISTRY_LOCK:
        pending = list(_REGISTRY.values())
    for found in pending:
        found.flush()


def keys_to_chunk_ids(
    conn: sqlite3.Connection, scope: str, hits: list[tuple[int, float]]
) -> list[str]:
    """Map index keys back to chunk ids, nearest first, re-checking ``scope``.

    The scope filter is defence in depth (LLM08): even a stale or tampered
    sidecar can only name rows that belong to the scope being searched.
    Non-positive similarities are dropped, matching the exact-scan contract.
    """
    positive = [(key, sim) for key, sim in hits if sim > 0.0]
    if not positive:
        return []
    marks = ",".join("?" for _ in positive)
    found: dict[int, str] = dict(
        conn.execute(
            f"SELECT rowid, chunk_id FROM chunks WHERE scope=? AND rowid IN ({marks})",  # noqa: S608
            (scope, *[key for key, _ in positive]),
        ).fetchall()
    )
    ranked = sorted(
        ((sim, found[key]) for key, sim in positive if key in found),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [chunk_id for _, chunk_id in ranked]


__all__ = [
    "ScopeAnn",
    "VectorDelta",
    "apply_deltas",
    "forget_loaded_indexes",
    "keys_to_chunk_ids",
    "mark_scope_written",
    "mark_stale",
    "read_generation",
    "scope_ann",
    "sidecar_paths",
]
