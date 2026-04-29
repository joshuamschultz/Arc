"""Built-in ``bash`` tool — SPEC-021 port of arcagent.tools.bash.

Runs a shell command in the workspace directory and returns combined
stdout/stderr. Output is truncated at 30,000 characters and the
process is killed if it exceeds the timeout.
"""

from __future__ import annotations

import asyncio

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool

_MAX_OUTPUT_CHARS = 30_000


@tool(
    name="bash",
    description="Execute a shell command in the workspace directory.",
    classification="state_modifying",
    capability_tags=["subprocess", "file_write", "state_mutation"],
    when_to_use="When you need to run a CLI command, build, or test in the workspace.",
    version="1.0.0",
)
async def bash(command: str, timeout: int = 120) -> str:
    """Run ``command`` in the workspace; return combined stdout+stderr."""
    ws = _runtime.workspace()
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ws),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
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
