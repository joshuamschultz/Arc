"""Doc-pool embed backfill: give every connected-document chunk its vector, eventually.

``DocIndex.index_source`` embeds a source's chunks once, at write time. When the
embedder cannot serve at that moment (model still loading, queue full, process
just started) the chunks are written lexical-only and nothing ever retried them:
in production one agent held 1.24M doc chunks and only 44k vectors. The agent's
own memory scope has a retry (``SurfaceIndex.index_if_needed``); document pools
never did. This module is that retry.

How it works, in the order a reader needs it:

* **The state is the hash columns.** A chunk is pending while ``embedded_hash``
  is NULL or differs from ``content_hash`` (a partial index lists exactly those).
  No cursor is persisted: after a restart the next pass simply finds what is
  still pending, so the backfill resumes where it stopped.
* **Streaming.** Pending chunks are read one scope at a time, ``batch_size`` rows
  per keyset page (``chunk_id > after``); at most one page of texts is held.
* **Vector-only writes.** ``IndexBackend.set_embeddings`` stores the vector and
  stamps ``embedded_hash`` without touching text or FTS rows, and only while the
  chunk still holds the content that was embedded. The SQLite backend hands each
  batch to the HNSW sidecar as a delta, so the index grows in place.
* **Bounded and polite.** ``run_batches`` does at most ``max_batches`` batches
  and yields between them. Its embeds are labelled ``embed:backfill``, which the
  local embed worker queues behind any ``retrieve*`` (live recall) request, so a
  turn waits for at most one in-flight batch. :class:`BackfillPacer` turns each
  tick's outcome into the next sleep (short while working, long when idle,
  exponential backoff while the embedder is down).
* **One writer per index file.** :func:`backfill_writer_lock` is an advisory
  ``flock`` the serving process holds for as long as it runs a backfill, so an
  operator's one-shot run can refuse instead of writing the same ``index.db``
  from a second process (which would force the service to rebuild its whole
  vector sidecar from ``vec0`` again and again).

Which scopes: every scope whose key starts with ``scope_prefix`` AND names a
document pool (``…:doc:…``, see :func:`arcmemory.doc_index.doc_scope`). An
agent's own memory scope is never touched here; its refresh path owns it.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TextIO

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from arcmemory.config import MemoryConfig
from arcmemory.db import PENDING_EMBED_SQL, MemoryDB
from arcmemory.index.backend import (
    EmbedBacklog,
    EmbeddingWrite,
    IndexBackend,
    PendingEmbed,
    PostgresIndexBackend,
    SqliteIndexBackend,
    open_index_backend,
)
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.source import embed_text

_logger = logging.getLogger(__name__)

#: The scope-key segment every document pool carries (``<did>:doc:<source>``).
DOC_SCOPE_MARKER = ":doc:"
#: Texts per embed call: the local embed worker's own batch cap, so one backfill
#: request is one encode and a live recall waits behind at most one of them.
DEFAULT_BATCH_SIZE = 256
#: Batches per background tick (~4k chunks, ~8 s of GPU at 490 chunks/s).
DEFAULT_TICK_BATCHES = 16
#: The embed operation label. Not ``retrieve*``, so the worker serves it after
#: any live recall query.
EMBED_OPERATION = "embed:backfill"
#: Minimum seconds between progress log lines while a pass runs.
_PROGRESS_LOG_INTERVAL_S = 60.0
_LOCK_FILE = ".embed-backfill.lock"


@dataclass(frozen=True)
class BackfillTick:
    """What one bounded slice of backfill work did."""

    batches: int = 0
    embedded: int = 0
    #: The embedder could not serve (unavailable or failing): back off.
    embedder_down: bool = False
    #: Nothing pending was left to read in this pass: idle.
    exhausted: bool = False
    #: Another process owns this index file's backfill: write nothing, idle.
    blocked: bool = False


class DocEmbedBackfill:
    """Embeds the missing/stale vectors of one index file's document pools.

    Cheap to build per tick: where the current pass has got to lives in a
    process-wide :class:`_PassState` per index file and scope prefix, so a new
    instance (a shared store's per-run port) continues the pass instead of
    restarting it, and a text that cannot be embedded is passed over until the
    next pass rather than retried on every tick.
    """

    def __init__(
        self,
        db: MemoryDB,
        config: MemoryConfig,
        embedder: Embedder | None,
        *,
        scope_prefix: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        pause_s: float = 0.0,
        backend: IndexBackend | None = None,
        actor_did: str = "",
        audit_sink: AuditSink | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(f"batch_size must be > 0, got {batch_size!r}")
        self._backend = backend or open_index_backend(config.index_backend, db=db)
        self._embedder = embedder
        self._prefix = scope_prefix
        self._batch_size = batch_size
        self._pause_s = pause_s
        self._tier = config.tier
        self._actor = actor_did or scope_prefix or "arcmemory"
        self._claim = backfill_writer_lock(db.db_path)
        self._audit: AuditSink = audit_sink or NullSink()
        self._label = _backend_label(self._backend, db)
        self._pass = _pass_state(db.db_path, scope_prefix, type(self._backend).__qualname__)

    async def backlog(self) -> dict[str, EmbedBacklog]:
        """Every document pool's total and pending chunk counts (operator progress)."""
        found = await self._backend.embed_backlog(self._prefix)
        return {scope: b for scope, b in sorted(found.items()) if DOC_SCOPE_MARKER in scope}

    async def run_batches(self, max_batches: int) -> BackfillTick:
        """Embed and store up to ``max_batches`` batches; never raises on embedder trouble.

        ``blocked`` when another process owns this index file's backfill, or
        another backfill of the same file is mid-run in this process (two agents
        reading one shared store must not both embed its backlog).
        """
        if self._embedder is None or not self._backend.vec_available:
            return BackfillTick(exhausted=True)
        if self._pass.running or not self._hold_claim():
            return BackfillTick(blocked=True)
        self._pass.running = True
        try:
            return await self._run(max_batches)
        finally:
            self._pass.running = False

    async def maintain(self, max_batches: int = DEFAULT_TICK_BATCHES) -> float:
        """One bounded tick for a background loop; returns the seconds to wait next."""
        return self.next_delay(await self.run_batches(max_batches))

    def next_delay(self, tick: BackfillTick) -> float:
        """The pause after ``tick``, from this index file's shared :class:`BackfillPacer`."""
        return self._pass.pacer.next_delay(tick)

    async def _run(self, max_batches: int) -> BackfillTick:
        batches = embedded = 0
        refreshed = False
        while batches < max_batches:
            page = await self._next_page(allow_refresh=not refreshed)
            refreshed = True
            if page is None:
                self._finish_pass()
                return BackfillTick(batches=batches, embedded=embedded, exhausted=True)
            scope, items = page
            written = await self._embed_page(scope, items)
            if written is None:
                return BackfillTick(batches=batches, embedded=embedded, embedder_down=True)
            batches += 1
            embedded += written
            self._note_progress(written)
            # Yield (and optionally pause) so turns interleave with the backfill.
            await asyncio.sleep(self._pause_s)
        return BackfillTick(batches=batches, embedded=embedded)

    def _hold_claim(self) -> bool:
        """Claim the index file for this process; False while another process owns it.

        The claim outlives this instance (see :class:`BackfillWriterLock`): a
        serving process keeps the file for as long as it runs, so an operator's
        one-shot run is refused that whole time instead of slipping in between
        two ticks.
        """
        if self._claim.acquire():
            return True
        _logger.info("embed backfill: %s is claimed by another process", self._claim)
        return False

    async def _next_page(self, *, allow_refresh: bool) -> tuple[str, list[PendingEmbed]] | None:
        """The next non-empty page of the current pass, starting a new pass at most once."""
        while True:
            if not self._pass.queue:
                if not allow_refresh or not await self._start_pass():
                    return None
                allow_refresh = False
            scope = self._pass.queue[0]
            items = await self._backend.pending_embeds(
                scope, after=self._pass.cursors.get(scope, ""), limit=self._batch_size
            )
            if items:
                return scope, items
            self._pass.queue.pop(0)

    async def _start_pass(self) -> bool:
        """Queue every pool with pending chunks; False when there is none."""
        backlog = {scope: b for scope, b in (await self.backlog()).items() if b.pending}
        self._pass.queue = list(backlog)
        self._pass.cursors.clear()
        self._pass.embedded = 0
        self._pass.pending = sum(b.pending for b in backlog.values())
        self._report_pass_start(len(backlog))
        if not backlog:
            return False
        self._emit("started", pending=self._pass.pending, embedded=0, pools=len(backlog))
        self._pass.last_log = time.monotonic()
        return True

    def _report_pass_start(self, pools: int) -> None:
        """Name the store and its backlog at the start of a pass, so a wrong store shows.

        Every pass with work is logged; an empty one only when the backlog just
        changed (startup, or a backlog that drained), so an idle loop stays quiet
        but a backfill looking at an empty store says so once.
        """
        if self._pass.pending == 0 and self._pass.last_reported == 0:
            return
        self._pass.last_reported = self._pass.pending
        _logger.info(
            "embed backfill pass on %s: %d chunk(s) pending across %d document pool(s) under %r",
            self._label,
            self._pass.pending,
            pools,
            self._prefix,
        )

    async def _embed_page(self, scope: str, items: list[PendingEmbed]) -> int | None:
        """Embed and store one page; ``None`` when the embedder could not serve it.

        An unavailable embedder (``None`` from the degrade funnel) leaves the page
        to be retried after a backoff. Any other failure is retried one text at a
        time, so a single unembeddable text costs only itself: its neighbours get
        their vectors, it stays pending, and the pass moves past it.
        """
        texts = [embed_text(item.text) for item in items]
        try:
            vectors = await embed_or_none(self._embedder, texts, operation=EMBED_OPERATION)
        except Exception as exc:  # reason: a failing embedder degrades, never crashes its host
            _logger.warning("embed backfill: batch in %r failed (%s); per text now", scope, exc)
            return await self._embed_one_by_one(scope, items)
        if vectors is None or len(vectors) != len(items):
            return None
        return await self._store(scope, list(zip(items, vectors, strict=True)))

    async def _embed_one_by_one(self, scope: str, items: list[PendingEmbed]) -> int | None:
        """Embed a failed page text by text, skipping the texts that fail.

        ``None`` (back off) when the embedder is unavailable, or when no text at
        all could be embedded: that looks like a broken embedder, so the loop
        waits. The pass still moves past such a page, so it is retried on the
        next pass and never holds back the pages and pools after it.
        """
        embedded: list[tuple[PendingEmbed, list[float]]] = []
        for item in items:
            try:
                vectors = await embed_or_none(
                    self._embedder, [embed_text(item.text)], operation=EMBED_OPERATION
                )
            except Exception:  # reason: one bad text is skipped, never fatal
                _logger.warning("embed backfill: chunk %s cannot be embedded", item.chunk_id)
                continue
            if vectors is None or len(vectors) != 1:
                return None
            embedded.append((item, vectors[0]))
        if not embedded:
            self._pass.cursors[scope] = items[-1].chunk_id
            return None
        return await self._store(scope, embedded)

    async def _store(self, scope: str, embedded: list[tuple[PendingEmbed, list[float]]]) -> int:
        """Write the vectors and move the pass past this page."""
        written = await self._backend.set_embeddings(
            scope,
            [
                EmbeddingWrite(chunk_id=item.chunk_id, content_hash=item.content_hash, embedding=v)
                for item, v in embedded
            ],
        )
        self._pass.cursors[scope] = max(item.chunk_id for item, _ in embedded)
        return written

    def _note_progress(self, written: int) -> None:
        self._pass.embedded += written
        now = time.monotonic()
        if now - self._pass.last_log < _PROGRESS_LOG_INTERVAL_S:
            return
        self._pass.last_log = now
        _logger.info(
            "embed backfill: %d of %d pending chunk(s) embedded under %r",
            self._pass.embedded,
            self._pass.pending,
            self._prefix,
        )

    def _finish_pass(self) -> None:
        if not self._pass.pending:
            return
        _logger.info(
            "embed backfill: pass complete, %d chunk(s) embedded under %r",
            self._pass.embedded,
            self._prefix,
        )
        self._emit("completed", pending=self._pass.pending, embedded=self._pass.embedded)
        self._pass.pending = 0

    def _emit(self, phase: str, *, pending: int, embedded: int, pools: int = 0) -> None:
        extra = {"pending": str(pending), "embedded": str(embedded), "store": self._label}
        if pools:
            extra["pools"] = str(pools)
        emit(
            AuditEvent(
                actor_did=self._actor,
                action=f"memory.embed_backfill.{phase}",
                target=self._prefix or "*",
                outcome="allow",
                tier=self._tier,
                extra=extra,
            ),
            self._audit,
        )


@dataclass
class BackfillPacer:
    """How long a background loop sleeps after each tick.

    * work done, more remains: ``busy_s`` (a short breather between slices);
    * nothing pending, or another process owns the file: ``idle_s``;
    * embedder down: exponential backoff from ``backoff_initial_s`` up to
      ``backoff_max_s``, reset by the next tick that embeds anything.
    """

    busy_s: float = 0.5
    idle_s: float = 300.0
    backoff_initial_s: float = 30.0
    backoff_max_s: float = 900.0
    _failures: int = 0

    def next_delay(self, tick: BackfillTick) -> float:
        if tick.embedder_down:
            delay = min(self.backoff_initial_s * float(2**self._failures), self.backoff_max_s)
            self._failures += 1
            return delay
        self._failures = 0
        return self.idle_s if tick.exhausted or tick.blocked else self.busy_s


def _backend_label(backend: IndexBackend, db: MemoryDB) -> str:
    """Which store a backfill works on, for logs and audit (never a DSN or secret)."""
    if isinstance(backend, SqliteIndexBackend):
        return f"sqlite {db.db_path}"
    if isinstance(backend, PostgresIndexBackend):
        return "postgres"
    return type(backend).__name__


@dataclass
class _PassState:
    """Where one index file's backfill pass has got to (one per file and prefix)."""

    #: Pools still to visit in this pass, in order.
    queue: list[str] = field(default_factory=list)
    #: Per pool, the last chunk id this pass has handled.
    cursors: dict[str, str] = field(default_factory=dict)
    pending: int = 0
    embedded: int = 0
    last_log: float = 0.0
    #: A backfill of this file is mid-run in this process.
    running: bool = False
    #: The backlog the last pass-start line reported (``None`` before the first).
    last_reported: int | None = None
    pacer: BackfillPacer = field(default_factory=BackfillPacer)


_PASSES: dict[tuple[Path, str, str], _PassState] = {}
_PASSES_GUARD = threading.Lock()


def _pass_state(db_path: Path, scope_prefix: str, backend_kind: str) -> _PassState:
    """The pass of one store: an index file's path, a scope prefix and a backend kind.

    The backend kind is part of the key: a Brain on Postgres and a connected-data
    port on the workspace SQLite file share a path and prefix but not a store, and
    must neither share a queue nor block each other.
    """
    key = (Path(db_path).resolve(), scope_prefix, backend_kind)
    with _PASSES_GUARD:
        state = _PASSES.get(key)
        if state is None:
            state = _PASSES[key] = _PassState()
        return state


class BackfillWriterLock:
    """An advisory, process-exclusive claim on backfilling one index file.

    A claim belongs to the PROCESS, not to a caller: once taken it is kept until
    :meth:`release` or process exit, so a serving process that backfilled a file
    once keeps it for its whole life (its sync ports come and go per run; the
    claim must not lapse between them). ``flock`` locks belong to an open file,
    so every claim in one process goes through one shared handle per file (see
    :func:`backfill_writer_lock`): every agent of a fleet process shares it, a
    second process is refused.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None
        self._guard = threading.Lock()

    def __str__(self) -> str:
        return str(self._path.parent.parent)

    def acquire(self) -> bool:
        """Claim the file without blocking; True when this process holds it."""
        with self._guard:
            if self._handle is not None:
                return True
            handle = self._open()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return False
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            self._handle = handle
            return True

    def release(self) -> None:
        """Give the file up (an operator's one-shot run, at its end). Idempotent."""
        with self._guard:
            if self._handle is None:
                return
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def _open(self) -> TextIO:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return self._path.open("a+", encoding="utf-8")


_LOCKS: dict[Path, BackfillWriterLock] = {}
_LOCKS_GUARD = threading.Lock()


def backfill_writer_lock(db_path: Path) -> BackfillWriterLock:
    """The process-wide backfill claim for the index file at ``db_path``."""
    path = (Path(db_path).parent / _LOCK_FILE).resolve()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = _LOCKS[path] = BackfillWriterLock(path)
        return lock


def backfill_owner(db_path: Path) -> int | None:
    """The PID of the process that owns this index file's backfill, ``None`` when unclaimed.

    Probes with a second open file, so it answers for any process, this one
    included (a claim this process holds reports this process's PID).
    """
    lock_path = Path(db_path).parent / _LOCK_FILE
    try:
        handle = lock_path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            text = handle.read().strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None


def read_embed_backlog(db_path: Path) -> dict[str, EmbedBacklog]:
    """Every document pool's coverage in one SQLite index file, read-only.

    For an operator watching the backfill: opens the file read-only (no schema
    work, no writes), so it is safe beside the serving process. Blocking; ``{}``
    when the file does not exist.
    """
    if not Path(db_path).is_file():
        return {}
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        totals = conn.execute(
            "SELECT scope, COUNT(*) FROM chunks WHERE instr(scope, ?) > 0 GROUP BY scope",
            (DOC_SCOPE_MARKER,),
        ).fetchall()
        pending = dict(conn.execute(_READ_PENDING, (DOC_SCOPE_MARKER,)).fetchall())
    finally:
        conn.close()
    return {
        str(scope): EmbedBacklog(total=int(total), pending=int(pending.get(scope, 0)))
        for scope, total in sorted(totals)
    }


_READ_PENDING = (
    "SELECT scope, COUNT(*) FROM chunks "  # noqa: S608 - interpolates only a module constant
    f"WHERE instr(scope, ?) > 0 AND {PENDING_EMBED_SQL} GROUP BY scope"
)


@dataclass(frozen=True)
class BackfillOutcome:
    """How an operator's one-shot run of one store ended."""

    #: ``done`` (nothing left), ``blocked`` (another process owns the store) or
    #: ``embedder_down`` (it stayed unavailable through every retry).
    status: Literal["done", "blocked", "embedder_down"]
    embedded: int


async def backfill_store(
    workspace: Path,
    config: MemoryConfig,
    embedder: Embedder | None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_progress: Callable[[int], None] | None = None,
    retries: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BackfillOutcome:
    """Backfill every document pool of one SQLite index file to completion (operator run).

    Refuses (``blocked``) while another process owns the store, writing nothing.
    Waits out an embedder outage with the usual backoff, at most ``retries``
    times in a row. The claim is released at the end, so the service can take
    the store over when it next starts. ``on_progress`` gets the running total
    of vectors written after each tick.
    """
    db = MemoryDB(workspace)
    # The run is over this one index FILE (its readout, owner and claim are all
    # file-based), so it reads and writes that file, whatever index backend the
    # config names for the agent's own memory.
    backfill = DocEmbedBackfill(
        db,
        config,
        embedder,
        scope_prefix="",
        batch_size=batch_size,
        backend=SqliteIndexBackend(db),
    )
    embedded = failures = 0
    try:
        while True:
            tick = await backfill.run_batches(DEFAULT_TICK_BATCHES)
            embedded += tick.embedded
            if on_progress is not None and tick.embedded:
                on_progress(embedded)
            if tick.blocked:
                return BackfillOutcome(status="blocked", embedded=embedded)
            if tick.exhausted:
                return BackfillOutcome(status="done", embedded=embedded)
            failures = failures + 1 if tick.embedder_down else 0
            if failures > retries:
                return BackfillOutcome(status="embedder_down", embedded=embedded)
            if tick.embedder_down:
                await sleep(backfill.next_delay(tick))
    finally:
        # Only this process's own claim is released (a no-op when it never held one).
        backfill_writer_lock(db.db_path).release()
        db.close()


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_TICK_BATCHES",
    "DOC_SCOPE_MARKER",
    "EMBED_OPERATION",
    "BackfillOutcome",
    "BackfillPacer",
    "BackfillTick",
    "BackfillWriterLock",
    "DocEmbedBackfill",
    "backfill_owner",
    "backfill_store",
    "backfill_writer_lock",
    "read_embed_backlog",
]
