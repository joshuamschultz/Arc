"""Unit tests for arcagent.modules.session.search (session_search tool)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.session import SessionIndex
from arcagent.modules.session.search import (
    build_session_search_tool,
    session_search,
    set_index,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_index() -> None:
    """Ensure _index is cleared after each test to prevent cross-test state."""
    set_index(None)
    yield
    set_index(None)


@pytest.fixture
def tmp_sessions(tmp_path: Path) -> Path:
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    return sdir


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "index.db"


def _make_entry(role: str = "user", content: str = "hello") -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": content,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _write_session(sdir: Path, session_id: str, entries: list[dict[str, Any]]) -> Path:
    path = sdir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return path


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestBuildSessionSearchTool:
    def test_tool_name(self) -> None:
        tool = build_session_search_tool()
        assert tool.name == "session_search"

    def test_tool_description_mentions_full_text(self) -> None:
        tool = build_session_search_tool()
        assert "full-text" in tool.description.lower() or "query" in tool.description.lower()

    def test_tool_has_query_param(self) -> None:
        tool = build_session_search_tool()
        props = tool.input_schema.get("properties", {})
        assert "query" in props

    def test_tool_requires_query(self) -> None:
        tool = build_session_search_tool()
        required = tool.input_schema.get("required", [])
        assert "query" in required

    def test_tool_classification_is_read_only(self) -> None:
        tool = build_session_search_tool()
        assert tool.classification == "read_only"

    def test_tool_category(self) -> None:
        tool = build_session_search_tool()
        assert tool.category == "recall"

    def test_execute_is_callable(self) -> None:
        tool = build_session_search_tool()
        assert callable(tool.execute)


# ---------------------------------------------------------------------------
# session_search callable — no index
# ---------------------------------------------------------------------------


class TestSessionSearchNoIndex:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_index(self) -> None:
        result = await session_search(query="anything")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_query_empty(self) -> None:
        result = await session_search(query="")
        assert result == []


# ---------------------------------------------------------------------------
# session_search callable — with live index
# ---------------------------------------------------------------------------


async def _make_live_index(db_path: Path, sessions_dir: Path) -> SessionIndex:
    """Helper: create, start, and wire a SessionIndex (long poll_interval)."""
    index = SessionIndex(db_path, sessions_dir, poll_interval=3600.0)
    await index.start()
    set_index(index)
    return index


class TestSessionSearchWithIndex:
    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self, db_path: Path, tmp_sessions: Path) -> None:
        index = await _make_live_index(db_path, tmp_sessions)
        try:
            entries = [_make_entry(content="needle in haystack")]
            _write_session(tmp_sessions, "sess-dicts", entries)
            await asyncio.to_thread(index._scan_once)

            result = await session_search(query="needle")
            assert isinstance(result, list)
            if result:
                assert isinstance(result[0], dict)
        finally:
            await index.stop()
            set_index(None)

    @pytest.mark.asyncio
    async def test_result_has_required_keys(self, db_path: Path, tmp_sessions: Path) -> None:
        index = await _make_live_index(db_path, tmp_sessions)
        try:
            entries = [_make_entry(content="test result keys")]
            _write_session(tmp_sessions, "sess-keys", entries)
            await asyncio.to_thread(index._scan_once)

            result = await session_search(query="test result")
            assert len(result) >= 1
            hit = result[0]
            for key in ("session_id", "role", "ts", "snippet", "jsonl_path", "jsonl_offset"):
                assert key in hit
        finally:
            await index.stop()
            set_index(None)

    @pytest.mark.asyncio
    async def test_since_filter_as_string(self, db_path: Path, tmp_sessions: Path) -> None:
        index = await _make_live_index(db_path, tmp_sessions)
        try:
            entries = [
                {
                    "type": "message",
                    "role": "user",
                    "content": "old python message",
                    "timestamp": "2020-01-01T00:00:00+00:00",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": "new python message",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                },
            ]
            _write_session(tmp_sessions, "sess-since-str", entries)
            await asyncio.to_thread(index._scan_once)

            result = await session_search(query="python", since="2025-01-01T00:00:00+00:00")
            assert len(result) == 1
            assert "new" in result[0]["snippet"]
        finally:
            await index.stop()
            set_index(None)

    @pytest.mark.asyncio
    async def test_invalid_since_does_not_crash(self, db_path: Path, tmp_sessions: Path) -> None:
        """An invalid 'since' value should log a warning but still return results."""
        index = await _make_live_index(db_path, tmp_sessions)
        try:
            entries = [_make_entry(content="robust error handling")]
            _write_session(tmp_sessions, "sess-invalid-since", entries)
            await asyncio.to_thread(index._scan_once)

            result = await session_search(query="robust", since="not-a-date")
            # Should not raise; since filter is silently dropped
            assert isinstance(result, list)
        finally:
            await index.stop()
            set_index(None)

    @pytest.mark.asyncio
    async def test_classification_max_filter(self, db_path: Path, tmp_sessions: Path) -> None:
        index = await _make_live_index(db_path, tmp_sessions)
        try:
            entries = [
                {
                    "type": "message",
                    "role": "user",
                    "content": "classified content alpha",
                    "classification": "unclassified",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": "classified content beta",
                    "classification": "cui",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            ]
            _write_session(tmp_sessions, "sess-clf", entries)
            await asyncio.to_thread(index._scan_once)

            result = await session_search(
                query="classified content", classification_max="unclassified"
            )
            assert len(result) == 1
            assert result[0]["classification"] == "unclassified"
        finally:
            await index.stop()
            set_index(None)
