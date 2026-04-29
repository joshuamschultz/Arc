"""Unit tests for the delegate module agent-facing tool.

Tests:
- DELEGATE_BLOCKED_TOOLS stripped unconditionally
- Allowlist intersection with parent tools
- DelegateConfig tier-driven depth caps
- make_delegate_tool() factory returns correctly named tool
- Tool schema validation
"""

from __future__ import annotations

import pytest
from arcrun.types import Tool

from arcagent.modules.delegate import DELEGATE_BLOCKED_TOOLS, DelegateConfig, make_delegate_tool
from arcagent.modules.delegate.config import _DEPTH_CAPS
from arcagent.modules.delegate.delegate_tool import _build_child_tool_list

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


async def _noop(params: dict, ctx: object) -> str:
    return "ok"


def _make_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Tool: {name}",
        input_schema={"type": "object"},
        execute=_noop,
    )


PARENT_TOOLS = [
    _make_tool("search"),
    _make_tool("read_file"),
    _make_tool("write_file"),
    _make_tool("delegate"),  # blocked
    _make_tool("memory"),  # blocked
    _make_tool("send_message"),  # blocked
    _make_tool("execute_code"),  # blocked
    _make_tool("clarify"),  # blocked
]


# ---------------------------------------------------------------------------
# DELEGATE_BLOCKED_TOOLS
# ---------------------------------------------------------------------------


class TestDelegateBlockedTools:
    def test_blocked_tools_frozenset(self) -> None:
        assert isinstance(DELEGATE_BLOCKED_TOOLS, frozenset)

    def test_required_tools_in_blocked_set(self) -> None:
        required = {"delegate", "memory", "send_message", "execute_code", "clarify"}
        assert required.issubset(DELEGATE_BLOCKED_TOOLS)

    def test_blocked_tools_immutable(self) -> None:
        with pytest.raises(AttributeError):
            DELEGATE_BLOCKED_TOOLS.add("new_tool")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _build_child_tool_list
# ---------------------------------------------------------------------------


class TestBuildChildToolList:
    def test_blocked_tools_stripped_when_no_requested_names(self) -> None:
        """Without requested names, all parent tools minus blocked are returned."""
        allowed, stripped = _build_child_tool_list(PARENT_TOOLS, None)
        allowed_names = {t.name for t in allowed}
        # Blocked tools must not appear
        for blocked in DELEGATE_BLOCKED_TOOLS:
            assert blocked not in allowed_names, f"{blocked} was not stripped"

    def test_non_blocked_tools_preserved(self) -> None:
        allowed, stripped = _build_child_tool_list(PARENT_TOOLS, None)
        allowed_names = {t.name for t in allowed}
        assert "search" in allowed_names
        assert "read_file" in allowed_names
        assert "write_file" in allowed_names

    def test_blocked_tools_in_stripped_list(self) -> None:
        _, stripped = _build_child_tool_list(PARENT_TOOLS, None)
        stripped_set = set(stripped)
        for blocked in DELEGATE_BLOCKED_TOOLS:
            if any(t.name == blocked for t in PARENT_TOOLS):
                assert blocked in stripped_set

    def test_requested_names_intersection_with_parent(self) -> None:
        """Requested tools that aren't in parent are excluded (no escalation)."""
        allowed, stripped = _build_child_tool_list(
            PARENT_TOOLS,
            ["search", "nonexistent_tool", "read_file"],
        )
        allowed_names = {t.name for t in allowed}
        assert "search" in allowed_names
        assert "read_file" in allowed_names
        assert "nonexistent_tool" not in allowed_names

    def test_requested_blocked_tools_stripped_even_when_requested(self) -> None:
        """DELEGATE_BLOCKED_TOOLS stripped even when explicitly requested by model."""
        allowed, stripped = _build_child_tool_list(
            PARENT_TOOLS,
            ["search", "delegate", "memory"],  # model requested blocked tools
        )
        allowed_names = {t.name for t in allowed}
        assert "delegate" not in allowed_names
        assert "memory" not in allowed_names
        assert "search" in allowed_names
        assert "delegate" in stripped
        assert "memory" in stripped

    def test_empty_parent_tools_returns_empty(self) -> None:
        allowed, stripped = _build_child_tool_list([], None)
        assert allowed == []
        assert stripped == []

    def test_all_blocked_tools_returns_empty(self) -> None:
        all_blocked = [_make_tool(name) for name in DELEGATE_BLOCKED_TOOLS]
        allowed, stripped = _build_child_tool_list(all_blocked, None)
        assert allowed == []
        assert len(stripped) == len(DELEGATE_BLOCKED_TOOLS)


