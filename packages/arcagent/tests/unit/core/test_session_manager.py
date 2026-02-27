"""Tests for SessionManager — session lifecycle, JSONL persistence, compaction."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock


def _mock_model(*, return_value: Any = None, side_effect: Exception | None = None) -> MagicMock:
    """Create a mock LLM model with invoke() returning LLMResponse-like object."""
    model = MagicMock()
    if side_effect is not None:
        model.invoke = AsyncMock(side_effect=side_effect)
    else:
        model.invoke = AsyncMock(return_value=MagicMock(content=return_value))
    return model


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
            "THIS IS INVALID JSON\n"
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
        await sm.create_session()

        # Add enough messages to trigger compaction
        for i in range(20):
            await sm.append_message({"role": "user", "content": f"message {i} " * 50})

        mock_model = _mock_model(return_value="Summary of older messages.")

        await sm.compact(mock_model, tmp_path)

        # Should have fewer messages after compaction
        msgs = sm.get_messages()
        # First entry should be compaction summary
        has_summary = any(m.get("type") == "compaction_summary" for m in msgs)
        assert has_summary

    async def test_compaction_preserves_recent_messages(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        for i in range(20):
            await sm.append_message({"role": "user", "content": f"message {i}"})

        original_count = sm.message_count
        mock_model = _mock_model(return_value="Summary.")

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

        mock_model = _mock_model(return_value="Summary of conversation.")

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

    async def test_resume_nonexistent_file(self, tmp_path: Path) -> None:
        """Resume session when file doesn't exist returns empty list."""
        sm = _make_session_manager(tmp_path)
        messages = await sm.resume_session("nonexistent-session")
        assert messages == []
        assert sm.session_id == "nonexistent-session"

    async def test_resume_with_empty_lines(self, tmp_path: Path) -> None:
        """Empty lines in JSONL are skipped."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        jsonl_path = sessions_dir / "test.jsonl"
        jsonl_path.write_text(
            '{"type":"message","role":"user","content":"first"}\n'
            "\n"
            '{"type":"message","role":"assistant","content":"second"}\n'
            "\n\n"
            '{"type":"message","role":"user","content":"third"}\n'
        )

        sm = _make_session_manager(tmp_path)
        messages = await sm.resume_session("test")
        assert len(messages) == 3

    async def test_compact_with_few_messages(self, tmp_path: Path) -> None:
        """Compaction does nothing with fewer than 4 messages."""
        sm = _make_session_manager(tmp_path)
        await sm.create_session()
        await sm.append_message({"role": "user", "content": "msg1"})
        await sm.append_message({"role": "assistant", "content": "msg2"})

        mock_model = _mock_model(return_value="Summary.")

        await sm.compact(mock_model, tmp_path)

        # Should still have 2 messages
        assert sm.message_count == 2

    async def test_sanitize_context_truncation(self, tmp_path: Path) -> None:
        """_sanitize_context_output truncates at 2000 chars."""
        sm = _make_session_manager(tmp_path)
        long_text = "a" * 3000
        result = sm._sanitize_context_output(long_text)
        assert len(result) <= 2000 + len("\n[truncated]")
        assert "[truncated]" in result

    async def test_cleanup_no_sessions_dir(self, tmp_path: Path) -> None:
        """cleanup_old_sessions when sessions dir doesn't exist."""
        sm = _make_session_manager(tmp_path)
        # Don't create sessions dir
        await sm.cleanup_old_sessions()
        # Should not raise

    async def test_pre_compact_flush_empty_messages(self, tmp_path: Path) -> None:
        """Pre-compaction flush with no message content."""
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        mock_model = _mock_model(return_value="Facts")

        # Messages with no content
        messages = [
            {"type": "compaction_summary", "summary": "old summary"},
        ]

        await sm._pre_compact_flush(messages, tmp_path, mock_model)
        # Model should not be called
        mock_model.invoke.assert_not_called()

    async def test_pre_compact_flush_model_failure(self, tmp_path: Path) -> None:
        """Pre-compaction flush continues when model fails."""
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        mock_model = _mock_model(side_effect=Exception("Model failed"))

        messages = [
            {"type": "message", "role": "user", "content": "hello"},
        ]

        # Should not raise
        await sm._pre_compact_flush(messages, tmp_path, mock_model)

    async def test_pre_compact_flush_appends_to_context(self, tmp_path: Path) -> None:
        """Pre-compaction flush appends to existing context.md."""
        context_path = tmp_path / "context.md"
        context_path.write_text("# Existing context\n\nOld content")

        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        mock_model = _mock_model(return_value="New facts")

        messages = [
            {"type": "message", "role": "user", "content": "hello"},
        ]

        await sm._pre_compact_flush(messages, tmp_path, mock_model)

        content = context_path.read_text()
        assert "Existing context" in content
        assert "Old content" in content
        assert "Compaction Flush" in content
        assert "New facts" in content


class TestContextSanitizationUnicode:
    """ASI-06: context.md writes sanitize Unicode attack vectors."""

    async def test_nfkc_normalization(self, tmp_path: Path) -> None:
        """Fullwidth characters are normalized to ASCII equivalents."""
        sm = _make_session_manager(tmp_path)
        # Fullwidth 'A' (U+FF21) should normalize to regular 'A'
        result = sm._sanitize_context_output("\uff21\uff22\uff23")
        assert result == "ABC"

    async def test_zero_width_characters_stripped(self, tmp_path: Path) -> None:
        """Zero-width and invisible Unicode chars are removed."""
        sm = _make_session_manager(tmp_path)
        result = sm._sanitize_context_output("hello\u200bworld\ufeff")
        assert result == "helloworld"

    async def test_control_characters_stripped(self, tmp_path: Path) -> None:
        """ASCII control characters are removed."""
        sm = _make_session_manager(tmp_path)
        result = sm._sanitize_context_output("clean\x00\x01text")
        assert result == "cleantext"

    async def test_newlines_and_tabs_preserved(self, tmp_path: Path) -> None:
        """Newlines and tabs are kept for readability."""
        sm = _make_session_manager(tmp_path)
        result = sm._sanitize_context_output("line1\nline2\ttab")
        assert result == "line1\nline2\ttab"

    async def test_pre_compact_flush_sanitizes_output(self, tmp_path: Path) -> None:
        """End-to-end: context.md writes are sanitized against poisoning."""
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        # Model returns text with zero-width chars (injection attempt)
        mock_model = _mock_model(return_value="Facts\u200b with \ufeffinvisible chars")

        messages = [
            {"type": "message", "role": "user", "content": "hello"},
        ]

        await sm._pre_compact_flush(messages, tmp_path, mock_model)

        context_path = tmp_path / "context.md"
        content = context_path.read_text()
        assert "\u200b" not in content
        assert "\ufeff" not in content
        assert "Facts with invisible chars" in content


class TestSummarizationFailure:
    """Lines 265-267: Summarization exception returns fallback."""

    async def test_summarize_fallback_on_exception(self, tmp_path: Path) -> None:
        sm = _make_session_manager(tmp_path)
        await sm.create_session()

        mock_model = _mock_model(side_effect=RuntimeError("LLM down"))
        messages = [
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "message", "role": "assistant", "content": "hi"},
        ]

        result = await sm._summarize_messages(messages, mock_model)
        assert "[Compacted 2 messages]" in result
