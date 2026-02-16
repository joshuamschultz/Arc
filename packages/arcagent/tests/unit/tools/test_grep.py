"""Tests for the grep tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools.grep import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def grep_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestGrepTool:
    """Core grep functionality."""

    async def test_grep_simple_pattern(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "code.py").write_text("def hello():\n    return 'world'\n")
        result = await grep_tool(pattern="hello")
        assert "code.py:1:" in result
        assert "def hello" in result

    async def test_grep_regex_pattern(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "data.txt").write_text("error: bad input\ninfo: ok\nerror: timeout\n")
        result = await grep_tool(pattern=r"error:.*")
        assert "data.txt:1:" in result
        assert "data.txt:3:" in result
        assert "info" not in result

    async def test_grep_no_matches_returns_message(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "empty_match.txt").write_text("nothing here\n")
        result = await grep_tool(pattern="zzzznotfound")
        assert "No matches" in result

    async def test_grep_in_subdirectory(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "nested.py").write_text("# TODO: fix this\n")
        result = await grep_tool(pattern="TODO")
        assert "sub/nested.py:1:" in result

    async def test_grep_with_path_filter(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "match.py").write_text("target\n")
        (workspace / "skip.txt").write_text("target\n")
        result = await grep_tool(pattern="target", path="match.py")
        assert "match.py" in result

    async def test_grep_with_glob_filter(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "yes.py").write_text("found\n")
        (workspace / "no.txt").write_text("found\n")
        result = await grep_tool(pattern="found", glob_filter="*.py")
        assert "yes.py" in result
        assert "no.txt" not in result

    async def test_grep_max_results_limit(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        lines = "\n".join(f"match line {i}" for i in range(200))
        (workspace / "many.txt").write_text(lines)
        result = await grep_tool(pattern="match", max_results=5)
        result_lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(result_lines) <= 6  # 5 matches + possible truncation note

    async def test_grep_skips_binary_files(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "binary.bin").write_bytes(b"target\x00binary")
        (workspace / "text.txt").write_text("target here\n")
        result = await grep_tool(pattern="target")
        assert "text.txt" in result
        assert "binary.bin" not in result

    async def test_grep_empty_workspace(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        result = await grep_tool(pattern="anything")
        assert "No matches" in result


class TestGrepToolSecurity:
    """Security boundary tests."""

    async def test_grep_path_outside_workspace_blocked(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await grep_tool(pattern="root", path="/etc")
        assert exc_info.value.code in (
            "TOOL_PATH_OUTSIDE_WORKSPACE",
            "TOOL_SYMLINK_DENIED",
        )


class TestGrepReDoSProtection:
    """ReDoS prevention tests."""

    async def test_pattern_exceeding_max_length_rejected(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("content\n")
        long_pattern = "a" * 1001
        result = await grep_tool(pattern=long_pattern)
        assert "Error: Pattern too long" in result
        assert "1001" in result

    async def test_pattern_at_max_length_accepted(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("a" * 1000 + "\n")
        pattern = "a" * 1000
        result = await grep_tool(pattern=pattern)
        assert "Error" not in result


class TestGrepToolFactory:
    """Tool metadata tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "grep"

    def test_tool_has_input_schema(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert "pattern" in tool.input_schema["properties"]

    def test_tool_source(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.source == "arcagent.tools.grep"
