"""Tests for SessionManager — session lifecycle, JSONL persistence, compaction."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.core.session_manager import SessionManager


def _make_telemetry() -> MagicMock:
    """Create a mock telemetry object."""
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_session_manager(
    workspace: Path,
    *,
    retention_count: int = 50,
    retention_days: int = 30,
) -> SessionManager:
    """Create a SessionManager with test defaults."""
    return SessionManager(
        config=SessionConfig(
            retention_count=retention_count,
            retention_days=retention_days,
        ),
        context_config=ContextConfig(),
        telemetry=_make_telemetry(),
        workspace=workspace,
    )


class TestCreateSession:
    async def test_creates_session_with_uuid4_id(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()
        assert len(session_id) == 36  # UUID4 format: 8-4-4-4-12
        assert "-" in session_id

    async def test_creates_sessions_directory(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()
        assert (tmp_path / "sessions").is_dir()

    async def test_creates_jsonl_file(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()
        assert (tmp_path / "sessions" / f"{session_id}.jsonl").exists()

    async def test_session_id_property(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()
        assert sm.session_id == session_id

    async def test_message_count_starts_at_zero(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()
        assert sm.message_count == 0


class TestAppendMessage:
    async def test_appends_to_in_memory_list(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()
        await sm.append_message({"role": "user", "content": "hello"})
        assert sm.message_count == 1

    async def test_writes_jsonl_line(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()
        await sm.append_message({"role": "user", "content": "hello"})

        jsonl_path = tmp_path / "sessions" / f"{session_id}.jsonl"
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["role"] == "user"
        assert entry["content"] == "hello"
        assert entry["type"] == "message"
        assert "timestamp" in entry

    async def test_multiple_messages_append(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()
        await sm.append_message({"role": "user", "content": "hi"})
        await sm.append_message({"role": "assistant", "content": "hello"})
        await sm.append_message({"role": "user", "content": "bye"})

        assert sm.message_count == 3
        jsonl_path = tmp_path / "sessions" / f"{session_id}.jsonl"
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 3

    async def test_thread_safety(self, tmp_path: Path) -> None:
        """Concurrent appends should all succeed without data loss."""
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        async def append(i: int) -> None:
            await sm.append_message({"role": "user", "content": f"msg-{i}"})

        await asyncio.gather(*(append(i) for i in range(20)))
        assert sm.message_count == 20


class TestResumeSession:
    async def test_loads_messages_from_jsonl(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()
        await sm.append_message({"role": "user", "content": "msg1"})
        await sm.append_message({"role": "assistant", "content": "msg2"})

        # Create new manager, resume
        sm2 = _make_session_manager(tmp_path)
        messages = await sm2.resume_session(session_id)
        assert len(messages) == 2
        assert messages[0]["content"] == "msg1"
        assert messages[1]["content"] == "msg2"

    async def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        jsonl_path = sessions_dir / "test-session.jsonl"
        jsonl_path.write_text(
            '{"type":"message","role":"user","content":"good"}\n'
            'THIS IS INVALID JSON\n'
            '{"type":"message","role":"assistant","content":"also good"}\n'
        )

        sm = _make_session_manager(tmp_path)
        messages = await sm.resume_session("test-session")
        assert len(messages) == 2
        assert messages[0]["content"] == "good"
        assert messages[1]["content"] == "also good"

    async def test_loads_empty_session(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "empty.jsonl").write_text("")

        sm = _make_session_manager(tmp_path)
        messages = await sm.resume_session("empty")
        assert messages == []

    async def test_session_id_set_after_resume(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "my-session.jsonl").write_text(
            '{"type":"message","role":"user","content":"hi"}\n'
        )

        sm = _make_session_manager(tmp_path)
        await sm.resume_session("my-session")
        assert sm.session_id == "my-session"


class TestGetMessages:
    async def test_returns_snapshot_not_reference(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()
        await sm.append_message({"role": "user", "content": "hello"})

        msgs = sm.get_messages()
        msgs.append({"role": "user", "content": "injected"})
        assert sm.message_count == 1  # Original unchanged


class TestCompaction:
    async def test_compaction_produces_summary_entry(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()

        # Add enough messages to trigger compaction
        for i in range(20):
            await sm.append_message({"role": "user", "content": f"message {i} " * 50})

        mock_model = AsyncMock()
        mock_model.return_value = "Summary of older messages."

        await sm.compact(mock_model, tmp_path)

        # Should have fewer messages after compaction
        msgs = sm.get_messages()
        # First entry should be compaction summary
        has_summary = any(
            m.get("type") == "compaction_summary" for m in msgs
        )
        assert has_summary

    async def test_compaction_preserves_recent_messages(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        for i in range(20):
            await sm.append_message({"role": "user", "content": f"message {i}"})

        original_count = sm.message_count
        mock_model = AsyncMock()
        mock_model.return_value = "Summary."

        await sm.compact(mock_model, tmp_path)

        msgs = sm.get_messages()
        # Recent 70% preserved + 1 summary = less than original
        assert len(msgs) < original_count
        # Last message should still be present
        assert any(m.get("content") == "message 19" for m in msgs)

    async def test_compaction_writes_summary_to_jsonl(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        session_id = await sm.create_session()

        for i in range(10):
            await sm.append_message({"role": "user", "content": f"msg {i} " * 50})

        mock_model = AsyncMock()
        mock_model.return_value = "Summary of conversation."

        await sm.compact(mock_model, tmp_path)

        jsonl_path = tmp_path / "sessions" / f"{session_id}.jsonl"
        lines = jsonl_path.read_text().strip().split("\n")
        summaries = [json.loads(l) for l in lines if "compaction_summary" in l]
        assert len(summaries) >= 1
        assert summaries[0]["type"] == "compaction_summary"


class TestCleanupOldSessions:
    async def test_cleanup_by_count(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create 5 session files
        for i in range(5):
            (sessions_dir / f"session-{i:03d}.jsonl").write_text(
                f'{{"type":"message","role":"user","content":"s{i}"}}\n'
            )

        sm = _make_session_manager(tmp_path, retention_count=3)
        await sm.cleanup_old_sessions()

        remaining = list(sessions_dir.glob("*.jsonl"))
        assert len(remaining) == 3

    async def test_cleanup_preserves_newest(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)

        import time
        for i in range(5):
            path = sessions_dir / f"session-{i:03d}.jsonl"
            path.write_text(f'{{"content":"s{i}"}}\n')
            # Ensure different mtimes
            time.sleep(0.01)

        sm = _make_session_manager(tmp_path, retention_count=2)
        await sm.cleanup_old_sessions()

        remaining = sorted(p.name for p in sessions_dir.glob("*.jsonl"))
        # Two newest should remain
        assert len(remaining) == 2
        assert "session-004.jsonl" in remaining
        assert "session-003.jsonl" in remaining


class TestEdgeCases:
    async def test_empty_session_id_before_create(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        assert sm.session_id == ""

    async def test_message_count_zero_before_create(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        assert sm.message_count == 0
