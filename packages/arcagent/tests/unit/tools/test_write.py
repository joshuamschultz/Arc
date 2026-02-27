"""Tests for the write tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools.write import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def write_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestWriteTool:
    """Core write functionality."""

    async def test_write_new_file(self, workspace: Path, write_tool: Any) -> None:
        result = await write_tool(file_path="new.txt", content="hello world")
        assert "Written" in result
        assert (workspace / "new.txt").read_text() == "hello world"

    async def test_write_overwrites_existing(self, workspace: Path, write_tool: Any) -> None:
        (workspace / "exist.txt").write_text("old content")
        await write_tool(file_path="exist.txt", content="new content")
        assert (workspace / "exist.txt").read_text() == "new content"

    async def test_write_creates_parent_dirs(self, workspace: Path, write_tool: Any) -> None:
        result = await write_tool(file_path="deep/nested/dir/file.txt", content="nested")
        assert "Written" in result
        assert (workspace / "deep/nested/dir/file.txt").read_text() == "nested"

    async def test_write_reports_byte_count(self, workspace: Path, write_tool: Any) -> None:
        content = "twelve chars"
        result = await write_tool(file_path="count.txt", content=content)
        assert str(len(content)) in result

    async def test_write_empty_content(self, workspace: Path, write_tool: Any) -> None:
        result = await write_tool(file_path="empty.txt", content="")
        assert "Written" in result
        assert (workspace / "empty.txt").read_text() == ""

    async def test_write_utf8_content(self, workspace: Path, write_tool: Any) -> None:
        content = "Hello \u00e9\u00e8\u00ea \u4e16\u754c"
        await write_tool(file_path="utf8.txt", content=content)
        assert (workspace / "utf8.txt").read_text(encoding="utf-8") == content

    async def test_write_to_directory_returns_error(
        self, workspace: Path, write_tool: Any
    ) -> None:
        (workspace / "subdir").mkdir()
        result = await write_tool(file_path="subdir", content="oops")
        assert "Error" in result
        assert "Not a file" in result


class TestWriteToolSecurity:
    """Security hardening tests."""

    async def test_symlink_blocked(self, workspace: Path, write_tool: Any) -> None:
        target = workspace / "real.txt"
        target.write_text("original")
        link = workspace / "link.txt"
        link.symlink_to(target)
        with pytest.raises(ToolError) as exc_info:
            await write_tool(file_path="link.txt", content="injected")
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        # Original file should be unchanged
        assert target.read_text() == "original"


class TestWriteToolFactory:
    """Tool metadata tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "write"

    def test_tool_has_required_fields(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert "file_path" in tool.input_schema["properties"]
        assert "content" in tool.input_schema["properties"]
        assert "file_path" in tool.input_schema["required"]
        assert "content" in tool.input_schema["required"]
