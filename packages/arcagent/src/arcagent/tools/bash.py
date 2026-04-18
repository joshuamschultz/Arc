"""Bash tool -- execute shell commands in the workspace.

Commands run with the workspace as the current working directory.
Output is captured from both stdout and stderr, and truncated
at 30,000 characters to prevent context overflow.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport

_MAX_OUTPUT_CHARS = 30_000

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command to execute.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds. Defaults to 120.",
        },
    },
    "required": ["command"],
}


def create_tool(workspace: Path) -> RegisteredTool:
    """Create a workspace-scoped bash tool."""
    ws = workspace.resolve()

    async def execute(
        *,
        command: str,
        timeout: int = 120,
        **_kwargs: Any,
    ) -> str:
        """Execute a shell command in the workspace directory."""
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ws),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # Process already exited
            await process.wait()
            return f"Error: Command timed out after {timeout}s"

        parts: list[str] = []
        if stdout:
            parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            parts.append(stderr.decode("utf-8", errors="replace"))

        output = "\n".join(parts)

        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(output)} total chars)"

        if process.returncode != 0:
            return f"Exit code: {process.returncode}\n{output}"

        return output if output else "(no output)"

    return RegisteredTool(
        name="bash",
        description="Execute a shell command in the workspace directory.",
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        # Above the default 120s command timeout to avoid double-timeout
        timeout_seconds=130,
        source="arcagent.tools.bash",
        classification="state_modifying",
        capability_tags=["subprocess", "file_write", "state_mutation"],
    )
