"""Tests for NoteManager — append-only enforcement and notes injection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.config import MemoryConfig
from arcagent.core.module_bus import EventContext
from arcagent.modules.memory.markdown_memory import NoteManager


def _make_ctx(
    event: str, data: dict[str, Any] | None = None
) -> EventContext:
    return EventContext(
        event=event,
        data=data or {},
        agent_did="did:arc:test",
        trace_id="trace-1",
    )


def _make_note_manager(workspace: Path) -> NoteManager:
    return NoteManager(workspace, MemoryConfig())


class TestAppendOnlyEnforcement:
    """T3.2.1: Write tool vetoed, edit tool allowed, read allowed."""

    def test_write_tool_vetoed(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        ctx = _make_ctx("agent:pre_tool")
        mgr.enforce_append_only(ctx, "write")
        assert ctx.is_vetoed
        assert "append-only" in ctx.veto_reason.lower()

    def test_edit_tool_allowed(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        ctx = _make_ctx("agent:pre_tool")
        mgr.enforce_append_only(ctx, "edit")
        assert not ctx.is_vetoed

    def test_read_tool_allowed(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        ctx = _make_ctx("agent:pre_tool")
        mgr.enforce_append_only(ctx, "read")
        assert not ctx.is_vetoed


class TestBashBypass:
    """T3.2.2: Bash bypass blocked."""

    def test_bash_tool_vetoed(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        ctx = _make_ctx("agent:pre_tool")
        mgr.enforce_append_only(ctx, "bash")
        assert ctx.is_vetoed
        assert "append-only" in ctx.veto_reason.lower()


class TestVetoMessage:
    """T3.2.3: Clear veto explanation."""

    def test_write_veto_has_clear_message(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        ctx = _make_ctx("agent:pre_tool")
        mgr.enforce_append_only(ctx, "write")
        assert ctx.veto_reason  # Non-empty
        assert "edit" in ctx.veto_reason.lower()  # Suggests using edit

    def test_bash_veto_has_clear_message(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        ctx = _make_ctx("agent:pre_tool")
        mgr.enforce_append_only(ctx, "bash")
        assert ctx.veto_reason
        assert "bash" in ctx.veto_reason.lower() or "append-only" in ctx.veto_reason.lower()


class TestGetRecentNotes:
    """T3.2.4: Today + yesterday content retrieval."""

    @pytest.mark.asyncio()
    async def test_returns_today_notes(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        from datetime import date
        today = date.today()
        today_file = notes_dir / f"{today.isoformat()}.md"
        today_file.write_text("Today's notes content")

        result = await mgr.get_recent_notes()
        assert "Today's notes content" in result
        assert today.isoformat() in result

    @pytest.mark.asyncio()
    async def test_returns_yesterday_notes(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        from datetime import date, timedelta
        yesterday = date.today() - timedelta(days=1)
        yesterday_file = notes_dir / f"{yesterday.isoformat()}.md"
        yesterday_file.write_text("Yesterday's notes")

        result = await mgr.get_recent_notes()
        assert "Yesterday's notes" in result

    @pytest.mark.asyncio()
    async def test_returns_both_today_and_yesterday(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        from datetime import date, timedelta
        today = date.today()
        yesterday = today - timedelta(days=1)
        (notes_dir / f"{today.isoformat()}.md").write_text("Today content")
        (notes_dir / f"{yesterday.isoformat()}.md").write_text("Yesterday content")

        result = await mgr.get_recent_notes()
        assert "Today content" in result
        assert "Yesterday content" in result

    @pytest.mark.asyncio()
    async def test_missing_files_handled_gracefully(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        # No notes directory even created
        result = await mgr.get_recent_notes()
        assert result == ""

    @pytest.mark.asyncio()
    async def test_empty_notes_dir_returns_empty(self, tmp_path: Path) -> None:
        mgr = _make_note_manager(tmp_path)
        (tmp_path / "notes").mkdir()
        result = await mgr.get_recent_notes()
        assert result == ""


class TestTokenBudget:
    """T3.2.5: Token budget on injected notes."""

    @pytest.mark.asyncio()
    async def test_today_notes_truncated_to_budget(self, tmp_path: Path) -> None:
        config = MemoryConfig(notes_budget_today_tokens=10)  # Very small budget
        mgr = NoteManager(tmp_path, config)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        from datetime import date
        today = date.today()
        # Create notes longer than budget (~10 tokens = ~40 chars)
        long_content = "A" * 200
        (notes_dir / f"{today.isoformat()}.md").write_text(long_content)

        result = await mgr.get_recent_notes()
        # Result should be shorter than original
        assert len(result) < len(long_content) + 100  # Allow for headers

    @pytest.mark.asyncio()
    async def test_yesterday_notes_truncated_to_budget(self, tmp_path: Path) -> None:
        config = MemoryConfig(notes_budget_yesterday_tokens=5)  # Very small
        mgr = NoteManager(tmp_path, config)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        from datetime import date, timedelta
        yesterday = date.today() - timedelta(days=1)
        long_content = "B" * 200
        (notes_dir / f"{yesterday.isoformat()}.md").write_text(long_content)

        result = await mgr.get_recent_notes()
        assert len(result) < len(long_content) + 100
