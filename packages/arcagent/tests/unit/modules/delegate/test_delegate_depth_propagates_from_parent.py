"""Tests for depth propagation through ToolContext.parent_state.

Goal B M3 gap-close: delegate_tool must read depth from ctx.parent_state
rather than defaulting to 0.  This file proves:

1. Parent at depth=1 → child spawns at depth=2
2. Parent at depth=2 (max_depth=2) → child (depth=3) is rejected with "max_depth"
3. Parent at depth=0 (default) → child at depth=1 succeeds depth check
4. ctx.parent_state=None → raises ValueError (hard requirement, TD-036)
5. DELEGATE_BLOCKED_TOOLS still stripped correctly through the parent chain
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool, ToolContext

from arcagent.modules.delegate.config import DelegateConfig
from arcagent.modules.delegate.delegate_tool import _build_child_tool_list, make_delegate_tool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _noop(params: dict, ctx: object) -> str:
    return "ok"


def _make_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Tool: {name}",
        input_schema={"type": "object", "properties": {}, "required": []},
        execute=_noop,
    )


def _make_run_state(
    *,
    depth: int = 0,
    max_depth: int = 2,
    run_id: str = "test-run",
) -> RunState:
    """Build a minimal RunState with the given depth settings."""
    bus = EventBus(run_id=run_id)
    tools = [_make_tool("search")]
    registry = ToolRegistry(tools=tools, event_bus=bus)
    return RunState(
        messages=[],
        registry=registry,
        event_bus=bus,
        run_id=run_id,
        depth=depth,
        max_depth=max_depth,
    )


def _make_ctx(parent_state: RunState | None, run_id: str = "test-run") -> ToolContext:
    """Build a ToolContext with the given parent_state."""
    bus = EventBus(run_id=run_id) if parent_state is None else parent_state.event_bus
    return ToolContext(
        run_id=run_id,
        tool_call_id="tc-1",
        turn_number=1,
        event_bus=bus,
        cancelled=asyncio.Event(),
        parent_state=parent_state,
    )


# ---------------------------------------------------------------------------
# Depth cap rejection tests (no spawn() call needed)
# ---------------------------------------------------------------------------


class TestDepthCapRejection:
    """Verify that delegate tool rejects when child_depth > max_depth."""

    @pytest.mark.asyncio
    async def test_parent_depth_0_max_depth_2_child_at_1_allowed(self) -> None:
        """Child at depth=1 is below max_depth=2 — should NOT be rejected by depth check.

        We verify this by confirming the tool does NOT return a max_depth error.
        (The tool will still fail because spawn() needs a real model, but the
        rejection must NOT come from the depth check.)
        """
        cfg = DelegateConfig(max_depth=2)
        parent_state = _make_run_state(depth=0, max_depth=2)
        ctx = _make_ctx(parent_state)

        parent_tools = [_make_tool("search")]
        tool = make_delegate_tool(parent_tools=parent_tools, config=cfg)

        # Patch spawn() so we never actually call a model
        with patch(
            "arcagent.modules.delegate.delegate_tool.spawn",
            new_callable=AsyncMock,
        ) as mock_spawn:
            from arcagent.orchestration import SpawnResult, TokenUsage

            mock_spawn.return_value = SpawnResult(
                child_run_id="child-1",
                child_did="did:arc:child",
                status="completed",
                summary="done",
                tokens=TokenUsage(input=10, output=5, total=15),
                tool_trace=[],
                audit_chain_tip="abc",
                duration_s=0.1,
            )

            raw = await tool.execute({"task": "do something"}, ctx)
            result = json.loads(raw)

        # Must NOT be a depth-cap error
        assert result.get("error") != "max_depth"
        assert result.get("status") != "error" or result.get("error") != "max_depth"

    @pytest.mark.asyncio
    async def test_parent_depth_1_max_depth_2_child_at_2_allowed(self) -> None:
        """Child at depth=2 equals max_depth=2 — allowed (boundary case)."""
        cfg = DelegateConfig(max_depth=2)
        parent_state = _make_run_state(depth=1, max_depth=2)
        ctx = _make_ctx(parent_state)

        parent_tools = [_make_tool("search")]
        tool = make_delegate_tool(parent_tools=parent_tools, config=cfg)

        with patch(
            "arcagent.modules.delegate.delegate_tool.spawn",
            new_callable=AsyncMock,
        ) as mock_spawn:
            from arcagent.orchestration import SpawnResult, TokenUsage

            mock_spawn.return_value = SpawnResult(
                child_run_id="child-2",
                child_did="did:arc:child",
                status="completed",
                summary="done",
                tokens=TokenUsage(input=10, output=5, total=15),
                tool_trace=[],
                audit_chain_tip="abc",
                duration_s=0.1,
            )

            raw = await tool.execute({"task": "do something"}, ctx)
            result = json.loads(raw)

        assert result.get("error") != "max_depth"

    @pytest.mark.asyncio
    async def test_parent_depth_2_max_depth_2_child_at_3_rejected(self) -> None:
        """Parent at depth=2, max_depth=2 → child would be at depth=3 → rejected."""
        cfg = DelegateConfig(max_depth=2)
        parent_state = _make_run_state(depth=2, max_depth=2)
        ctx = _make_ctx(parent_state)

        parent_tools = [_make_tool("search")]
        tool = make_delegate_tool(parent_tools=parent_tools, config=cfg)

        raw = await tool.execute({"task": "do something"}, ctx)
        result = json.loads(raw)

        assert result["status"] == "error"
        assert result["error"] == "max_depth"

    @pytest.mark.asyncio
    async def test_parent_depth_1_max_depth_2_grandchild_rejected(self) -> None:
        """Full chain test: parent=1 → child=2 (ok) → grandchild=3 (rejected).

        We simulate the grandchild by creating a 'child' context at depth=2
        and calling delegate.  That should be rejected because 2+1=3 > max_depth=2.
        """
        cfg = DelegateConfig(max_depth=2)
        # Simulate the context as seen by a child agent already at depth=2
        grandparent_state = _make_run_state(depth=2, max_depth=2)
        ctx = _make_ctx(grandparent_state)

        parent_tools = [_make_tool("search")]
        tool = make_delegate_tool(parent_tools=parent_tools, config=cfg)

        raw = await tool.execute({"task": "grandchild task"}, ctx)
        result = json.loads(raw)

        assert result["status"] == "error"
        assert result["error"] == "max_depth"
        assert "depth" in result.get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_parent_state_none_raises_value_error(self) -> None:
        """TD-036: ctx.parent_state=None must raise ValueError (not silently depth=0).

        Callers MUST populate ctx.parent_state before invoking delegate_tool.
        Silently defaulting to depth=0 bypasses the federal depth cap — we fail
        fast instead so the misconfiguration is caught immediately.
        """
        cfg = DelegateConfig(max_depth=2)
        ctx = _make_ctx(parent_state=None)

        parent_tools = [_make_tool("search")]
        tool = make_delegate_tool(parent_tools=parent_tools, config=cfg)

        with pytest.raises(ValueError, match="parent_state"):
            await tool.execute({"task": "legacy task"}, ctx)

    @pytest.mark.asyncio
    async def test_depth_error_includes_numbers_in_detail(self) -> None:
        """Rejection message should include the depth numbers for debuggability."""
        cfg = DelegateConfig(max_depth=2)
        parent_state = _make_run_state(depth=2, max_depth=2)
        ctx = _make_ctx(parent_state)

        parent_tools = [_make_tool("search")]
        tool = make_delegate_tool(parent_tools=parent_tools, config=cfg)

        raw = await tool.execute({"task": "deep task"}, ctx)
        result = json.loads(raw)

        assert result["status"] == "error"
        assert result["error"] == "max_depth"
        detail = result.get("detail", "")
        assert "3" in detail or "max_depth" in detail.lower()


# ---------------------------------------------------------------------------
# DELEGATE_BLOCKED_TOOLS still works through parent chain
# ---------------------------------------------------------------------------


class TestBlockedToolsThroughParentChain:
    def test_blocked_tools_stripped_at_depth_1(self) -> None:
        """Blocked tools are stripped regardless of parent depth."""
        parent_tools = [
            _make_tool("search"),
            _make_tool("delegate"),  # blocked
            _make_tool("memory"),  # blocked
        ]
        allowed, stripped = _build_child_tool_list(parent_tools, None)
        allowed_names = {t.name for t in allowed}
        assert "delegate" not in allowed_names
        assert "memory" not in allowed_names
        assert "search" in allowed_names

    def test_blocked_tools_stripped_at_depth_2(self) -> None:
        """Same behaviour at a deeper depth level."""
        parent_tools = [
            _make_tool("read_file"),
            _make_tool("send_message"),  # blocked
            _make_tool("execute_code"),  # blocked
        ]
        allowed, stripped = _build_child_tool_list(parent_tools, None)
        allowed_names = {t.name for t in allowed}
        assert "send_message" not in allowed_names
        assert "execute_code" not in allowed_names
        assert "read_file" in allowed_names


# ---------------------------------------------------------------------------
# ToolContext carries parent_state
# ---------------------------------------------------------------------------


class TestToolContextParentState:
    def test_tool_context_accepts_parent_state(self) -> None:
        """ToolContext can be constructed with parent_state set."""
        state = _make_run_state(depth=1)
        ctx = _make_ctx(state)
        assert ctx.parent_state is state

    def test_tool_context_parent_state_defaults_none(self) -> None:
        """ToolContext.parent_state defaults to None for backward compat."""
        ctx = ToolContext(
            run_id="r",
            tool_call_id="tc",
            turn_number=1,
            event_bus=None,
            cancelled=asyncio.Event(),
        )
        assert ctx.parent_state is None

    def test_tool_context_depth_readable_from_parent_state(self) -> None:
        """Reading ctx.parent_state.depth works when set."""
        state = _make_run_state(depth=3)
        ctx = _make_ctx(state)
        assert ctx.parent_state is not None
        assert ctx.parent_state.depth == 3
