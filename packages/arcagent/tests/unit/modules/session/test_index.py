"""Unit tests for arcagent.modules.session.index (SessionIndex)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arcagent.modules.session.index import (
    SearchHit,
    SessionIndex,
    _classifications_up_to,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_sessions(tmp_path: Path) -> Path:
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    return sdir


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "index.db"


def _make_entry(
    role: str = "user",
    content: str = "hello",
    classification: str = "unclassified",
    ts: str | None = None,
) -> dict:
    return {
        "type": "message",
        "role": role,
        "content": content,
        "classification": classification,
        "timestamp": ts or datetime.now(UTC).isoformat(),
    }


def _write_session(sdir: Path, session_id: str, entries: list[dict]) -> Path:
    path = sdir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return path


def _make_index(db_path: Path, sessions_dir: Path, poll_interval: float = 3600.0) -> SessionIndex:
    """Create a SessionIndex with a very long poll_interval so the background
    loop doesn't fire during unit tests.  Unit tests call _scan_once directly.
    """
    return SessionIndex(db_path, sessions_dir, poll_interval=poll_interval)


# ---------------------------------------------------------------------------
# Schema / startup
# ---------------------------------------------------------------------------


class TestSessionIndexSchema:
    @pytest.mark.asyncio
    async def test_start_creates_db_tables(self, db_path: Path, tmp_sessions: Path) -> None:
        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "messages" in tables
        assert "sync_state" in tables

    @pytest.mark.asyncio
    async def test_start_creates_fts5_virtual_table(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        virtual_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'"
            ).fetchall()
        }
        conn.close()
        assert "messages_fts" in virtual_tables

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, db_path: Path, tmp_sessions: Path) -> None:
        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert row[0] == "wal"


# ---------------------------------------------------------------------------
# Polling and indexing
# ---------------------------------------------------------------------------


class TestSessionIndexPolling:
    @pytest.mark.asyncio
    async def test_scan_once_indexes_10_lines(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """Insert 10 JSONL lines, run one poll cycle, assert all 10 in messages."""
        entries = [_make_entry(content=f"message {i}") for i in range(10)]
        _write_session(tmp_sessions, "sess-abc", entries)

        # Use a long poll_interval so the background task does not run.
        index = _make_index(db_path, tmp_sessions)
        await index.start()
        # Trigger one scan cycle directly.
        await asyncio.to_thread(index._scan_once)
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 10

    @pytest.mark.asyncio
    async def test_scan_once_populates_fts5(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """After indexing, FTS5 virtual table must contain rows."""
        entries = [_make_entry(content=f"unique_token_{i}") for i in range(5)]
        _write_session(tmp_sessions, "sess-fts", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        conn.close()
        assert count == 5

    @pytest.mark.asyncio
    async def test_second_scan_does_not_duplicate(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """Running scan_once twice must not insert duplicate rows (idempotent)."""
        entries = [_make_entry(content="dedupe test")]
        _write_session(tmp_sessions, "sess-dedup", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)
        await asyncio.to_thread(index._scan_once)  # second run must be idempotent
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 1

    @pytest.mark.asyncio
    async def test_incremental_index_picks_up_new_lines(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """After first scan, new lines appended to the file are indexed on second scan."""
        path = _write_session(tmp_sessions, "sess-incr", [_make_entry(content="first")])

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        # Append a new line
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_make_entry(content="second")) + "\n")

        await asyncio.to_thread(index._scan_once)
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 2

    @pytest.mark.asyncio
    async def test_compaction_summary_entries_not_indexed(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """Compaction summary entries have no 'content' field — must be skipped."""
        entries = [
            _make_entry(content="real message"),
            {
                "type": "compaction_summary",
                "summarized_count": 3,
                "summary": "stuff",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        ]
        _write_session(tmp_sessions, "sess-compact", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)
        await index.stop()

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSessionIndexSearch:
    @pytest.mark.asyncio
    async def test_search_returns_matching_snippets(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """FTS5 search returns results with <<>> snippet markers."""
        entries = [
            _make_entry(content="the quick brown fox jumps over the lazy dog"),
            _make_entry(content="completely unrelated content about weather"),
        ]
        _write_session(tmp_sessions, "sess-search", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        hits = index.search("fox")
        await index.stop()

        assert len(hits) >= 1
        assert isinstance(hits[0], SearchHit)
        # snippet() wraps matching tokens with <<>>
        assert "<<" in hits[0].snippet
        assert ">>" in hits[0].snippet

    @pytest.mark.asyncio
    async def test_search_no_match_returns_empty(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        entries = [_make_entry(content="hello world")]
        _write_session(tmp_sessions, "sess-nomatch", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        hits = index.search("zxqjkwvpz")
        await index.stop()
        assert hits == []

    @pytest.mark.asyncio
    async def test_search_since_filter(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """since filter excludes messages before the cutoff timestamp."""
        old_ts = "2020-01-01T00:00:00+00:00"
        new_ts = "2026-01-01T00:00:00+00:00"
        entries = [
            _make_entry(content="old message about python", ts=old_ts),
            _make_entry(content="new message about python", ts=new_ts),
        ]
        _write_session(tmp_sessions, "sess-since", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        cutoff = datetime(2025, 1, 1, tzinfo=UTC)
        hits = index.search("python", since=cutoff)
        await index.stop()

        assert len(hits) == 1
        assert "new" in hits[0].snippet

    @pytest.mark.asyncio
    async def test_search_classification_max_filter(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """classification_max='unclassified' excludes 'cui' and 'secret' rows."""
        entries = [
            _make_entry(content="unclassified data point", classification="unclassified"),
            _make_entry(content="controlled unclassified data point", classification="cui"),
            _make_entry(content="secret classified data point", classification="secret"),
        ]
        _write_session(tmp_sessions, "sess-acl", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        hits = index.search("data point", classification_max="unclassified")
        await index.stop()

        assert len(hits) == 1
        assert hits[0].classification == "unclassified"

    @pytest.mark.asyncio
    async def test_search_returns_searchhit_model(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        entries = [_make_entry(content="test hit model")]
        _write_session(tmp_sessions, "sess-model", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        hits = index.search("test hit")
        await index.stop()

        assert len(hits) == 1
        hit = hits[0]
        assert hit.session_id == "sess-model"
        assert isinstance(hit.ts, float)
        assert isinstance(hit.snippet, str)
        assert isinstance(hit.jsonl_path, str)

    @pytest.mark.asyncio
    async def test_search_before_start_returns_empty(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        """search() before start() returns empty list without raising."""
        index = _make_index(db_path, tmp_sessions)
        hits = index.search("anything")
        assert hits == []

    @pytest.mark.asyncio
    async def test_search_limit_respected(
        self, db_path: Path, tmp_sessions: Path
    ) -> None:
        entries = [_make_entry(content=f"apple banana cherry {i}") for i in range(10)]
        _write_session(tmp_sessions, "sess-limit", entries)

        index = _make_index(db_path, tmp_sessions)
        await index.start()
        await asyncio.to_thread(index._scan_once)

        hits = index.search("apple", limit=3)
        await index.stop()
        assert len(hits) <= 3


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestClassificationsUpTo:
    def test_unclassified_only(self) -> None:
        assert _classifications_up_to("unclassified") == ["unclassified"]

    def test_cui_includes_lower(self) -> None:
        result = _classifications_up_to("cui")
        assert "unclassified" in result
        assert "cui" in result
        assert "secret" not in result

    def test_secret_includes_all(self) -> None:
        result = _classifications_up_to("secret")
        assert set(result) == {"unclassified", "cui", "secret"}

    def test_unknown_level_defaults_to_unclassified(self) -> None:
        assert _classifications_up_to("top_secret") == ["unclassified"]
