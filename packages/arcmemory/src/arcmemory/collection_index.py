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
import logging
import os
import threading
import weakref
from dataclasses import replace
from pathlib import Path

from arcokf import (
    DIGEST_NAME,
    INDEX_NAME,
    FolderIndexValidation,
    IndexEntry,
    folder_entry,
    folder_summary_entry,
    listable_dir,
    listable_file,
    read_folder_digest,
    render_folder_digest,
    render_folder_index,
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

#: The files a maintainer derives at a collection root: they hold no memory data.
DERIVED_INDEX_FILES = frozenset({INDEX_NAME, DIGEST_NAME, _JOURNAL})

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
    ) -> None:
        self._root = Path(root)
        self._bundle_root = bundle_root
        self._nested = nested
        self._debounce_s = DEBOUNCE_S if debounce_s is None else debounce_s
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
        parts = [] if rel == "." else rel.split("/")
        if not all(listable_dir(part) for part in parts):
            return None
        if len(parts) >= 2 and parts[0] in self._nested:
            return None
        return "/".join(parts)

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
            await self.drain()
            if self._pending():
                await asyncio.sleep(self._debounce_s)

    async def drain(self) -> int:
        """Regenerate every dirty folder now (off-loop); return folders rewritten."""
        return await asyncio.to_thread(self.drain_sync)

    def drain_sync(self, *, force: bool = False) -> int:
        """Blocking drain, deepest folders first so parents see fresh counts.

        ``force`` re-reads every document instead of reusing the entries of the
        index already on disk.
        """
        with self._drain_lock:
            self._merge_journal()
            written = 0
            failed: set[str] = set()
            while True:
                with self._lock:
                    batch = sorted(self._dirty - failed, key=lambda rel: (-rel.count("/"), rel))
                    if not batch:
                        break
                    self._dirty.difference_update(batch)
                for rel in batch:
                    try:
                        written += self._regenerate(rel, reuse=not force)
                    except OSError:
                        _logger.warning("okf index regeneration failed for %r", rel, exc_info=True)
                        failed.add(rel)
            with self._lock:
                self._dirty |= failed
                self._compact_journal()
            return written

    def _pending(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def _regenerate(self, rel: str, *, reuse: bool = True) -> int:
        folder = self._root / rel if rel else self._root
        parent = rel.rpartition("/")[0] if "/" in rel else ""
        if not folder.is_dir():
            self._cache.pop(str(folder), None)
            if rel:
                self._mark(parent)
            return 0
        entries = self._collect(folder, reuse=reuse)
        text = render_folder_index(entries, root=self._bundle_root and rel == "")
        previous = read_folder_digest(folder)
        wrote = _write_if_changed(folder / INDEX_NAME, text)
        wrote |= _write_if_changed(
            folder / DIGEST_NAME, render_folder_digest(text, tuple(entries))
        )
        count = sum(1 if not e.is_folder else e.count for e in entries)
        if rel and (previous is None or previous.count != count):
            self._mark(parent)
        return int(wrote)

    def _collect(self, folder: Path, *, reuse: bool) -> list[IndexEntry]:
        entries: list[IndexEntry] = []
        cached = self._cache.get(str(folder), {})
        prior, prior_mtime = self._prior_entries(folder) if reuse else ({}, 0)
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

    def _prior_entries(self, folder: Path) -> tuple[dict[str, IndexEntry], int]:
        """Document entries of the trusted on-disk index, plus when it was written.

        Lets a fresh maintainer (a new process, a new sync run) read only the
        documents modified since, instead of the whole folder. Empty when the
        index does not verify, so a tampered index is never reused.
        """
        validation = validate_folder_index(folder, root=self._bundle_root and folder == self._root)
        digest = read_folder_digest(folder)
        if not validation.valid or digest is None:
            return {}, 0
        entries = {
            entry.path: replace(entry, digest=digest.docs[entry.path])
            for entry in validation.entries
            if not entry.is_folder
        }
        return entries, (folder / INDEX_NAME).stat().st_mtime_ns

    # -- self-heal ----------------------------------------------------------

    def sync_all(self, *, force: bool = False) -> int:
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
        return self.drain_sync(force=force)

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
            with self._journal.open("a", encoding="utf-8") as handle:
                handle.write(rel + "\n")
        except OSError:
            _logger.warning("okf index journal append failed", exc_info=True)

    def _merge_journal(self) -> None:
        """Adopt folders journaled by a crashed run or another process."""
        try:
            lines = self._journal.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return
        for line in lines:
            parts = [] if not line else line.split("/")
            if all(listable_dir(part) for part in parts) and not (
                len(parts) >= 2 and parts[0] in self._nested
            ):
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
    "MEMORY_NESTED_COLLECTIONS",
    "OkfIndexMaintainer",
    "memory_maintainer",
    "refresh_memory_document",
    "routing_text",
    "source_maintainer",
]
