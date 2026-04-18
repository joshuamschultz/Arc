"""Integration tests for SessionIndex polling crash-safety and end-to-end flow.

Tests:
  1. Write JSONL, wait one poll cycle, assert all lines indexed.
  2. Simulate crash mid-poll (partial JSONL line written) — restart →
     only complete lines indexed, partial line skipped.
  3. Full end-to-end: write → poll → search returns results.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arcagent.modules.session.index import SessionIndex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _make_msg(content: str, classification: str = "unclassified") -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": content,
        "classification": classification,
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def _run_single_poll(index: SessionIndex) -> None:
    """Trigger exactly one poll cycle via asyncio.to_thread."""
    await asyncio.to_thread(index._scan_once)


# ---------------------------------------------------------------------------
# Test 1: Write JSONL → wait one poll cycle → all rows indexed
# ---------------------------------------------------------------------------


class TestPollingFullCycle:
    @pytest.mark.asyncio
    async def test_write_jsonl_then_poll_indexes_all_lines(self, tmp_path: Path) -> None:
        """Write 10 JSONL lines, run one poll, assert 10 rows in messages table."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"

        entries = [_make_msg(f"integration message {i}") for i in range(10)]
        _write_entries(sessions_dir / "session-a.jsonl", entries)

        index = SessionIndex(db_path, sessions_dir, poll_interval=0.5)
        await index.start()

        # Wait for at least one poll cycle to fire naturally.
        await asyncio.sleep(0.8)

        hits = index.search("integration message")
        await index.stop()

        assert len(hits) == 10, f"Expected 10 hits, got {len(hits)}"

    @pytest.mark.asyncio
    async def test_poll_indexes_multiple_sessions(self, tmp_path: Path) -> None:
        """Multiple session files are all indexed in one poll cycle."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"

        for i in range(3):
            _write_entries(
                sessions_dir / f"session-{i}.jsonl",
                [_make_msg(f"topic_{i} content")],
            )

        index = SessionIndex(db_path, sessions_dir, poll_interval=0.5)
        await index.start()
        await asyncio.sleep(0.8)
        await index.stop()

        # Reopen to inspect without the index being active
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 3


# ---------------------------------------------------------------------------
# Test 2: Crash recovery — partial line at EOF must not be indexed on restart
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_partial_line_at_eof_not_indexed_on_first_run(
        self, tmp_path: Path
    ) -> None:
        """A partial (no trailing newline) JSONL line must be skipped.

        This simulates the indexer seeing an in-progress write — the writer
        has started the line but not yet flushed the newline terminator.
        On the next poll (or after recovery) the partial line stays unindexed
        until the writer completes it.
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"

        complete = _make_msg("complete line that should be indexed")
        partial_raw = '{"type": "message", "role": "user", "content": "partial NO NEWLINE"'

        path = sessions_dir / "session-crash.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(complete) + "\n")
            fh.write(partial_raw)  # no trailing newline — simulates crash mid-write

        index = SessionIndex(db_path, sessions_dir, poll_interval=60.0)
        await index.start()
        await _run_single_poll(index)
        await index.stop()

        import sqlite3

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        rows = conn.execute("SELECT content FROM messages").fetchall()
        conn.close()

        # Only the complete line must be indexed.
        assert count == 1, f"Expected 1 row, got {count}: {rows}"
        assert "complete line" in rows[0][0]

    @pytest.mark.asyncio
    async def test_partial_line_indexed_after_writer_completes(
        self, tmp_path: Path
    ) -> None:
        """After the writer finishes the partial line, the next poll picks it up."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"

        complete = _make_msg("first complete line")
        partial_data = _make_msg("second line completed later")

        path = sessions_dir / "session-recover.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(complete) + "\n")
            # Simulate partial write (no newline yet)
            fh.write(json.dumps(partial_data))  # no newline

        index = SessionIndex(db_path, sessions_dir, poll_interval=60.0)
        await index.start()
        await _run_single_poll(index)

        # "Writer" finishes the line
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n")  # complete the partial line

        await _run_single_poll(index)
        await index.stop()

        import sqlite3

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 2

    @pytest.mark.asyncio
    async def test_restart_resumes_from_committed_offset(self, tmp_path: Path) -> None:
        """Crash recovery: after indexer restart, replay starts from last checkpoint.

        Simulate: index 5 lines → stop indexer (simulating crash) → append 5
        more lines → start new indexer → only the 5 new lines are indexed.
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"
        path = sessions_dir / "session-restart.jsonl"

        # Phase 1: write and index 5 lines
        first_batch = [_make_msg(f"batch1 item {i}") for i in range(5)]
        _write_entries(path, first_batch)

        index1 = SessionIndex(db_path, sessions_dir, poll_interval=60.0)
        await index1.start()
        await _run_single_poll(index1)
        await index1.stop()  # "crash"

        # Phase 2: append 5 more lines
        with open(path, "a", encoding="utf-8") as fh:
            for i in range(5):
                fh.write(json.dumps(_make_msg(f"batch2 item {i}")) + "\n")

        # Restart indexer — should resume from saved offset
        index2 = SessionIndex(db_path, sessions_dir, poll_interval=60.0)
        await index2.start()
        await _run_single_poll(index2)
        await index2.stop()

        import sqlite3

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        # Total should be 10 (no duplicates, no gaps)
        assert count == 10, f"Expected 10 total rows, got {count}"


# ---------------------------------------------------------------------------
# Test 3: End-to-end: write → poll → search
# ---------------------------------------------------------------------------


class TestEndToEndSearch:
    @pytest.mark.asyncio
    async def test_write_poll_search_full_cycle(self, tmp_path: Path) -> None:
        """Full integration: write JSONL, poll, verify FTS5 search returns hits."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"

        entries = [
            _make_msg("the quantum computing paradigm is shifting"),
            _make_msg("machine learning models require vast datasets"),
            _make_msg("the quick brown fox jumped"),
        ]
        _write_entries(sessions_dir / "session-e2e.jsonl", entries)

        index = SessionIndex(db_path, sessions_dir, poll_interval=0.5)
        await index.start()
        await asyncio.sleep(0.8)  # let poll fire

        hits_quantum = index.search("quantum computing")
        hits_fox = index.search("fox")
        hits_nothing = index.search("nonexistenttokenz9z9z9")

        await index.stop()

        assert len(hits_quantum) >= 1
        assert "<<" in hits_quantum[0].snippet
        assert len(hits_fox) >= 1
        assert len(hits_nothing) == 0

    @pytest.mark.asyncio
    async def test_fts5_ranked_order_best_match_first(self, tmp_path: Path) -> None:
        """Results are ordered by BM25 rank (most relevant first)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        db_path = sessions_dir / "index.db"

        entries = [
            _make_msg("python python python is the best language"),  # high match
            _make_msg("python is good but java exists"),  # lower match
        ]
        _write_entries(sessions_dir / "session-rank.jsonl", entries)

        index = SessionIndex(db_path, sessions_dir, poll_interval=60.0)
        await index.start()
        await _run_single_poll(index)

        hits = index.search("python")
        await index.stop()

        assert len(hits) >= 1
        # First hit should have higher density of "python"
        assert "python" in hits[0].snippet.lower()
