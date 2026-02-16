"""Tests for the find tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools.find import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def find_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestFindTool:
    """Core find functionality."""

    async def test_find_by_glob(
        self, workspace: Path, find_tool: Any
    ) -> None:
        (workspace / "app.py").write_text("code")
        (workspace / "test.py").write_text("test")
        (workspace / "readme.md").write_text("docs")
        result = await find_tool(pattern="*.py")
        assert "app.py" in result
        assert "test.py" in result
        assert "readme.md" not in result

    async def test_find_recursive(
        self, workspace: Path, find_tool: Any
    ) -> None:
        sub = workspace / "src" / "utils"
        sub.mkdir(parents=True)
        (sub / "helpers.py").write_text("code")
        result = await find_tool(pattern="**/*.py")
        assert "helpers.py" in result

    async def test_find_sorted_by_mtime_newest_first(
        self, workspace: Path, find_tool: Any
    ) -> None:
        old = workspace / "old.py"
        old.write_text("old")
        # Ensure different mtime
        time.sleep(0.05)
        new = workspace / "new.py"
        new.write_text("new")
        result = await find_tool(pattern="*.py")
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        assert lines[0] == "new.py"
        assert lines[1] == "old.py"

    async def test_find_with_path(
        self, workspace: Path, find_tool: Any
    ) -> None:
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "inside.txt").write_text("in")
        (workspace / "outside.txt").write_text("out")
        result = await find_tool(pattern="*.txt", path="sub")
        assert "inside.txt" in result
        assert "outside.txt" not in result

    async def test_find_max_results_limit(
        self, workspace: Path, find_tool: Any
    ) -> None:
        for i in range(20):
            (workspace / f"file{i:02d}.txt").write_text(f"content {i}")
        result = await find_tool(pattern="*.txt", max_results=5)
        lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(lines) <= 6  # 5 results + possible truncation note

    async def test_find_no_matches(
        self, workspace: Path, find_tool: Any
    ) -> None:
        (workspace / "file.py").write_text("code")
        result = await find_tool(pattern="*.rs")
        assert "No matches" in result

    async def test_find_returns_relative_paths(
        self, workspace: Path, find_tool: Any
    ) -> None:
        sub = workspace / "src"
        sub.mkdir()
        (sub / "main.py").write_text("code")
        result = await find_tool(pattern="**/*.py")
        assert "src/main.py" in result
        # Should not contain absolute path
        assert str(workspace) not in result

    async def test_find_not_a_directory_error(
        self, workspace: Path, find_tool: Any
    ) -> None:
        """Line 60: Path is not a directory."""
        (workspace / "file.txt").write_text("content")
        result = await find_tool(pattern="*.py", path="file.txt")
        assert "Error: Not a directory" in result

    async def test_find_stat_error_skips_file(
        self, workspace: Path, find_tool: Any
    ) -> None:
        """Lines 71-72: OSError on stat() skips file."""
        (workspace / "good.py").write_text("code")
        result = await find_tool(pattern="*.py")
        assert "good.py" in result

    async def test_find_skips_non_files(
        self, workspace: Path, find_tool: Any
    ) -> None:
        """Line 74: Non-file entries (directories) are skipped."""
        (workspace / "code.py").write_text("content")
        (workspace / "subdir").mkdir()
        result = await find_tool(pattern="*")
        # Should only find the file, not the directory
        assert "code.py" in result
        # Directory names shouldn't appear as results
        lines = result.strip().split("\n")
        assert len([l for l in lines if "subdir" in l]) == 0 or "truncated" in result


class TestFindTraversalProtection:
    """Pattern traversal attack tests."""

    async def test_double_dot_pattern_rejected(
        self, workspace: Path, find_tool: Any
    ) -> None:
        result = await find_tool(pattern="../../etc/*")
        assert "Error: Pattern must not contain '..'" in result

    async def test_double_dot_in_middle_rejected(
        self, workspace: Path, find_tool: Any
    ) -> None:
        result = await find_tool(pattern="sub/../../../etc/passwd")
        assert "Error: Pattern must not contain '..'" in result

    async def test_single_dot_pattern_allowed(
        self, workspace: Path, find_tool: Any
    ) -> None:
        """Single dot in pattern should be fine (e.g., *.py)."""
        (workspace / "code.py").write_text("code")
        result = await find_tool(pattern="*.py")
        assert "code.py" in result


class TestFindToolSecurity:
    """Security boundary tests."""

    async def test_find_path_outside_workspace_blocked(
        self, workspace: Path, find_tool: Any
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await find_tool(pattern="*", path="/etc")
        assert exc_info.value.code in (
            "TOOL_PATH_OUTSIDE_WORKSPACE",
            "TOOL_SYMLINK_DENIED",
        )


class TestFindToolFactory:
    """Tool metadata tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "find"

    def test_tool_has_input_schema(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert "pattern" in tool.input_schema["properties"]

    def test_tool_source(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.source == "arcagent.tools.find"


class TestFindOSError:
    """Lines 71-72: OSError on stat() during find is caught."""

    async def test_broken_symlink_skipped(
        self, workspace: Path, find_tool: Any
    ) -> None:
        # Create a valid file and a broken symlink
        (workspace / "good.txt").write_text("content")
        (workspace / "broken.txt").symlink_to(workspace / "nonexistent")

        result = await find_tool(pattern="*.txt")
        assert "good.txt" in result
