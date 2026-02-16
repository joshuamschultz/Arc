"""Tests for the edit tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools.edit import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def edit_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestEditTool:
    """Core edit functionality."""

    async def test_replace_unique_string(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("hello world")
        result = await edit_tool(
            file_path="file.txt", old_string="hello", new_string="goodbye"
        )
        assert "Replaced 1" in result
        assert (workspace / "file.txt").read_text() == "goodbye world"

    async def test_replace_all_occurrences(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("aaa bbb aaa")
        result = await edit_tool(
            file_path="file.txt",
            old_string="aaa",
            new_string="ccc",
            replace_all=True,
        )
        assert "Replaced 2" in result
        assert (workspace / "file.txt").read_text() == "ccc bbb ccc"

    async def test_non_unique_without_replace_all_errors(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("aaa bbb aaa")
        result = await edit_tool(
            file_path="file.txt", old_string="aaa", new_string="ccc"
        )
        assert "Error" in result
        assert "2 times" in result
        # File should be unchanged
        assert (workspace / "file.txt").read_text() == "aaa bbb aaa"

    async def test_old_string_not_found(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("hello world")
        result = await edit_tool(
            file_path="file.txt", old_string="missing", new_string="x"
        )
        assert "Error" in result
        assert "not found" in result

    async def test_file_not_found(self, edit_tool: Any) -> None:
        result = await edit_tool(
            file_path="missing.txt", old_string="a", new_string="b"
        )
        assert "Error" in result
        assert "not found" in result

    async def test_edit_directory_returns_error(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        (workspace / "subdir").mkdir()
        result = await edit_tool(
            file_path="subdir", old_string="a", new_string="b"
        )
        assert "Error" in result
        assert "Not a file" in result

    async def test_replace_all_false_replaces_only_first(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        """When count==1, replace_all=False should replace that one."""
        (workspace / "file.txt").write_text("old value here")
        result = await edit_tool(
            file_path="file.txt", old_string="old", new_string="new"
        )
        assert "Replaced 1" in result
        assert (workspace / "file.txt").read_text() == "new value here"

    async def test_replacement_count_accuracy(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        """replace_all=False should report 1, not total count."""
        (workspace / "file.txt").write_text("unique_string here")
        result = await edit_tool(
            file_path="file.txt",
            old_string="unique_string",
            new_string="replaced",
        )
        assert "Replaced 1 occurrence" in result


class TestEditToolSecurity:
    """Security and edge case tests."""

    async def test_empty_old_string_blocked(self, edit_tool: Any) -> None:
        """Empty old_string would match everywhere — must be blocked."""
        result = await edit_tool(
            file_path="any.txt", old_string="", new_string="injected"
        )
        assert "Error" in result
        assert "empty" in result

    async def test_symlink_blocked(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        target = workspace / "real.txt"
        target.write_text("original content")
        link = workspace / "link.txt"
        link.symlink_to(target)
        with pytest.raises(ToolError) as exc_info:
            await edit_tool(
                file_path="link.txt",
                old_string="original",
                new_string="hacked",
            )
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        # Original unchanged
        assert target.read_text() == "original content"

    async def test_binary_file_returns_error(
        self, workspace: Path, edit_tool: Any
    ) -> None:
        (workspace / "binary.bin").write_bytes(b"\x80\x81\x82\x83")
        result = await edit_tool(
            file_path="binary.bin", old_string="x", new_string="y"
        )
        assert "Error" in result
        assert "not valid UTF-8" in result


class TestEditToolFactory:
    """Tool metadata tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "edit"

    def test_tool_has_required_fields(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        required = tool.input_schema["required"]
        assert "file_path" in required
        assert "old_string" in required
        assert "new_string" in required
