"""Built-in ``bash`` tool.

Runs a shell command in the workspace directory and returns combined
stdout/stderr. Output is truncated at 30,000 characters and the
process is killed if it exceeds the timeout.
"""

from __future__ import annotations

import asyncio
import codecs
import math
import os
import signal
from dataclasses import dataclass, field

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool

_MAX_OUTPUT_CHARS = 30_000
_MAX_TIMEOUT_SECONDS = 3_600
_READ_CHUNK_BYTES = 16_384
_TERMINATE_GRACE_SECONDS = 0.5


@dataclass
class _BoundedTextCapture:
    """Incrementally decode a pipe without retaining unbounded output."""

    limit: int
    _parts: list[str] = field(default_factory=list)
    total_chars: int = 0
    _retained_chars: int = 0
    _decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )

    def add(self, data: bytes, *, final: bool = False) -> None:
        text = self._decoder.decode(data, final=final)
        self.total_chars += len(text)
        remaining = self.limit - self._retained_chars
        if remaining > 0:
            retained = text[:remaining]
            self._parts.append(retained)
            self._retained_chars += len(retained)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def _validated_timeout(timeout: int | float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a number")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0 or value > _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be greater than 0 and at most {_MAX_TIMEOUT_SECONDS}s")
    return value


async def _capture_stream(
    stream: asyncio.StreamReader,
    capture: _BoundedTextCapture,
) -> None:
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        capture.add(chunk)
    capture.add(b"", final=True)


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    """Terminate all descendants in the subprocess's isolated process group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            pass
    # The shell may exit before a descendant that ignores SIGTERM. Address the
    # process group even after the leader has been reaped.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        await process.wait()


async def _finish_readers(
    readers: tuple[asyncio.Task[None], asyncio.Task[None]],
    readers_done: asyncio.Future[tuple[None, None]],
) -> None:
    """Bound pipe cleanup even if a detached descendant retained an fd."""
    try:
        await asyncio.wait_for(asyncio.shield(readers_done), timeout=_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


@tool(
    name="bash",
    description="Execute a shell command in the workspace directory.",
    classification="state_modifying",
    capability_tags=["subprocess", "file_write", "state_mutation"],
    when_to_use="When you need to run a CLI command, build, or test in the workspace.",
    version="1.0.0",
)
async def bash(command: str, timeout: int = 120) -> str:
    """Run ``command`` in the workspace; return combined stdout+stderr.

    SPEC-035 REQ-020/024: enterprise/federal run the command inside arcrun's
    tier-routed isolation backend (container/VM) — never host ``bash`` — so the
    shell cannot reach ``~/.arc/operator/**`` or ``.audit/**``. Personal keeps
    host bash with the advisory goal-lock guard.
    """
    timeout_seconds = _validated_timeout(timeout)
    if _runtime.tier() in ("enterprise", "federal"):
        return await _runtime.run_sandboxed_bash(command, timeout=timeout)
    _runtime.check_shell_command(command, tool_name="bash")
    # cwd = the working dir (the trusted project for a coding agent; the workspace
    # otherwise). Agent state (memory/sessions) never runs through bash, so it is
    # unaffected — it persists directly to the workspace.
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_runtime.working_dir()),
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - PIPE invariant
        await _stop_process_group(process)
        raise RuntimeError("failed to open subprocess output pipes")
    stdout_capture = _BoundedTextCapture(_MAX_OUTPUT_CHARS)
    stderr_capture = _BoundedTextCapture(_MAX_OUTPUT_CHARS)
    readers = (
        asyncio.create_task(_capture_stream(process.stdout, stdout_capture)),
        asyncio.create_task(_capture_stream(process.stderr, stderr_capture)),
    )
    readers_done = asyncio.gather(*readers)
    try:
        async with asyncio.timeout(timeout_seconds):
            await process.wait()
            # A descendant can inherit the pipes after its shell exits. Keep
            # pipe draining inside the same command deadline.
            await asyncio.shield(readers_done)
    except TimeoutError:
        await _stop_process_group(process)
        await _finish_readers(readers, readers_done)
        return f"Error: Command timed out after {timeout}s"
    except asyncio.CancelledError:
        await asyncio.shield(_stop_process_group(process))
        await asyncio.shield(_finish_readers(readers, readers_done))
        raise
    finally:
        if process.returncode is None:
            await asyncio.shield(_stop_process_group(process))

    parts: list[str] = []
    if stdout_capture.text:
        parts.append(stdout_capture.text)
    if stderr_capture.text:
        parts.append(stderr_capture.text)
    output = "\n".join(parts)
    total_chars = stdout_capture.total_chars + stderr_capture.total_chars
    if len(parts) == 2:
        total_chars += 1
    if total_chars > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + f"\n... (truncated, {total_chars} total chars)"
    if process.returncode != 0:
        return f"Exit code: {process.returncode}\n{output}"
    return output if output else "(no output)"
