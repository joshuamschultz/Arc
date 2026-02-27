"""Tests for the read tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools.read import _MAX_FILE_SIZE, create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def read_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestReadTool:
    """Core read functionality."""

    async def test_read_simple_file(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "hello.txt").write_text("line1\nline2\nline3\n")
        result = await read_tool(file_path="hello.txt")
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    async def test_read_with_line_numbers(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "numbered.txt").write_text("alpha\nbeta\n")
        result = await read_tool(file_path="numbered.txt")
        # cat -n style: line numbers with tab separator
        assert "\t" in result
        assert "1" in result
        assert "alpha" in result

    async def test_read_with_offset(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "offset.txt").write_text("a\nb\nc\nd\n")
        result = await read_tool(file_path="offset.txt", offset=3)
        assert "c" in result
        assert "d" in result
        assert "a" not in result
        assert "b" not in result

    async def test_read_with_limit(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "limit.txt").write_text("a\nb\nc\nd\n")
        result = await read_tool(file_path="limit.txt", limit=2)
        assert "a" in result
        assert "b" in result
        lines = [l for l in result.split("\n") if l.strip()]
        assert len(lines) == 2

    async def test_read_with_offset_and_limit(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "both.txt").write_text("a\nb\nc\nd\ne\n")
        result = await read_tool(file_path="both.txt", offset=2, limit=2)
        assert "b" in result
        assert "c" in result
        lines = [l for l in result.split("\n") if l.strip()]
        assert len(lines) == 2

    async def test_read_nonexistent_file(self, read_tool: Any) -> None:
        result = await read_tool(file_path="missing.txt")
        assert "Error" in result
        assert "not found" in result

    async def test_read_directory_returns_error(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "subdir").mkdir()
        result = await read_tool(file_path="subdir")
        assert "Error" in result
        assert "Not a file" in result

    async def test_read_empty_file(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "empty.txt").write_text("")
        result = await read_tool(file_path="empty.txt")
        # Empty file should return empty string
        assert result == ""


class TestReadToolSecurity:
    """Security hardening tests."""

    async def test_symlink_blocked(self, workspace: Path, read_tool: Any) -> None:
        target = workspace / "real.txt"
        target.write_text("secret")
        link = workspace / "link.txt"
        link.symlink_to(target)
        with pytest.raises(ToolError) as exc_info:
            await read_tool(file_path="link.txt")
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"

    async def test_binary_file_returns_error(self, workspace: Path, read_tool: Any) -> None:
        (workspace / "binary.bin").write_bytes(b"\x80\x81\x82\x83")
        result = await read_tool(file_path="binary.bin")
        assert "Error" in result
        assert "not valid UTF-8" in result

    async def test_file_too_large_returns_error(self, workspace: Path, read_tool: Any) -> None:
        large_file = workspace / "huge.txt"
        # Create a file just over the limit using sparse/truncate approach
        large_file.write_bytes(b"x" * (_MAX_FILE_SIZE + 1))
        result = await read_tool(file_path="huge.txt")
        assert "Error" in result
        assert "too large" in result

    async def test_file_at_size_limit_reads_ok(self, workspace: Path, read_tool: Any) -> None:
        at_limit = workspace / "atlimit.txt"
        # Exactly at limit should be fine
        at_limit.write_text("x" * (_MAX_FILE_SIZE - 1))
        result = await read_tool(file_path="atlimit.txt")
        assert "Error" not in result


class TestReadToolFactory:
    """Tool metadata and factory tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "read"

    def test_tool_has_input_schema(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert "file_path" in tool.input_schema["properties"]

    def test_tool_source(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.source == "arcagent.tools.read"
