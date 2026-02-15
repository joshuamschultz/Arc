"""Tests for tools __init__ -- create_builtin_tools factory."""

from __future__ import annotations

from pathlib import Path

from arcagent.tools import create_builtin_tools


class TestCreateBuiltinTools:
    """Factory function tests."""

    def test_returns_seven_tools(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        assert len(tools) == 7

    def test_tool_names(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        names = {t.name for t in tools}
        assert names == {"read", "write", "edit", "bash", "grep", "find", "ls"}

    def test_all_tools_are_native_transport(self, tmp_path: Path) -> None:
        from arcagent.core.tool_registry import ToolTransport

        tools = create_builtin_tools(tmp_path)
        for tool in tools:
            assert tool.transport == ToolTransport.NATIVE

    def test_all_tools_have_execute(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        for tool in tools:
            assert tool.execute is not None

    def test_all_tools_have_input_schema(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        for tool in tools:
            assert isinstance(tool.input_schema, dict)
            assert "properties" in tool.input_schema

    def test_all_tools_have_source(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        for tool in tools:
            assert tool.source.startswith("arcagent.tools.")
