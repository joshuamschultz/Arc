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
from dataclasses import dataclass, replace
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
    LogDigest,
    LogEntry,
    archive_name,
    folder_entry,
    folder_summary_entry,
    listable_dir,
    listable_file,
    merge_log_events,
    parse_log_digest,
    read_folder_digest,
    read_regular_file,
    read_verified_log,
    render_change_log,
    render_folder_digest,
    render_folder_index,
    render_log_digest,
    validate_folder_index,
)

from arcmemory.mdfile import atomic_write_text
from arcmemory.okf_seal import SEAL_NAME, CollectionSeal, Seal, SealPending, sha256_hex

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
DERIVED_INDEX_FILES = frozenset(
    {INDEX_NAME, DIGEST_NAME, _JOURNAL, LOG_NAME, LOG_DIGEST_NAME, SEAL_NAME}
)

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


@dataclass(frozen=True, slots=True)
class _FolderPlan:
    """One folder's next index and sidecar, rendered in memory until the commit."""

    folder: Path
    text: str
    sidecar: str
    count: int
    entries: list[IndexEntry]


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
        self._labels: dict[str, tuple[_Stat, str]] = {}
        self._seal = CollectionSeal(self._root)
        self._warned_unsigned = False
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

        Every folder is rendered in memory first; the drain then commits once:
        a signed seal naming the new sidecars and the pending log events, the
        files, then the final seal. One signature pair per drain, off the loop,
        and a crash at any point is replayed from the seal, never lost.
        """
        with self._drain_lock:
            self._merge_journal()
            self._recover_pending()
            if self._resync:
                self._resync = False
                with self._lock:
                    self._dirty.update(self._walk())
            plans: dict[str, _FolderPlan] = {}
            removed: set[str] = set()
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
                        self._regenerate_or_fail(rel, failed, force, log, plans, removed)
                return self._commit(plans, removed)
            except BaseException:
                failed.update(plans)  # nothing committed: every planned folder stays journaled
                self._events = []
                raise
            finally:
                with self._lock:
                    self._dirty |= failed
                    self._compact_journal()

    def _regenerate_or_fail(
        self,
        rel: str,
        failed: set[str],
        force: bool,
        log: bool,
        plans: dict[str, _FolderPlan],
        removed: set[str],
    ) -> None:
        """Plan one folder; any failure leaves it journaled, never aborts the pass."""
        try:
            self._regenerate(rel, reuse=not force, log=log, plans=plans, removed=removed)
        except Exception:  # one bad folder (or a racing writer) must not stop the rest
            _logger.warning("okf index regeneration failed for %r", rel, exc_info=True)
            failed.add(rel)

    def _pending(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def _regenerate(
        self,
        rel: str,
        *,
        reuse: bool,
        log: bool,
        plans: dict[str, _FolderPlan],
        removed: set[str],
    ) -> None:
        """Render one folder's index and sidecar in memory (written at commit)."""
        folder = self._contained_folder(rel) if self._valid_rel(rel) else None
        if folder is None:
            _logger.warning("okf index refused folder outside the collection: %r", rel)
            return
        parent = rel.rpartition("/")[0] if "/" in rel else ""
        if not folder.is_dir():
            self._cache.pop(str(folder), None)
            plans.pop(rel, None)
            removed.add(rel)
            if rel:
                self._mark(parent)
            return
        removed.discard(rel)
        earlier = plans.get(rel)
        previous_count: int | None
        if earlier is not None:  # planned again this drain: diff against that plan
            prior = {e.path: e for e in earlier.entries if not e.is_folder}
            prior_mtime, valid, previous_count = 0, True, earlier.count
        else:
            validation = self.validate(folder)
            prior, prior_mtime, valid = self._prior_entries(folder, validation)
            previous_count = validation.digest.count if validation.digest is not None else None
        entries = self._collect(rel, folder, prior if reuse else {}, prior_mtime, plans, removed)
        text = render_folder_index(entries, root=self._bundle_root and rel == "")
        if log and (valid or not (folder / INDEX_NAME).exists()):
            self._record_changes(rel, entries, prior)
        count = sum(1 if not e.is_folder else e.count for e in entries)
        plans[rel] = _FolderPlan(
            folder, text, render_folder_digest(text, tuple(entries)), count, entries
        )
        if rel and previous_count != count:
            self._mark(parent)

    def _collect(
        self,
        rel: str,
        folder: Path,
        prior: dict[str, IndexEntry],
        prior_mtime: int,
        plans: dict[str, _FolderPlan],
        removed: set[str],
    ) -> list[IndexEntry]:
        entries: list[IndexEntry] = []
        cached = self._cache.get(str(folder), {})
        fresh: dict[str, tuple[_Stat, IndexEntry | None]] = {}
        invalid = 0
        with os.scandir(folder) as scan:
            items = sorted(scan, key=lambda item: item.name)
        for item in items:
            if item.is_dir(follow_symlinks=False):
                if not listable_dir(item.name):
                    continue
                child_rel = f"{rel}/{item.name}" if rel else item.name
                count = _child_count(child_rel, Path(item.path), plans, removed)
                if count is not None:
                    entries.append(folder_summary_entry(item.name, count))
            elif item.is_file(follow_symlinks=False) and listable_file(item.name):
                entry = self._document_entry(item, cached, fresh, prior, prior_mtime)
                if entry is None:
                    invalid += 1
                else:
                    entries.append(entry)
        self._cache[str(folder)] = fresh
        if invalid:
            _logger.warning("okf index: %d invalid document(s) not listed in %s", invalid, folder)
        return entries

    @staticmethod
    def _document_entry(
        item: os.DirEntry[str],
        cached: dict[str, tuple[_Stat, IndexEntry | None]],
        fresh: dict[str, tuple[_Stat, IndexEntry | None]],
        prior: dict[str, IndexEntry],
        prior_mtime: int,
    ) -> IndexEntry | None:
        st = item.stat(follow_symlinks=False)
        stat: _Stat = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = cached.get(item.name)
        if hit is not None and hit[0] == stat:
            entry = hit[1]
        elif (old := prior.get(item.name)) is not None and (
            st.st_mtime_ns + _MTIME_SLACK_NS < prior_mtime
        ):
            entry = old  # untouched since the trusted (signed) index was written
        else:
            # One open, never through a symlink, and only the inode scandir listed.
            entry = folder_entry(Path(item.path), expect=st)
        fresh[item.name] = (stat, entry)
        return entry

    def _prior_entries(
        self, folder: Path, validation: FolderIndexValidation
    ) -> tuple[dict[str, IndexEntry], int, bool]:
        """Document entries of the trusted on-disk index, when it was written, and
        whether it was trusted at all.

        Lets a fresh maintainer (a new process, a new sync run) read only the
        documents modified since, instead of the whole folder. Empty when the
        index is not signed by the agent, so a forged or tampered index is never
        reused and its folder is regenerated from the documents.
        """
        digest = validation.digest
        if not validation.valid or digest is None:
            return {}, 0, False
        try:
            index_mtime = (folder / INDEX_NAME).lstat().st_mtime_ns
        except OSError:
            return {}, 0, False
        if index_mtime > time.time_ns() + _MTIME_SLACK_NS:
            # A future timestamp would make every document look older than the
            # index, so even a signed index is not reused past it.
            return {}, 0, False
        entries = {
            entry.path: replace(entry, digest=digest.docs[entry.path])
            for entry in validation.entries
            if not entry.is_folder and entry.path in digest.docs
        }
        return entries, index_mtime, True

    # -- commit: signed intent, then files, then the final seal -------------

    def _commit(self, plans: dict[str, _FolderPlan], removed: set[str]) -> int:
        """Write this drain's folders and log under the agent's seal; return folders written."""
        events, self._events = self._events, []
        if not self._seal.can_sign:
            self._refuse_unsigned(plans)
            return 0
        seal = self._seal.load()
        if not plans and not removed and not events and seal is not None:
            return 0
        committed = {k: v for k, v in (seal.folders if seal else {}).items() if k not in removed}
        committed_log = seal.log if seal else ""
        new_shas = {rel: sha256_hex(plan.sidecar.encode("utf-8")) for rel, plan in plans.items()}
        log_files = self._plan_log(events, seal) if events else {}
        new_log = (
            sha256_hex(log_files[LOG_DIGEST_NAME].encode("utf-8")) if log_files else committed_log
        )
        if log_files:
            # Journal-first: the signed intent names every file about to change and
            # carries the log events, so a crash after the indexes replays the log.
            pending = SealPending(new_shas, new_log, tuple(events))
            seal = self._seal.write(committed, committed_log, pending, seal)
        written = 0
        for plan in plans.values():
            wrote = _write_if_changed(plan.folder / INDEX_NAME, plan.text)
            wrote |= _write_if_changed(plan.folder / DIGEST_NAME, plan.sidecar)
            written += int(wrote)
        self._write_log_files(log_files)
        final = {**committed, **new_shas}
        if (
            seal is None
            or seal.pending is not None
            or seal.folders != final
            or seal.log != new_log
        ):
            self._seal.write(final, new_log, None, seal)
        return written

    def _write_log_files(self, files: dict[str, str]) -> None:
        """Write the log and archives, then the sidecar that commits to them."""
        for name, text in files.items():
            if name != LOG_DIGEST_NAME:
                _write_if_changed(self._root / name, text)
        if LOG_DIGEST_NAME in files:
            _write_if_changed(self._root / LOG_DIGEST_NAME, files[LOG_DIGEST_NAME])

    def _refuse_unsigned(self, plans: dict[str, _FolderPlan]) -> None:
        """No agent key in this process: write nothing a reader could be asked to trust."""
        if plans and not self._warned_unsigned:
            self._warned_unsigned = True
            _logger.warning(
                "okf index: no agent signing key bound for %s; indexes not written "
                "(the agent's own heal pass regenerates and signs them)",
                self._root,
            )

    def _recover_pending(self) -> None:
        """Finish a drain that crashed after its signed intent: replay the log, re-seal."""
        if not self._seal.can_sign:
            return
        seal = self._seal.load()
        if seal is None or seal.pending is None:
            return
        pending = seal.pending
        new_log = seal.log
        if _sidecar_sha(self._root / LOG_DIGEST_NAME) == pending.log:
            new_log = pending.log
        elif pending.events:
            files = self._plan_log(list(pending.events), seal, committed_only=True)
            self._write_log_files(files)
            new_log = sha256_hex(files[LOG_DIGEST_NAME].encode("utf-8"))
            _logger.warning("okf log: replayed %d event(s) after a crash", len(pending.events))
        folders = dict(seal.folders)
        for rel, sha in pending.folders.items():
            folder = self._root / rel if rel else self._root
            if _sidecar_sha(folder / DIGEST_NAME) == sha:
                folders[rel] = sha
        self._seal.write(folders, new_log, None, seal)

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

    def _plan_log(
        self, events: list[LogEntry], seal: Seal | None, *, committed_only: bool = False
    ) -> dict[str, str]:
        """Render ``log.md`` (and any archive) with ``events`` folded in; nothing written.

        The result maps file name to text and always holds ``.log.digest``. The
        existing log is merged only when its sidecar is signed by the agent.
        """
        digest = self._trusted_log_digest(seal, committed_only=committed_only)
        existing = self._trusted_log(LOG_NAME, digest)
        archives = dict(digest.archives) if digest is not None else {}
        merged = merge_log_events(existing, events)
        files: dict[str, str] = {}
        if len(merged) > LOG_MAX_ENTRIES:
            archives = self._archive(merged[LOG_KEEP_ENTRIES:], archives, digest, files)
            merged = merged[:LOG_KEEP_ENTRIES]
        text = render_change_log(merged)
        files[LOG_NAME] = text
        files[LOG_DIGEST_NAME] = render_log_digest(text, archives)
        return files

    def _trusted_log_digest(self, seal: Seal | None, *, committed_only: bool) -> LogDigest | None:
        """The ``.log.digest`` on disk if the agent's seal vouches for it, else ``None``."""
        try:
            raw = read_regular_file(self._root / LOG_DIGEST_NAME)
        except OSError:
            return None
        sha = sha256_hex(raw)
        trusted = seal is not None and (
            seal.log == sha if committed_only else seal.trusts_log(sha)
        )
        if not trusted:
            _logger.warning(
                "okf log sidecar in %s is not signed by the agent; discarded", self._root
            )
            return None
        try:
            return parse_log_digest(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None

    def _trusted_log(self, name: str, digest: LogDigest | None) -> list[LogEntry]:
        """Entries of a log file that matches its signed sidecar; ``[]`` otherwise.

        A log that exists but does not verify was edited behind our back: it is
        discarded (loudly), never merged, so a forged entry cannot be laundered
        into the signed-off history.
        """
        verified = None if digest is None else read_verified_log(self._root, name, digest=digest)
        if verified is not None:
            return list(verified)
        if digest is not None and (self._root / name).exists():
            _logger.warning("okf %s failed verification in %s; discarding it", name, self._root)
        return []

    def _archive(
        self,
        overflow: list[LogEntry],
        archives: dict[str, str],
        digest: LogDigest | None,
        files: dict[str, str],
    ) -> dict[str, str]:
        """Roll the oldest entries into their year's ``log.YYYY.md``; return the digests."""
        by_year: dict[str, list[LogEntry]] = {}
        for entry in overflow:
            by_year.setdefault(entry.day[:4], []).append(entry)
        for year, rolled in by_year.items():
            name = archive_name(year)
            held = self._trusted_log(name, digest) if year in archives else []
            text = render_change_log(merge_log_events(held, rolled))
            files[name] = text
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
        """Missing, unsigned, forged, replayed, tampered, or older than a document."""
        folder = self._root / rel if rel else self._root
        validation = self.validate(folder)
        digest = validation.digest
        if not validation.valid or digest is None:
            return True
        try:
            index_mtime = (folder / INDEX_NAME).lstat().st_mtime_ns
        except OSError:
            return True
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
        """Verify one folder's index (the root's by default); never its children.

        Beyond the OKF checks, the folder's sidecar must be one the agent signed:
        its hash is in this collection's seal, which verifies against the pinned
        agent key. An unkeyed sidecar recomputed by anyone else, an older signed
        pair replayed, or a process with no pinned key all fail closed. The
        result carries the exact verified text and sidecar for the caller to use.
        """
        target = self._root if folder is None else Path(folder)
        is_root = self._bundle_root and target == self._root
        validation = validate_folder_index(target, root=is_root, deep=deep)
        if not validation.valid:
            return validation
        try:
            rel = target.relative_to(self._root).as_posix()
        except ValueError:
            return FolderIndexValidation(False, error="folder is outside this collection")
        seal = self._seal.load()
        if seal is None:
            return FolderIndexValidation(False, error="no verified agent seal for this collection")
        if not seal.trusts_folder("" if rel == "." else rel, validation.sidecar_sha):
            return FolderIndexValidation(False, error="folder index is not signed by the agent")
        return validation

    def document_labels(self, folder: Path, names: list[str]) -> dict[str, str]:
        """Each listed document's classification, read from the document itself.

        A routing chunk is gated on these labels, never on its index lines.
        Cached by ``(mtime, size, inode)`` so an unchanged document is not
        re-read; a document that is missing, a symlink or not valid OKF is left
        out (the caller treats it as unlabeled).
        """
        labels: dict[str, str] = {}
        for name in names:
            path = folder / name
            try:
                st = path.lstat()
            except OSError:
                continue
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
            hit = self._labels.get(str(path))
            if hit is not None and hit[0] == key:
                labels[name] = hit[1]
                continue
            entry = folder_entry(path, expect=st)
            if entry is None:
                continue
            self._labels[str(path)] = (key, entry.classification)
            labels[name] = entry.classification
        return labels

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
        if read_regular_file(path) == text.encode("utf-8"):
            return False
    except OSError:
        pass
    atomic_write_text(path, text)
    return True


def _sidecar_sha(path: Path) -> str:
    """SHA-256 of a sidecar's bytes, or ``""`` when it is absent or unsafe."""
    try:
        return sha256_hex(read_regular_file(path))
    except OSError:
        return ""


def _child_count(
    child_rel: str, child: Path, plans: dict[str, _FolderPlan], removed: set[str]
) -> int | None:
    """A child folder's document count: this drain's plan first, else its sidecar."""
    if child_rel in plans:
        return plans[child_rel].count
    if child_rel in removed:
        return None
    digest = read_folder_digest(child)
    return None if digest is None else digest.count


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
