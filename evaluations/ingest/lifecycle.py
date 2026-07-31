"""WorkspaceLifecycle (COMP-019 / REQ-205, REQ-211, REQ-213).

Owns the birth and the death of the throwaway directory one question's agent
lives in. Three properties carry the design:

*The marker is the resume unit, not the directory.* ``.ingest_complete`` is
written as the final ingest action, so a workspace carrying it holds a fully fed
haystack and a workspace lacking it holds an unknown prefix of one. Chunk-level
LLM calls are order-sensitive and not idempotent, so continuing into a partial
workspace yields a plausible-looking worse answer instead of an error — the
classic resume bug, where "the directory exists" is mistaken for "the question
is ingested". Entering a workspace without the marker therefore deletes it and
starts over, paying the ingest again rather than measuring a haystack with a
hole in it.

*Teardown checkpoints before it deletes.* ``PRAGMA wal_checkpoint(TRUNCATE)``
folds the write-ahead log back into the main database and zeroes the ``-wal``
file, and closing the last connection then drops ``-wal`` and ``-shm``
outright. Removing only the ``.db`` leaves those siblings behind, and the WAL of
a busy index can exceed the database it belongs to — so what a half-removed tree
leaves is not a rounding error.

*Leftovers are a startup gate, not a cleanup step.* Each workspace is 3-8MB, so
500 of them is 1.5-4GB. A teardown that stops running fails silently and
surfaces as a full disk somewhere in the middle of a phase, after the spend. The
count is therefore checked before the phase spends anything.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import TracebackType

_LOG = logging.getLogger(__name__)

INGEST_COMPLETE_MARKER = ".ingest_complete"
"""Written last by ingest; its absence means the haystack is a partial prefix."""

MAX_LEFTOVER_WORKSPACES = 16
"""Leftover workspaces tolerated at phase start (REQ-211).

Sixteen is roughly 50-130MB: room for a ``--keep-workspace-on-failure`` batch
retained from a prior run, and low enough that a teardown regression trips the
gate inside the first few percent of a 500-question phase rather than at a full
disk two thirds of the way through it.
"""


class LeftoverWorkspacesError(RuntimeError):
    """More leftover workspaces than the threshold allows, so the phase must not start.

    Fatal rather than a warning: the condition it detects is a teardown that
    stopped running, and every further question makes the disk cost worse while
    the run reports nothing wrong.
    """


class WorkspaceLifecycle:
    """Create, mark and destroy one question's throwaway workspace.

    Used as a context manager, which is what makes REQ-211 a guarantee rather
    than a convention: ``with`` compiles to ``try/finally``, so ``teardown``
    runs whether the question completed, voided or raised.
    """

    def __init__(self, run_dir: Path, *, keep_on_failure: bool = False) -> None:
        self._run_dir = Path(run_dir)
        self._keep_on_failure = keep_on_failure
        self._failure_reason: str | None = None
        self._resumed = False

    @property
    def run_dir(self) -> Path:
        """The workspace directory this lifecycle owns."""
        return self._run_dir

    @property
    def ingest_complete(self) -> bool:
        """True once the marker exists, meaning the haystack was fully fed."""
        return (self._run_dir / INGEST_COMPLETE_MARKER).exists()

    @property
    def resumed(self) -> bool:
        """True when the entered workspace already held a complete haystack.

        The caller skips ingest on a resumed workspace and re-runs it on a
        rebuilt one; nothing else may be inferred from the directory existing.
        """
        return self._resumed

    @property
    def failure_reason(self) -> str | None:
        """The recorded non-raising failure, if any (``void``, ``disagreed``)."""
        return self._failure_reason

    def __enter__(self) -> WorkspaceLifecycle:
        """Resume a marked workspace; delete and rebuild an unmarked one (REQ-205)."""
        self._resumed = self.ingest_complete
        if not self._resumed:
            _destroy_tree(self._run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Destroy the workspace unless it is being retained for debugging (REQ-213)."""
        reason = self._failure_reason or (type(exc).__name__ if exc is not None else None)
        if self._keep_on_failure and reason is not None:
            _LOG.info("retaining workspace %s (%s)", self._run_dir, reason)
            return
        self.teardown()

    def mark_ingest_complete(self) -> None:
        """Write the marker. This is the FINAL ingest action for the workspace.

        Guards process death — an exception, a Ctrl-C, the SIGKILL that ends a
        long run — which is the failure a 500-question phase actually meets. It
        is deliberately not fsynced: SQLite runs at ``synchronous=NORMAL``, so a
        marker fsync would order the marker against nothing and buy only the
        appearance of power-loss durability.
        """
        (self._run_dir / INGEST_COMPLETE_MARKER).touch()

    def mark_failed(self, reason: str) -> None:
        """Record a failure that does not raise, so the workspace can be retained.

        Void and judge-disagreed questions complete normally and return a result
        row; only an errored one propagates an exception. Without this the
        retained set under ``--keep-workspace-on-failure`` would be errors alone
        — the least interesting third of REQ-213.
        """
        self._failure_reason = reason

    def teardown(self) -> None:
        """Checkpoint every SQLite database in the tree, then remove the tree."""
        _destroy_tree(self._run_dir)


def assert_leftovers_under_threshold(
    runs_root: Path, *, threshold: int = MAX_LEFTOVER_WORKSPACES
) -> None:
    """Refuse to start a phase when leftover workspaces exceed ``threshold`` (REQ-211)."""
    leftovers = leftover_workspaces(runs_root)
    if len(leftovers) > threshold:
        names = ", ".join(path.name for path in leftovers[:5])
        raise LeftoverWorkspacesError(
            f"{len(leftovers)} leftover workspaces under {runs_root} exceed the "
            f"threshold of {threshold} (at 3-8MB each); teardown is not running. "
            f"First few: {names}"
        )


def leftover_workspaces(runs_root: Path) -> list[Path]:
    """Every workspace directory surviving under ``runs_root``, sorted by name."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def checkpoint_sqlite_databases(tree: Path) -> None:
    """Fold every write-ahead log in ``tree`` back into its database and truncate it."""
    for db_path in sorted(Path(tree).rglob("*.db")):
        _checkpoint(db_path)


def _checkpoint(db_path: Path) -> None:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` against one database file.

    A database that cannot be checkpointed is logged and left to the tree
    removal that follows. Refusing to delete an 8MB corpse because its index is
    corrupt would turn one bad question into the leftover threshold tripping.
    """
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as err:
        _LOG.warning("could not checkpoint %s before teardown: %s", db_path, err)


def _destroy_tree(tree: Path) -> None:
    """Checkpoint the tree's databases, then remove it whole (REQ-211)."""
    if not tree.exists():
        return
    checkpoint_sqlite_databases(tree)
    shutil.rmtree(tree)


__all__ = [
    "INGEST_COMPLETE_MARKER",
    "MAX_LEFTOVER_WORKSPACES",
    "LeftoverWorkspacesError",
    "WorkspaceLifecycle",
    "assert_leftovers_under_threshold",
    "checkpoint_sqlite_databases",
    "leftover_workspaces",
]
