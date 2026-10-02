"""Owning service for per-folder OKF ``index.md`` files.

Every folder under a collection root gets its own spec-shaped ``index.md``
(``arcokf``), kept current deterministically (no LLM) and incrementally:

* a store write only :meth:`~OkfIndexMaintainer.mark_dirty` s its folder — an
  in-memory set plus a one-line journal append, never an index render;
* one debounced background task regenerates each dirty folder off the event
  loop, reading only documents that changed since the last pass, and writes a
  file only when its bytes differ;
* a folder's parent is marked dirty only when the folder's summary line (its
  document count) changed, so a write rewrites the folder, and the root only
  when counts move;
* :meth:`~OkfIndexMaintainer.sync_all` is the self-heal pass (rebuild,
  consolidation): a cheap ``stat`` comparison finds folders whose index is
  missing, tampered or older than a document, and regenerates just those.

Only this module writes collection indexes. Readers verify through
``arcokf.validate_folder_index`` and fail closed on any edit behind our back.
The memory service owns ``workspace/memory/index.md`` (the bundle root, the
only index carrying ``okf_version``); every connected source owns the index at
``memory/connected/<source_id>/`` as a sub-bundle of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from arcokf import (
    CREATION,
    DEPRECATION,
    DIGEST_NAME,
    INDEX_NAME,
    LOG_DIGEST_NAME,
    LOG_NAME,
    UPDATE,
    FolderIndexValidation,
    IndexEntry,
    LogEntry,
    archive_name,
    folder_entry,
    folder_summary_entry,
    listable_dir,
    listable_file,
    merge_log_events,
    read_folder_digest,
    read_log_digest,
    read_verified_log,
    render_change_log,
    render_folder_digest,
    render_folder_index,
    render_log_digest,
    validate_folder_index,
)

from arcmemory.mdfile import atomic_write_text

_logger = logging.getLogger("arcmemory.collection_index")

#: Top-level memory subtrees that are collections of their own. Each connected
#: source keeps ``memory/connected/<source_id>/index.md`` and its own document
#: pool, maintained by its own sync run; the memory maintainer lists the sources
#: from their sidecars but never descends into them (tens of thousands of files,
#: and a per-source grant boundary).
MEMORY_NESTED_COLLECTIONS = frozenset({"connected"})

#: Seconds a burst of writes settles before its folders are regenerated.
DEBOUNCE_S = 3.0

_JOURNAL = ".dirty"

#: The dirty journal is read whole on every maintainer construction and drain, so
#: it is capped. A journal past the cap (or one that stopped growing because it
#: reached it) is dropped and replaced by one full self-heal pass.
JOURNAL_MAX_BYTES = 64 * 1024

#: The files a maintainer derives at a collection root: they hold no memory data.
DERIVED_INDEX_FILES = frozenset({INDEX_NAME, DIGEST_NAME, _JOURNAL, LOG_NAME, LOG_DIGEST_NAME})

#: ``log.md`` holds at most this many entries; past it the oldest roll into
#: per-year ``log.YYYY.md`` archives. Trimming drops to ``LOG_KEEP_ENTRIES`` so
#: an archive is rewritten once per many changes, not on every one.
LOG_MAX_ENTRIES = 500
LOG_KEEP_ENTRIES = 400

_LOG_ARCHIVE_FILE = re.compile(r"^log\.\d{4}\.md$")


def is_derived_file(name: str) -> bool:
    """Whether ``name`` is a file a maintainer derives at a collection root."""
    return name in DERIVED_INDEX_FILES or _LOG_ARCHIVE_FILE.fullmatch(name) is not None


#: Filesystem timestamps come from a coarse kernel clock, and a document can be
#: rewritten while its folder is being indexed. A document only counts as covered
#: by an index written this long after it; anything newer is read again.
_MTIME_SLACK_NS = 2_000_000_000

# A cached document entry is valid while (mtime, size, inode) are unchanged; the
# inode changes on every atomic replace, so a same-tick rewrite is still seen.
_Stat = tuple[int, int, int]


class OkfIndexMaintainer:
    """Keep every folder index under one collection root current."""

    def __init__(
        self,
        root: Path,
        *,
        bundle_root: bool = True,
        nested: frozenset[str] = frozenset(),
        debounce_s: float | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._root = Path(root)
        self._bundle_root = bundle_root
        self._nested = nested
        self._debounce_s = DEBOUNCE_S if debounce_s is None else debounce_s
        self._today = today or (lambda: datetime.now(UTC).date())
        self._events: list[LogEntry] = []
        self._resync = False
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._dirty: set[str] = set()
        self._cache: dict[str, dict[str, tuple[_Stat, IndexEntry | None]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._merge_journal()

    @property
    def index_path(self) -> Path:
        """The collection root's reserved index path."""
        return self._root / INDEX_NAME

    # -- turn path: O(1), no index rendering --------------------------------

    def mark_dirty(self, path: Path) -> None:
        """Record that the folder holding ``path`` needs a fresh index."""
        path = Path(path)
        if not (path.name.lower().endswith(".md") and path.name != INDEX_NAME):
            return
        rel = self._relative_folder(path.parent)
        if rel is not None:
            self._mark(rel)

    def mark_folder_dirty(self, folder: Path) -> None:
        """Record that ``folder`` itself needs a fresh index (a child changed)."""
        rel = self._relative_folder(Path(folder))
        if rel is not None:
            self._mark(rel)

    def _relative_folder(self, folder: Path) -> str | None:
        try:
            rel = folder.relative_to(self._root).as_posix()
        except ValueError:
            return None
        rel = "" if rel == "." else rel
        return rel if self._valid_rel(rel) else None

    def _valid_rel(self, rel: str) -> bool:
        """Whether ``rel`` names a folder this maintainer may index.

        A journal line is untrusted input: it must be a plain relative path of
        listable folder names. An absolute path, an empty part or a backslash
        would otherwise let a poisoned journal point a write outside the root.
        """
        if not rel:
            return True
        if rel.startswith("/") or "\\" in rel or "\0" in rel:
            return False
        parts = rel.split("/")
        if not all(part and listable_dir(part) for part in parts):
            return False
        return not (len(parts) >= 2 and parts[0] in self._nested)

    def _contained_folder(self, rel: str) -> Path | None:
        """The folder for ``rel`` if it stays under the root with no symlink on the way."""
        folder = self._root
        for part in rel.split("/") if rel else []:
            folder = folder / part
            if folder.is_symlink():
                return None
        if not folder.resolve().is_relative_to(self._root.resolve()):
            return None
        return folder

    def _mark(self, rel: str) -> None:
        with self._lock:
            if rel in self._dirty:
                return
            self._dirty.add(rel)
            self._append_journal(rel)
        self._ensure_task()

    def _ensure_task(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (worker thread / script): the next drain picks it up
        if self._task is None or self._task.done():
            self._task = loop.create_task(self.run())

    # -- background regeneration --------------------------------------------

    async def run(self) -> None:
        """Debounce, then drain dirty folders off the loop until none remain."""
        await asyncio.sleep(self._debounce_s)
        while self._pending():
            try:
                await self.drain()
            except Exception:  # a failed pass must not end the maintainer task
                _logger.warning("okf index drain failed", exc_info=True)
            if self._pending():
                await asyncio.sleep(self._debounce_s)

    async def drain(self) -> int:
        """Regenerate every dirty folder now (off-loop); return folders rewritten."""
        return await asyncio.to_thread(self.drain_sync)

    def drain_sync(self, *, force: bool = False, log: bool = True) -> int:
        """Blocking drain, deepest folders first so parents see fresh counts.

        ``force`` re-reads every document instead of reusing the entries of the
        index already on disk. ``log=False`` regenerates indexes without
        recording the changes in ``log.md`` (a layout move is not a change to
        the knowledge itself).
        """
        with self._drain_lock:
            self._merge_journal()
            if self._resync:
                self._resync = False
                with self._lock:
                    self._dirty.update(self._walk())
            written = 0
            failed: set[str] = set()
            try:
                while True:
                    with self._lock:
                        batch = sorted(
                            self._dirty - failed, key=lambda rel: (-rel.count("/"), rel)
                        )
                        if not batch:
                            break
                        self._dirty.difference_update(batch)
                    for rel in batch:
                        written += self._regenerate_or_fail(rel, failed, force, log)
                    self._flush_log()
            finally:
                with self._lock:
                    self._dirty |= failed
                    self._compact_journal()
            return written

    def _regenerate_or_fail(self, rel: str, failed: set[str], force: bool, log: bool) -> int:
        """Regenerate one folder; any failure leaves it journaled, never aborts the pass."""
        try:
            return self._regenerate(rel, reuse=not force, log=log)
        except Exception:  # one bad folder (or a racing writer) must not stop the rest
            _logger.warning("okf index regeneration failed for %r", rel, exc_info=True)
            failed.add(rel)
            return 0

    def _pending(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def _regenerate(self, rel: str, *, reuse: bool = True, log: bool = True) -> int:
        folder = self._contained_folder(rel) if self._valid_rel(rel) else None
        if folder is None:
            _logger.warning("okf index refused folder outside the collection: %r", rel)
            return 0
        parent = rel.rpartition("/")[0] if "/" in rel else ""
        if not folder.is_dir():
            self._cache.pop(str(folder), None)
            if rel:
                self._mark(parent)
            return 0
        previous = read_folder_digest(folder)
        prior, prior_mtime, valid = self._prior_entries(folder)
        entries = self._collect(folder, prior if reuse else {}, prior_mtime)
        text = render_folder_index(entries, root=self._bundle_root and rel == "")
        if log and (valid or not (folder / INDEX_NAME).exists()):
            self._record_changes(rel, entries, prior)
        wrote = _write_if_changed(folder / INDEX_NAME, text)
        wrote |= _write_if_changed(
            folder / DIGEST_NAME, render_folder_digest(text, tuple(entries))
        )
        count = sum(1 if not e.is_folder else e.count for e in entries)
        if rel and (previous is None or previous.count != count):
            self._mark(parent)
        return int(wrote)

    def _collect(
        self, folder: Path, prior: dict[str, IndexEntry], prior_mtime: int
    ) -> list[IndexEntry]:
        entries: list[IndexEntry] = []
        cached = self._cache.get(str(folder), {})
        fresh: dict[str, tuple[_Stat, IndexEntry | None]] = {}
        invalid = 0
        with os.scandir(folder) as scan:
            items = sorted(scan, key=lambda item: item.name)
        for item in items:
            if item.is_dir(follow_symlinks=False):
                child = read_folder_digest(Path(item.path)) if listable_dir(item.name) else None
                if child is not None:
                    entries.append(folder_summary_entry(item.name, child.count))
            elif item.is_file(follow_symlinks=False) and listable_file(item.name):
                st = item.stat(follow_symlinks=False)
                stat: _Stat = (st.st_mtime_ns, st.st_size, st.st_ino)
                hit = cached.get(item.name)
                if hit is not None and hit[0] == stat:
                    entry = hit[1]
                elif (old := prior.get(item.name)) is not None and (
                    st.st_mtime_ns + _MTIME_SLACK_NS < prior_mtime
                ):
                    entry = old  # untouched since the trusted index was written
                else:
                    entry = folder_entry(Path(item.path))
                fresh[item.name] = (stat, entry)
                if entry is None:
                    invalid += 1
                else:
                    entries.append(entry)
        self._cache[str(folder)] = fresh
        if invalid:
            _logger.warning("okf index: %d invalid document(s) not listed in %s", invalid, folder)
        return entries

    def _prior_entries(self, folder: Path) -> tuple[dict[str, IndexEntry], int, bool]:
        """Document entries of the trusted on-disk index, when it was written, and
        whether it was trusted at all.

        Lets a fresh maintainer (a new process, a new sync run) read only the
        documents modified since, instead of the whole folder. Empty when the
        index does not verify, so a tampered index is never reused.
        """
        validation = validate_folder_index(folder, root=self._bundle_root and folder == self._root)
        digest = read_folder_digest(folder)
        if not validation.valid or digest is None:
            return {}, 0, False
        index_mtime = (folder / INDEX_NAME).stat().st_mtime_ns
        if index_mtime > time.time_ns() + _MTIME_SLACK_NS:
            # A future timestamp would make every document look older than the
            # index, so a forged index could be reused indefinitely.
            return {}, 0, False
        entries = {
            entry.path: replace(entry, digest=digest.docs[entry.path])
            for entry in validation.entries
            if not entry.is_folder and entry.path in digest.docs
        }
        return entries, index_mtime, True

    # -- change log ---------------------------------------------------------

    def _record_changes(
        self, rel: str, entries: list[IndexEntry], prior: dict[str, IndexEntry]
    ) -> None:
        """Queue one log event per document created, changed or removed in a folder."""
        day = self._today().isoformat()
        prefix = f"{rel}/" if rel else ""
        current = {entry.path: entry for entry in entries if not entry.is_folder}
        for name, entry in current.items():
            before = prior.get(name)
            if before is None:
                kind = CREATION
            elif before.digest != entry.digest:
                kind = UPDATE
            else:
                continue
            self._events.append(LogEntry(day, kind, prefix + name, entry.title, entry.description))
        for name, before in prior.items():
            if name not in current:
                self._events.append(LogEntry(day, DEPRECATION, prefix + name, before.title))

    def _flush_log(self) -> None:
        """Fold queued events into ``log.md`` (and archives); one write per drain batch."""
        events, self._events = self._events, []
        if not events:
            return
        try:
            existing = self._trusted_log(LOG_NAME)
            digest = read_log_digest(self._root)
            archives = dict(digest.archives) if digest is not None else {}
            merged = merge_log_events(existing, events)
            if len(merged) > LOG_MAX_ENTRIES:
                archives = self._archive(merged[LOG_KEEP_ENTRIES:], archives)
                merged = merged[:LOG_KEEP_ENTRIES]
            text = render_change_log(merged)
            _write_if_changed(self._root / LOG_NAME, text)
            _write_if_changed(self._root / LOG_DIGEST_NAME, render_log_digest(text, archives))
        except OSError:
            _logger.warning("okf log write failed", exc_info=True)

    def _trusted_log(self, name: str) -> list[LogEntry]:
        """Entries of a log file that matches its sidecar; ``[]`` for an absent one.

        A log that exists but does not verify was edited behind our back: it is
        discarded (loudly), never merged, so a forged entry cannot be laundered
        into the signed-off history.
        """
        verified = read_verified_log(self._root, name)
        if verified is not None:
            return list(verified)
        if (self._root / name).exists():
            _logger.warning("okf %s failed verification in %s; discarding it", name, self._root)
        return []

    def _archive(self, overflow: list[LogEntry], archives: dict[str, str]) -> dict[str, str]:
        """Roll the oldest entries into their year's ``log.YYYY.md``; return the digests."""
        by_year: dict[str, list[LogEntry]] = {}
        for entry in overflow:
            by_year.setdefault(entry.day[:4], []).append(entry)
        for year, rolled in by_year.items():
            name = archive_name(year)
            held = self._trusted_log(name) if year in archives else []
            text = render_change_log(merge_log_events(held, rolled))
            _write_if_changed(self._root / name, text)
            archives[year] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return archives

    # -- self-heal ----------------------------------------------------------

    def sync_all(self, *, force: bool = False, log: bool = True) -> int:
        """Regenerate every folder whose index is missing, tampered or stale.

        A ``stat``-only comparison per folder: the index must verify against its
        sidecar, list exactly the documents and child folders on disk, and be
        newer than every document. Healthy folders are never read or rewritten.
        ``force`` regenerates every folder regardless (a caller that no longer
        trusts the on-disk indexes, e.g. one forged together with its sidecar).
        Returns the number of index files rewritten.
        """
        for rel in self._walk():
            if force or self._is_stale(rel):
                with self._lock:
                    self._dirty.add(rel)
        return self.drain_sync(force=force, log=log)

    def _walk(self) -> list[str]:
        if not self._root.is_dir():
            return []
        found = [""]
        stack: list[tuple[str, Path]] = [("", self._root)]
        while stack:
            rel, folder = stack.pop()
            with os.scandir(folder) as scan:
                for item in scan:
                    if not (item.is_dir(follow_symlinks=False) and listable_dir(item.name)):
                        continue
                    child = f"{rel}/{item.name}" if rel else item.name
                    if "/" in child and child.split("/", 1)[0] in self._nested:
                        continue
                    found.append(child)
                    stack.append((child, Path(item.path)))
        return found

    def _is_stale(self, rel: str) -> bool:
        folder = self._root / rel if rel else self._root
        digest = read_folder_digest(folder)
        index = folder / INDEX_NAME
        if digest is None or not index.is_file():
            return True
        if not self.validate(folder).valid:
            return True
        index_mtime = index.stat().st_mtime_ns
        if index_mtime > time.time_ns() + _MTIME_SLACK_NS:
            return True  # an index dated in the future is never trusted
        docs: set[str] = set()
        folders: dict[str, int] = {}
        with os.scandir(folder) as scan:
            for item in scan:
                if item.is_dir(follow_symlinks=False) and listable_dir(item.name):
                    child = read_folder_digest(Path(item.path))
                    if child is not None:
                        folders[item.name] = child.count
                elif item.is_file(follow_symlinks=False) and listable_file(item.name):
                    if item.stat(follow_symlinks=False).st_mtime_ns >= index_mtime:
                        return True
                    docs.add(item.name)
        return docs != set(digest.docs) or folders != digest.folders

    # -- verification -------------------------------------------------------

    def validate(self, folder: Path | None = None, *, deep: bool = False) -> FolderIndexValidation:
        """Verify one folder's index (the root's by default); never its children."""
        target = self._root if folder is None else Path(folder)
        is_root = self._bundle_root and target == self._root
        return validate_folder_index(target, root=is_root, deep=deep)

    def verify(self, folder: Path | None = None, *, deep: bool = False) -> bool:
        """Whether one folder's index is trusted."""
        return self.validate(folder, deep=deep).valid

    # -- crash-safe journal -------------------------------------------------

    @property
    def _journal(self) -> Path:
        return self._root / _JOURNAL

    def _append_journal(self, rel: str) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if self._journal.exists() and self._journal.stat().st_size >= JOURNAL_MAX_BYTES:
                return  # the in-memory set stays authoritative; sync_all heals a crash
            with self._journal.open("a", encoding="utf-8") as handle:
                handle.write(rel + "\n")
        except OSError:
            _logger.warning("okf index journal append failed", exc_info=True)

    def _merge_journal(self) -> None:
        """Adopt folders journaled by a crashed run or another process."""
        try:
            with self._journal.open("rb") as handle:
                raw = handle.read(JOURNAL_MAX_BYTES + 1)
        except OSError:
            return
        if len(raw) > JOURNAL_MAX_BYTES:
            # Too big to trust or replay: drop it and heal everything once instead.
            self._journal.unlink(missing_ok=True)
            self._resync = True
            return
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            if self._valid_rel(line):
                with self._lock:
                    self._dirty.add(line)

    def _compact_journal(self) -> None:
        """Rewrite the journal to the still-dirty folders (caller holds the lock)."""
        try:
            if self._dirty:
                atomic_write_text(
                    self._journal, "".join(f"{rel}\n" for rel in sorted(self._dirty))
                )
            else:
                self._journal.unlink(missing_ok=True)
        except OSError:
            _logger.warning("okf index journal compaction failed", exc_info=True)


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeError):
        pass
    atomic_write_text(path, text)
    return True


_REGISTRY: weakref.WeakValueDictionary[str, OkfIndexMaintainer] = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def memory_maintainer(mem_dir: Path) -> OkfIndexMaintainer:
    """The one maintainer for ``memory/``, so every store shares one dirty set.

    The registry is weak: a maintainer nobody holds (and with no pending task)
    is dropped, and its journal replays into the next one.
    """
    key = str(Path(mem_dir).absolute())
    with _REGISTRY_LOCK:
        maintainer = _REGISTRY.get(key)
        if maintainer is None:
            maintainer = OkfIndexMaintainer(Path(mem_dir), nested=MEMORY_NESTED_COLLECTIONS)
            _REGISTRY[key] = maintainer
        return maintainer


def source_maintainer(collection_root: Path) -> OkfIndexMaintainer:
    """The maintainer for one connected source's folder (a sub-bundle, no frontmatter)."""
    return OkfIndexMaintainer(Path(collection_root), bundle_root=False)


def refresh_memory_document(path: Path) -> None:
    """Mark one memory document's folder dirty after an owning write or delete.

    Memory stores call this after their durable write. It is O(1) and does no
    index I/O beyond a one-line journal append; the debounced maintainer does
    the regeneration off the turn path.
    """
    for parent in (path, *path.parents):
        if parent.name == "memory":
            memory_maintainer(parent).mark_dirty(path)
            return


def routing_text(index_text: str) -> str:
    """The human routing lines of a folder index: headings and listing lines only."""
    return "\n".join(line for line in index_text.splitlines() if line.startswith(("# ", "* [")))


__all__ = [
    "DEBOUNCE_S",
    "DERIVED_INDEX_FILES",
    "JOURNAL_MAX_BYTES",
    "LOG_KEEP_ENTRIES",
    "LOG_MAX_ENTRIES",
    "MEMORY_NESTED_COLLECTIONS",
    "OkfIndexMaintainer",
    "is_derived_file",
    "memory_maintainer",
    "refresh_memory_document",
    "routing_text",
    "source_maintainer",
]
