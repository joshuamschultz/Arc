"""Unit tests for arcagent.modules.session.store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcagent.modules.session.store import (
    iter_session_files,
    jsonl_path_for,
    read_messages_from_offset,
    sessions_dir,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Helper: write a list of dicts as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# sessions_dir / jsonl_path_for
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_sessions_dir_returns_correct_path(self, workspace: Path) -> None:
        expected = workspace / "sessions"
        assert sessions_dir(workspace) == expected

    def test_jsonl_path_for_returns_correct_path(self, workspace: Path) -> None:
        expected = workspace / "sessions" / "abc123.jsonl"
        assert jsonl_path_for(workspace, "abc123") == expected


# ---------------------------------------------------------------------------
# iter_session_files
# ---------------------------------------------------------------------------


class TestIterSessionFiles:
    def test_empty_workspace_returns_empty_list(self, workspace: Path) -> None:
        assert iter_session_files(workspace) == []

    def test_missing_sessions_dir_returns_empty_list(self, workspace: Path) -> None:
        # sessions dir does not exist
        assert iter_session_files(workspace) == []

    def test_returns_jsonl_files(self, workspace: Path) -> None:
        sdir = workspace / "sessions"
        sdir.mkdir()
        (sdir / "a.jsonl").touch()
        (sdir / "b.jsonl").touch()
        result = iter_session_files(workspace)
        assert len(result) == 2
        assert all(p.suffix == ".jsonl" for p in result)

    def test_non_jsonl_files_excluded(self, workspace: Path) -> None:
        sdir = workspace / "sessions"
        sdir.mkdir()
        (sdir / "a.jsonl").touch()
        (sdir / "index.db").touch()
        result = iter_session_files(workspace)
        assert len(result) == 1
        assert result[0].name == "a.jsonl"

    def test_sorted_by_mtime_oldest_first(self, workspace: Path) -> None:
        import time

        sdir = workspace / "sessions"
        sdir.mkdir()
        first = sdir / "first.jsonl"
        second = sdir / "second.jsonl"
        first.touch()
        time.sleep(0.01)
        second.touch()
        result = iter_session_files(workspace)
        assert result[0].name == "first.jsonl"
        assert result[1].name == "second.jsonl"


# ---------------------------------------------------------------------------
# read_messages_from_offset
# ---------------------------------------------------------------------------


class TestReadMessagesFromOffset:
    def test_nonexistent_file_returns_empty_no_error(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        entries, offset = read_messages_from_offset(path, 0)
        assert entries == []
        assert offset == 0

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.touch()
        entries, offset = read_messages_from_offset(path, 0)
        assert entries == []
        assert offset == 0

    def test_reads_complete_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        data = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
        _write_jsonl(path, data)
        entries, offset = read_messages_from_offset(path, 0)
        assert len(entries) == 5
        assert entries[0]["role"] == "user"
        assert offset > 0

    def test_offset_advances_after_read(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        data = [{"role": "user", "content": "hello"}]
        _write_jsonl(path, data)
        _, first_offset = read_messages_from_offset(path, 0)
        # Append more data
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "assistant", "content": "world"}) + "\n")
        entries, second_offset = read_messages_from_offset(path, first_offset)
        assert len(entries) == 1
        assert entries[0]["content"] == "world"
        assert second_offset > first_offset

    def test_partial_line_at_eof_not_included(self, tmp_path: Path) -> None:
        """A line without a trailing newline is incomplete and must be skipped."""
        path = tmp_path / "partial.jsonl"
        complete = {"role": "user", "content": "complete"}
        incomplete = '{"role": "user", "content": "no newline yet"}'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(complete) + "\n")
            fh.write(incomplete)  # no trailing newline
        entries, offset = read_messages_from_offset(path, 0)
        # Only the complete line should be indexed
        assert len(entries) == 1
        assert entries[0]["content"] == "complete"
        # Offset should stop at the end of the complete line, not include partial
        expected_offset = len((json.dumps(complete) + "\n").encode("utf-8"))
        assert offset == expected_offset

    def test_malformed_json_line_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"role": "user", "content": "good"}\n')
            fh.write("NOT JSON\n")
            fh.write('{"role": "user", "content": "also good"}\n')
        entries, _ = read_messages_from_offset(path, 0)
        assert len(entries) == 2

    def test_start_offset_skips_already_indexed_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        first_entry = {"role": "user", "content": "first"}
        second_entry = {"role": "assistant", "content": "second"}
        _write_jsonl(path, [first_entry])
        _, after_first = read_messages_from_offset(path, 0)
        # Append second entry
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(second_entry) + "\n")
        entries, _ = read_messages_from_offset(path, after_first)
        assert len(entries) == 1
        assert entries[0]["content"] == "second"
