"""Tests for COMP-019 WorkspaceLifecycle — T-813.

Covers REQ-205 (``.ingest_complete`` is the resume unit, and an unmarked
workspace is rebuilt rather than continued), REQ-211 (teardown is guaranteed,
checkpoints the write-ahead log, and the leftover count gates phase start) and
REQ-213 (``--keep-workspace-on-failure`` retains void, errored and disagreed
questions).

Everything here is real. The SQLite databases are real files written through the
stdlib driver with a real populated ``-wal``, and the checkpoint assertion copies
the ``.db`` away from its siblings and reads it — so the test fails if the
checkpoint is skipped, which asserting that a mock was called would not. Nothing
touches the network and nothing is written outside ``tmp_path``.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from evaluations.ingest.lifecycle import (
    INGEST_COMPLETE_MARKER,
    LeftoverWorkspacesError,
    WorkspaceLifecycle,
    assert_leftovers_under_threshold,
    checkpoint_sqlite_databases,
    leftover_workspaces,
)

# Enough rows that the write-ahead log is unmistakably populated, and far under
# SQLite's 1000-page autocheckpoint so nothing folds itself in behind the test.
ROW_COUNT = 200


def _populated_db(run_dir: Path) -> sqlite3.Connection:
    """Build ``memory/index.db`` with committed rows still living in its ``-wal``.

    The returned connection is left OPEN on purpose: SQLite deletes ``-wal`` and
    ``-shm`` when the last connection closes cleanly, so a test that closed here
    would assert against siblings SQLite had already tidied away. Holding the
    handle also matches the real teardown, which runs in a ``finally`` block that
    an exception can reach while the agent still owns its database.
    """
    db_path = run_dir / "memory" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE ev (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO ev (body) VALUES (?)",
        [(f"turn {i} " * 50,) for i in range(ROW_COUNT)],
    )
    conn.commit()
    return conn


def _siblings(root: Path) -> list[Path]:
    """Every ``-wal`` / ``-shm`` file surviving anywhere under ``root``."""
    return sorted([*root.rglob("*-wal"), *root.rglob("*-shm")])


# --- REQ-205: the marker is the resume unit -------------------------------


def test_marker_is_absent_until_ingest_marks_it(tmp_path: Path) -> None:
    with WorkspaceLifecycle(tmp_path / "q1", keep_on_failure=True) as workspace:
        assert not workspace.ingest_complete
        assert not (workspace.run_dir / INGEST_COMPLETE_MARKER).exists()

        workspace.mark_ingest_complete()

        assert workspace.ingest_complete
        assert (workspace.run_dir / INGEST_COMPLETE_MARKER).exists()


def test_an_interrupted_ingest_is_deleted_and_rebuilt_never_resumed(tmp_path: Path) -> None:
    """The classic resume bug: a directory existing is not a question ingested.

    Chunk-level LLM calls are order-sensitive and not idempotent, so a workspace
    holding an unknown prefix of the haystack must be thrown away, not continued.
    """
    run_dir = tmp_path / "q1"
    with pytest.raises(RuntimeError, match="ingest died"):
        with WorkspaceLifecycle(run_dir, keep_on_failure=True) as workspace:
            (workspace.run_dir / "chunk-0.txt").write_text("half the haystack")
            raise RuntimeError("ingest died mid-chunk")

    assert run_dir.exists(), "keep_on_failure should have retained the partial workspace"
    assert not (run_dir / INGEST_COMPLETE_MARKER).exists()

    with WorkspaceLifecycle(run_dir, keep_on_failure=True) as workspace:
        assert workspace.resumed is False
        assert not (workspace.run_dir / "chunk-0.txt").exists(), (
            "a workspace lacking .ingest_complete was resumed mid-ingest"
        )


def test_a_marked_workspace_is_resumed_with_its_haystack_intact(tmp_path: Path) -> None:
    run_dir = tmp_path / "q1"
    run_dir.mkdir()
    (run_dir / "chunk-0.txt").write_text("the whole haystack")
    (run_dir / INGEST_COMPLETE_MARKER).touch()

    with WorkspaceLifecycle(run_dir, keep_on_failure=True) as workspace:
        assert workspace.resumed is True
        assert (workspace.run_dir / "chunk-0.txt").read_text() == "the whole haystack"


def test_rebuilding_an_unmarked_workspace_leaves_no_wal_siblings(tmp_path: Path) -> None:
    """The rebuild goes through the same WAL-safe path as teardown."""
    run_dir = tmp_path / "q1"
    run_dir.mkdir()
    conn = _populated_db(run_dir)
    try:
        assert _siblings(run_dir), "fixture did not produce a real -wal/-shm pair"

        with WorkspaceLifecycle(run_dir) as workspace:
            assert workspace.resumed is False
            assert not (run_dir / "memory").exists()
            assert _siblings(tmp_path) == []
            workspace.mark_ingest_complete()
    finally:
        conn.close()


# --- REQ-211: guaranteed, WAL-safe teardown -------------------------------


def test_teardown_runs_when_the_body_raises(tmp_path: Path) -> None:
    run_dir = tmp_path / "q1"
    with pytest.raises(ValueError, match="the question blew up"):
        with WorkspaceLifecycle(run_dir) as workspace:
            (workspace.run_dir / "trace.jsonl").write_text("{}")
            raise ValueError("the question blew up")

    assert not run_dir.exists()


def test_teardown_checkpoints_the_wal_into_the_database(tmp_path: Path) -> None:
    """The rows must be readable from the ``.db`` alone, away from its ``-wal``.

    Without the checkpoint the copied file has no ``ev`` table at all, so this
    fails loudly if ``PRAGMA wal_checkpoint(TRUNCATE)`` stops running. The tree
    removal is the second half of teardown, so the copy is taken from the
    checkpoint step alone, while the tree still stands.
    """
    run_dir = tmp_path / "q1"
    run_dir.mkdir()
    conn = _populated_db(run_dir)
    db_path = run_dir / "memory" / "index.db"
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    try:
        assert wal_path.stat().st_size > 0

        checkpoint_sqlite_databases(run_dir)

        assert wal_path.stat().st_size == 0, "TRUNCATE did not zero the write-ahead log"
        alone = tmp_path / "alone.db"
        alone.write_bytes(db_path.read_bytes())
        with closing(sqlite3.connect(alone)) as folded:
            assert folded.execute("SELECT COUNT(*) FROM ev").fetchone()[0] == ROW_COUNT
    finally:
        conn.close()


def test_teardown_leaves_no_wal_or_shm_siblings(tmp_path: Path) -> None:
    """Removing the ``.db`` alone would leave siblings that can exceed it."""
    run_dir = tmp_path / "q1"
    run_dir.mkdir()
    conn = _populated_db(run_dir)
    try:
        assert _siblings(run_dir), "fixture did not produce a real -wal/-shm pair"

        WorkspaceLifecycle(run_dir).teardown()

        assert not run_dir.exists()
        assert _siblings(tmp_path) == []
    finally:
        conn.close()

    assert _siblings(tmp_path) == [], "closing the agent's handle resurrected a sibling"


def test_teardown_removes_a_tree_holding_an_unreadable_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt index must not cost the disk; the failure is logged, not swallowed."""
    run_dir = tmp_path / "q1"
    (run_dir / "memory").mkdir(parents=True)
    (run_dir / "memory" / "index.db").write_text("this is not a database")

    with caplog.at_level(logging.WARNING, logger="evaluations.ingest.lifecycle"):
        WorkspaceLifecycle(run_dir).teardown()

    assert not run_dir.exists()
    assert any("could not checkpoint" in record.message for record in caplog.records)


