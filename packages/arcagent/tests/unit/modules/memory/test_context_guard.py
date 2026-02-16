"""Tests for ContextGuard — context.md token budget enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.module_bus import EventContext
from arcagent.modules.memory.markdown_memory import ContextGuard


def _make_ctx(data: dict[str, Any] | None = None) -> EventContext:
    return EventContext(
        event="agent:pre_tool",
        data=data or {},
        agent_did="did:arc:test",
        trace_id="trace-1",
    )


class TestBudgetEnforcement:
    """T3.3.1: Content under budget passes."""

    @pytest.mark.asyncio()
    async def test_short_content_passes(self) -> None:
        guard = ContextGuard(budget_tokens=2000)
        args: dict[str, Any] = {"content": "short content here"}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        # Content unchanged
        assert args["content"] == "short content here"

    @pytest.mark.asyncio()
    async def test_content_at_budget_passes(self) -> None:
        guard = ContextGuard(budget_tokens=100)
        # 100 tokens ~ 400 chars
        content = "x" * 400
        args: dict[str, Any] = {"content": content}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        assert args["content"] == content


class TestOverBudgetTruncation:
    """T3.3.2: Over-budget auto-truncation from top (oldest entries)."""

    @pytest.mark.asyncio()
    async def test_over_budget_truncates_from_top(self) -> None:
        guard = ContextGuard(budget_tokens=10)  # 10 tokens ~ 40 chars
        lines = [f"Line {i}: some content here" for i in range(20)]
        content = "\n".join(lines)
        args: dict[str, Any] = {"content": content}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        # Result should be shorter than original
        assert len(args["content"]) < len(content)
        # Should keep later lines (truncate from top)
        assert "Line 19" in args["content"]

    @pytest.mark.asyncio()
    async def test_truncation_preserves_recent_lines(self) -> None:
        guard = ContextGuard(budget_tokens=5)  # Very small budget
        content = "old line 1\nold line 2\nold line 3\nnew line 4\nnew line 5"
        args: dict[str, Any] = {"content": content}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        # Most recent lines should survive
        result = args["content"]
        assert "new line 5" in result


class TestEdgeCases:
    """T3.3.3: Edge cases — empty content, single line over budget."""

    @pytest.mark.asyncio()
    async def test_empty_content_passes(self) -> None:
        guard = ContextGuard(budget_tokens=2000)
        args: dict[str, Any] = {"content": ""}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        assert args["content"] == ""

    @pytest.mark.asyncio()
    async def test_missing_content_key_safe(self) -> None:
        guard = ContextGuard(budget_tokens=2000)
        args: dict[str, Any] = {}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        # Should not crash

    @pytest.mark.asyncio()
    async def test_single_line_over_budget(self) -> None:
        guard = ContextGuard(budget_tokens=5)  # 5 tokens ~ 20 chars
        content = "A" * 200  # Single line way over budget
        args: dict[str, Any] = {"content": content}
        ctx = _make_ctx()
        await guard.enforce_budget(ctx, args)
        # Result should be truncated or empty
        result_tokens = len(args["content"]) // 4
        assert result_tokens <= 5 or args["content"] == ""
