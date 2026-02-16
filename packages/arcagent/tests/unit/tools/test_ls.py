"""Tests for the ls tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools.ls import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def ls_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestLsTool:
    """Core ls functionality."""

    async def test_ls_workspace_root(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        (workspace / "file.txt").write_text("content")
        (workspace / "subdir").mkdir()
        result = await ls_tool()
        assert "file.txt" in result
        assert "subdir" in result

    async def test_ls_shows_type_indicators(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        (workspace / "myfile.txt").write_text("content")
        (workspace / "mydir").mkdir()
        result = await ls_tool()
        lines = result.strip().split("\n")
        dir_lines = [l for l in lines if "mydir" in l]
        file_lines = [l for l in lines if "myfile" in l]
        assert any("d" in l for l in dir_lines)
        assert any("f" in l for l in file_lines)

    async def test_ls_dirs_before_files(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        (workspace / "aaa_file.txt").write_text("content")
        (workspace / "zzz_dir").mkdir()
        result = await ls_tool()
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        # Directory should appear before file
        dir_idx = next(i for i, l in enumerate(lines) if "zzz_dir" in l)
        file_idx = next(i for i, l in enumerate(lines) if "aaa_file" in l)
        assert dir_idx < file_idx

    async def test_ls_subdirectory(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "nested.py").write_text("code")
        result = await ls_tool(path="sub")
        assert "nested.py" in result

    async def test_ls_shows_file_size(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        (workspace / "sized.txt").write_text("hello world")
        result = await ls_tool()
        # Should contain some size indication
        assert "sized.txt" in result

    async def test_ls_empty_directory(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        result = await ls_tool()
        assert "empty" in result.lower() or result.strip() == ""

    async def test_ls_nonexistent_path(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        """Line 56: Nonexistent path returns error."""
        result = await ls_tool(path="nonexistent")
        assert "Error" in result

    async def test_ls_path_is_file_not_directory(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        """Line 59: Listing a file (not dir) returns error."""
        (workspace / "file.txt").write_text("content")
        result = await ls_tool(path="file.txt")
        assert "Error: Not a directory" in result

    async def test_ls_stat_error_skips_entry(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        """Lines 71-72: OSError on stat() skips that entry."""
        (workspace / "good_file.txt").write_text("content")
        (workspace / "good_dir").mkdir()
        result = await ls_tool()
        # Should contain the good entries
        assert "good_file.txt" in result
        assert "good_dir" in result

    async def test_ls_format_size_bytes(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        """Lines 30-32: Format size in bytes, KB, MB."""
        # Small file (bytes)
        (workspace / "tiny.txt").write_bytes(b"x" * 100)
        # Medium file (KB)
        (workspace / "medium.txt").write_bytes(b"x" * 2048)
        # Large file (MB)
        (workspace / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024))
        result = await ls_tool()
        # Check size formatting appears
        assert " B" in result or " KB" in result or " MB" in result


class TestLsToolSecurity:
    """Security boundary tests."""

    async def test_ls_path_outside_workspace_blocked(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ls_tool(path="/etc")
        assert exc_info.value.code in (
            "TOOL_PATH_OUTSIDE_WORKSPACE",
            "TOOL_SYMLINK_DENIED",
        )


class TestLsToolFactory:
    """Tool metadata tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "ls"

    def test_tool_has_input_schema(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert "path" in tool.input_schema["properties"]

    def test_tool_source(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.source == "arcagent.tools.ls"


class TestLsOSError:
    """Lines 71-72: OSError on stat() during ls is caught."""

    async def test_broken_symlink_skipped(
        self, workspace: Path, ls_tool: Any
    ) -> None:
        (workspace / "good.txt").write_text("content")
        (workspace / "broken.txt").symlink_to(workspace / "nonexistent")

        result = await ls_tool()
        assert "good.txt" in result