def test_teardown_is_idempotent(tmp_path: Path) -> None:
    workspace = WorkspaceLifecycle(tmp_path / "gone")

    workspace.teardown()
    workspace.teardown()

    assert not (tmp_path / "gone").exists()


# --- REQ-211: the leftover gate -------------------------------------------


def test_leftovers_at_the_threshold_are_allowed(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"q{index}").mkdir()

    assert_leftovers_under_threshold(tmp_path, threshold=3)


def test_leftovers_above_the_threshold_refuse_the_phase(tmp_path: Path) -> None:
    for index in range(4):
        (tmp_path / f"q{index}").mkdir()

    with pytest.raises(LeftoverWorkspacesError, match="4 leftover workspaces"):
        assert_leftovers_under_threshold(tmp_path, threshold=3)


def test_leftover_count_ignores_files_and_a_missing_root(tmp_path: Path) -> None:
    (tmp_path / "results.jsonl").write_text("{}\n")
    (tmp_path / "q0").mkdir()

    assert leftover_workspaces(tmp_path) == [tmp_path / "q0"]
    assert leftover_workspaces(tmp_path / "never-created") == []
    assert_leftovers_under_threshold(tmp_path / "never-created", threshold=0)


# --- REQ-213: --keep-workspace-on-failure ---------------------------------


def test_keep_on_failure_retains_an_errored_workspace(tmp_path: Path) -> None:
    run_dir = tmp_path / "q1"
    with pytest.raises(RuntimeError, match="provider timed out"):
        with WorkspaceLifecycle(run_dir, keep_on_failure=True):
            raise RuntimeError("provider timed out")

    assert run_dir.exists()


@pytest.mark.parametrize("reason", ["void", "judge_disagreed"])
def test_keep_on_failure_retains_a_void_or_disagreed_workspace(
    tmp_path: Path, reason: str
) -> None:
    """Void and disagreed questions return a row rather than raising."""
    run_dir = tmp_path / "q1"
    with WorkspaceLifecycle(run_dir, keep_on_failure=True) as workspace:
        workspace.mark_ingest_complete()
        workspace.mark_failed(reason)
        assert workspace.failure_reason == reason

    assert run_dir.exists()


def test_keep_on_failure_still_destroys_a_successful_workspace(tmp_path: Path) -> None:
    run_dir = tmp_path / "q1"
    with WorkspaceLifecycle(run_dir, keep_on_failure=True) as workspace:
        workspace.mark_ingest_complete()

    assert not run_dir.exists()


@pytest.mark.parametrize("reason", ["void", "judge_disagreed"])
def test_a_failure_without_the_flag_is_destroyed(tmp_path: Path, reason: str) -> None:
    run_dir = tmp_path / "q1"
    with WorkspaceLifecycle(run_dir) as workspace:
        workspace.mark_failed(reason)

    assert not run_dir.exists()


def test_an_error_without_the_flag_is_destroyed_and_still_propagates(tmp_path: Path) -> None:
    run_dir = tmp_path / "q1"
    with pytest.raises(RuntimeError, match="provider timed out"):
        with WorkspaceLifecycle(run_dir):
            raise RuntimeError("provider timed out")

    assert not run_dir.exists()