# ---------------------------------------------------------------------------
# DelegateConfig
# ---------------------------------------------------------------------------


class TestDelegateConfig:
    def test_federal_tier_depth_cap_2(self) -> None:
        cfg = DelegateConfig.for_tier("federal")
        assert cfg.max_depth == 2

    def test_enterprise_tier_depth_cap_3(self) -> None:
        cfg = DelegateConfig.for_tier("enterprise")
        assert cfg.max_depth == 3

    def test_personal_tier_depth_cap_4(self) -> None:
        cfg = DelegateConfig.for_tier("personal")
        assert cfg.max_depth == 4

    def test_federal_concurrency_cap_3(self) -> None:
        cfg = DelegateConfig.for_tier("federal")
        assert cfg.max_concurrent == 3

    def test_enterprise_concurrency_cap_5(self) -> None:
        cfg = DelegateConfig.for_tier("enterprise")
        assert cfg.max_concurrent == 5

    def test_default_config_personal(self) -> None:
        cfg = DelegateConfig()
        assert cfg.tier == "personal"
        assert cfg.enabled is True

    def test_config_fields_valid(self) -> None:
        cfg = DelegateConfig(
            tier="federal",
            max_depth=2,
            max_concurrent=3,
            default_max_turns=25,
            default_timeout_s=300,
        )
        assert cfg.max_depth == 2
        assert cfg.default_max_turns == 25

    def test_unknown_tier_falls_back_to_personal(self) -> None:
        cfg = DelegateConfig.for_tier("unknown_tier")
        assert cfg.max_depth == _DEPTH_CAPS["personal"]

    def test_federal_tier_case_insensitive(self) -> None:
        cfg = DelegateConfig.for_tier("FEDERAL")
        assert cfg.max_depth == _DEPTH_CAPS["federal"]


# ---------------------------------------------------------------------------
# make_delegate_tool() factory
# ---------------------------------------------------------------------------


class TestMakeDelegateTool:
    def test_returns_tool_named_delegate(self) -> None:
        parent_tools = [_make_tool("search"), _make_tool("read_file")]
        tool = make_delegate_tool(parent_tools=parent_tools)
        assert isinstance(tool, Tool)
        assert tool.name == "delegate"

    def test_tool_has_task_required(self) -> None:
        tool = make_delegate_tool(parent_tools=[_make_tool("search")])
        assert "task" in tool.input_schema["required"]

    def test_tool_has_optional_context(self) -> None:
        tool = make_delegate_tool(parent_tools=[_make_tool("search")])
        assert "context" in tool.input_schema["properties"]
        assert "context" not in tool.input_schema.get("required", [])

    def test_tool_has_optional_tools(self) -> None:
        tool = make_delegate_tool(parent_tools=[_make_tool("search")])
        assert "tools" in tool.input_schema["properties"]

    def test_tool_has_timeout_s_parameter(self) -> None:
        tool = make_delegate_tool(parent_tools=[_make_tool("search")])
        assert "timeout_s" in tool.input_schema["properties"]

    def test_tool_has_token_budget_parameter(self) -> None:
        tool = make_delegate_tool(parent_tools=[_make_tool("search")])
        assert "token_budget" in tool.input_schema["properties"]

    def test_tool_timeout_greater_than_default_child_timeout(self) -> None:
        """Tool's own timeout_seconds should exceed default child timeout."""
        cfg = DelegateConfig()
        tool = make_delegate_tool(parent_tools=[_make_tool("s")], config=cfg)
        assert tool.timeout_seconds is not None
        assert tool.timeout_seconds > cfg.default_timeout_s

    def test_tool_with_federal_config(self) -> None:
        cfg = DelegateConfig.for_tier("federal")
        tool = make_delegate_tool(
            parent_tools=[_make_tool("search")],
            config=cfg,
        )
        assert tool.name == "delegate"

    def test_execute_is_callable(self) -> None:
        tool = make_delegate_tool(parent_tools=[_make_tool("search")])
        assert callable(tool.execute)

    def test_parent_sk_bytes_optional(self) -> None:
        """make_delegate_tool must not crash if parent_sk_bytes is None."""
        tool = make_delegate_tool(
            parent_tools=[_make_tool("search")],
            parent_sk_bytes=None,
        )
        assert tool.name == "delegate"

    def test_parent_sk_bytes_provided(self) -> None:
        tool = make_delegate_tool(
            parent_tools=[_make_tool("search")],
            parent_sk_bytes=b"\x42" * 32,
        )
        assert tool.name == "delegate"
