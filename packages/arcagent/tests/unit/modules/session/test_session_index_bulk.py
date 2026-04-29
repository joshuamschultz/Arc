"""Unit tests for SessionIndex bulk insert and asyncio.create_task fix.

SPEC-018 Wave B1 performance fixes.  Covers:
  - test_bulk_insert_10k_under_3s  — 10K-line JSONL, _scan_once < 3s
  - test_create_task_not_get_loop  — start() uses asyncio.create_task()
  - test_executemany_batching      — multiple batch boundaries exercised

The existing crash-recovery semantics (idempotent replay from offset) are
exercised by test_index.py and remain unchanged.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from arcagent.modules.session.index import _INSERT_BATCH_SIZE, SessionIndex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl_session(sessions_dir: Path, n_messages: int) -> Path:
    """Write a synthetic JSONL session file with n_messages content entries."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / "00000000-0000-0000-0000-000000000001.jsonl"
    with path.open("w") as fh:
        for i in range(n_messages):
            entry = {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"message content number {i} " + ("x" * 40),
                "timestamp": "2024-01-01T00:00:00",
                "user_did": "did:arc:user:human/abc123",
                "agent_did": "did:arc:agent/test",
                "classification": "unclassified",
            }
            fh.write(json.dumps(entry) + "\n")
    return path


# ---------------------------------------------------------------------------
# Performance test: 10K messages under 3s
# ---------------------------------------------------------------------------


@pytest.mark.performance
def test_bulk_insert_10k_under_3s(tmp_path: Path) -> None:
    """_scan_once must complete 10 000 inserts in under 3 seconds.

    This validates that the executemany batching provides sufficient
    throughput improvement over the previous single-row insert loop.
    SQLite is synchronous here; asyncio.to_thread wrapping is the caller's
    responsibility.
    """
    sessions_dir = tmp_path / "sessions"
    _write_jsonl_session(sessions_dir, n_messages=10_000)

    db_path = tmp_path / "index.db"
    index = SessionIndex(
        db_path=db_path,
        sessions_dir=sessions_dir,
        poll_interval=3600.0,  # never polls; we call _scan_once directly
    )
    index._init_schema()

    start = time.monotonic()
    index._scan_once()
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, (
        f"_scan_once with 10K messages took {elapsed:.2f}s — expected < 3.0s. "
        "Check executemany batching is active."
    )

    # Verify rows were actually inserted.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == 10_000, f"Expected 10000 rows, got {count}"


# ---------------------------------------------------------------------------
# asyncio.create_task() vs get_event_loop().create_task()
# ---------------------------------------------------------------------------


async def test_create_task_not_get_loop(tmp_path: Path) -> None:
    """start() must use asyncio.create_task(), not get_event_loop().create_task().

    We check the actual call site in the source code: the deprecated call
    pattern is ``get_event_loop().create_task(`` — a method call chain.
    The word 'get_event_loop' may appear in comments, so we check for the
    actual deprecated invocation pattern.
    """
    source = inspect.getsource(SessionIndex.start)

    # The deprecated pattern is the method call chain:  .get_event_loop().create_task
    # (with no whitespace between the two calls in practice)
    deprecated_call = "get_event_loop().create_task"
    assert deprecated_call not in source, (
        "SessionIndex.start() must use asyncio.create_task() directly, "
        "not asyncio.get_event_loop().create_task()"
    )
    assert "asyncio.create_task" in source

    # Also verify the task is created and can be cancelled.
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    db_path = tmp_path / "index.db"
    index = SessionIndex(
        db_path=db_path,
        sessions_dir=sessions_dir,
        poll_interval=3600.0,
    )
    await index.start()
    assert index._task is not None
    assert not index._task.done()
    await index.stop()
    assert index._started is False


# ---------------------------------------------------------------------------
# executemany batching: multiple batches exercised
# ---------------------------------------------------------------------------


def test_executemany_batching(tmp_path: Path) -> None:
    """Insert more than _INSERT_BATCH_SIZE rows to exercise the batch loop."""
    n = _INSERT_BATCH_SIZE * 3 + 7  # intentionally straddles three full batches
    sessions_dir = tmp_path / "sessions"
    _write_jsonl_session(sessions_dir, n_messages=n)

    db_path = tmp_path / "index.db"
    index = SessionIndex(
        db_path=db_path,
        sessions_dir=sessions_dir,
        poll_interval=3600.0,
    )
    index._init_schema()
    index._scan_once()

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == n, f"Expected {n} rows, got {count}"
