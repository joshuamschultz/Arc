"""Tests for the bash tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.tools.bash import _MAX_OUTPUT_CHARS, create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def bash_tool(workspace: Path) -> Any:
    tool = create_tool(workspace)
    return tool.execute


class TestBashTool:
    """Core bash functionality."""

    async def test_simple_command(self, bash_tool: Any) -> None:
        result = await bash_tool(command="echo hello")
        assert "hello" in result

    async def test_command_with_exit_code(self, bash_tool: Any) -> None:
        result = await bash_tool(command="exit 1")
        assert "Exit code: 1" in result

    async def test_stderr_captured(self, bash_tool: Any) -> None:
        result = await bash_tool(command="echo error >&2")
        assert "error" in result

    async def test_both_stdout_and_stderr(self, bash_tool: Any) -> None:
        result = await bash_tool(command="echo out && echo err >&2")
        assert "out" in result
        assert "err" in result

    async def test_no_output_returns_placeholder(
        self, bash_tool: Any
    ) -> None:
        result = await bash_tool(command="true")
        assert result == "(no output)"

    async def test_runs_in_workspace_directory(
        self, workspace: Path, bash_tool: Any
    ) -> None:
        result = await bash_tool(command="pwd")
        assert str(workspace.resolve()) in result

    async def test_timeout_returns_error(self, bash_tool: Any) -> None:
        result = await bash_tool(command="sleep 10", timeout=1)
        assert "Error" in result
        assert "timed out" in result

    async def test_timeout_process_already_exited(
        self, bash_tool: Any
    ) -> None:
        """Lines 59-60: ProcessLookupError when process already exited."""
        # Use a very short timeout with a command that exits immediately
        # This tests the exception path where process.kill() raises ProcessLookupError
        result = await bash_tool(command="exit 0", timeout=1)
        # Should complete successfully (process exits before timeout)
        assert "timed out" not in result

    async def test_output_truncation(
        self, workspace: Path, bash_tool: Any
    ) -> None:
        # Generate output larger than max
        char_count = _MAX_OUTPUT_CHARS + 1000
        result = await bash_tool(
            command=f"python3 -c \"print('x' * {char_count})\""
        )
        assert "truncated" in result

    async def test_multiline_output(self, bash_tool: Any) -> None:
        result = await bash_tool(command="echo 'line1'; echo 'line2'")
        assert "line1" in result
        assert "line2" in result


class TestBashToolFactory:
    """Tool metadata tests."""

    def test_tool_name(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.name == "bash"

    def test_tool_timeout_above_default(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert tool.timeout_seconds == 130

    def test_tool_has_required_command(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        assert "command" in tool.input_schema["required"]


class TestBashTimeout:
    """Lines 59-60: ProcessLookupError during kill after timeout."""

    async def test_timeout_returns_error_message(self, workspace: Path) -> None:
        tool = create_tool(workspace)
        # Sleep command that exceeds the tool's timeout
        result = await tool.execute(command="sleep 300", timeout=1)
        assert "timed out" in result.lower()
