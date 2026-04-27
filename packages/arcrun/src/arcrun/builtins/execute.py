"""ExecuteTool — sandboxed Python subprocess execution."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from typing import Any

from arcrun.types import Tool, ToolContext

_DEFAULT_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "en_US.UTF-8",
}

_GRACE_PERIOD = 5.0


def make_execute_tool(
    *,
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    extra_env: dict[str, str] | None = None,
    tier: str = "personal",
) -> Tool:
    """Create a sandboxed Python execution tool.

    Args:
        timeout_seconds: Maximum execution time in seconds.
        max_output_bytes: Maximum size of captured stdout+stderr.
        extra_env: Additional environment variables for the subprocess.
        tier: Deployment tier ("personal", "enterprise", "federal").
            Reserved for future use — currently all tiers use the local
            subprocess backend. Federal tier may require a Firecracker
            backend in future releases.
    """
    _ = tier  # tier is accepted for API compatibility; local backend used for all tiers
    env = {**_DEFAULT_ENV, **(extra_env or {})}

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        code = params["code"]
        start = time.time()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_path = os.path.join(tmpdir, "script.py")
            with open(code_path, "w") as f:
                f.write(code)

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                code_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=env,
                start_new_session=True,
            )

            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except TimeoutError:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    await asyncio.sleep(_GRACE_PERIOD)
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout = b""
                stderr = b"Error: execution timed out"

            duration_ms = (time.time() - start) * 1000

            return json.dumps(
                {
                    "stdout": stdout[:max_output_bytes].decode(errors="replace"),
                    "stderr": stderr[:max_output_bytes].decode(errors="replace"),
                    "exit_code": proc.returncode if not timed_out else -1,
                    "duration_ms": round(duration_ms, 1),
                }
            )

    return Tool(
        name="execute_python",
        description=(
            "Execute Python code in a sandboxed subprocess. "
            "Returns stdout, stderr, exit_code, and duration."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
            },
            "required": ["code"],
        },
        execute=_execute,
        timeout_seconds=None,
    )
