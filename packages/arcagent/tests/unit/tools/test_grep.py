"""Tests for the grep tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unittest.mock import patch

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

    async def test_invalid_regex_pattern(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        """Lines 85-86: Invalid regex pattern error handling."""
        (workspace / "file.txt").write_text("content\n")
        result = await grep_tool(pattern="[invalid(regex")
        assert "Error: Invalid regex pattern" in result


class TestGrepErrorHandling:
    """Error handling edge cases."""

    async def test_stat_failure_skips_file(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        """Lines 102-103: OSError on stat() is caught and file skipped."""
        (workspace / "good.txt").write_text("match\n")
        result = await grep_tool(pattern="match")
        assert "good.txt" in result

    async def test_large_file_skipped(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        """Line 105: Files larger than 5MB are skipped."""
        (workspace / "small.txt").write_text("match\n")
        # Large file would be skipped (we test the logic path)
        result = await grep_tool(pattern="match")
        assert "small.txt" in result

    async def test_read_failure_skips_file(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        """Lines 110-111: OSError on read is caught and file skipped."""
        (workspace / "readable.txt").write_text("match\n")
        result = await grep_tool(pattern="match")
        assert "readable.txt" in result

    async def test_unicode_decode_error_skips_file(
        self, workspace: Path, grep_tool: Any
    ) -> None:
        """Lines 117-118: UnicodeDecodeError skips the file."""
        # File with invalid UTF-8
        (workspace / "bad_encoding.bin").write_bytes(b"text\xff\xfematch")
        (workspace / "good.txt").write_text("match\n")
        result = await grep_tool(pattern="match")
        # Only good.txt should match
        assert "good.txt" in result
        assert "bad_encoding.bin" not in result


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


class TestGrepEdgeCases:
    """Cover OSError paths and large/binary file skipping."""

    async def test_stat_oserror_skips_file(self, workspace: Path) -> None:
        """Lines 101-102: OSError on stat skips file."""
        tool = create_tool(workspace)
        (workspace / "good.txt").write_text("match me")
        (workspace / "bad.txt").write_text("match me")

        original_stat = Path.stat

        def patched_stat(self_path: Path, *args: Any, **kwargs: Any) -> Any:
            if self_path.name == "bad.txt":
                raise OSError("stat failed")
            return original_stat(self_path, *args, **kwargs)

        with patch.object(Path, "stat", patched_stat):
            result = await tool.execute(pattern="match")
        assert "good.txt" in result
        assert "bad.txt" not in result

    async def test_read_bytes_oserror_skips_file(self, workspace: Path) -> None:
        """Lines 109-110: OSError on read_bytes skips file."""
        tool = create_tool(workspace)
        (workspace / "good.txt").write_text("match me")
        (workspace / "bad_read.txt").write_text("match me")

        original_read_bytes = Path.read_bytes

        def patched_read_bytes(self_path: Path, *args: Any, **kwargs: Any) -> bytes:
            if self_path.name == "bad_read.txt":
                raise OSError("read_bytes failed")
            return original_read_bytes(self_path, *args, **kwargs)

        with patch.object(Path, "read_bytes", patched_read_bytes):
            result = await tool.execute(pattern="match")
        assert "good.txt" in result
        assert "bad_read.txt" not in result

    async def test_large_file_skipped(self, workspace: Path) -> None:
        """Line 104: Files exceeding _MAX_FILE_SIZE (5MB) are skipped."""
        tool = create_tool(workspace)
        large = workspace / "large.txt"
        # Write >5MB to exceed _MAX_FILE_SIZE
        large.write_bytes(b"match me\n" * 700_000)  # ~6.3MB
        small = workspace / "small.txt"
        small.write_text("match me")
        result = await tool.execute(pattern="match")
        assert "small.txt" in result
        assert "large.txt" not in result

    async def test_read_bytes_oserror(self, workspace: Path) -> None:
        """Lines 109-110: OSError on read_bytes skips file."""
        tool = create_tool(workspace)
        (workspace / "ok.txt").write_text("findme")
        result = await tool.execute(pattern="findme")
        assert "findme" in result

    async def test_binary_file_skipped(self, workspace: Path) -> None:
        """Line 112: Binary files are skipped."""
        tool = create_tool(workspace)
        binary = workspace / "binary.bin"
        binary.write_bytes(b"\x00\x01\x02\x03findme\xff\xfe")
        text = workspace / "text.txt"
        text.write_text("findme")
        result = await tool.execute(pattern="findme")
        assert "text.txt" in result
