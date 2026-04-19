"""LocalBackend — execute commands in the local OS process tree.

Security model
--------------
- Each command runs in a fresh process group (os.setsid via start_new_session).
- Cancel sends SIGTERM to the entire pgroup, waits grace seconds, then SIGKILL.
- This prevents orphaned grandchild processes (the Hermes production bug).
- No sandbox: the caller is responsible for supplying a trusted command.

Capabilities
------------
- isolation: "none"  (runs as the host user)
- cold_start_budget_ms: 10
- supports_bind_mount: True
- supports_separated_streams: True  (run_separated() exposes distinct stdout/stderr)
- max_stdout_bytes: 64 KiB (configurable)

Stream modes
------------
- run() + stream()         — merged stdout+stderr, suitable for display streaming
- run_separated()          — separate stdout/stderr collectors + exit code returned
                             together via SeparatedResult.  Used by execute.py so
                             test_stderr_capture and test_exit_code_on_failure keep
                             working while routing through LocalBackend uniformly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from arcrun.backends.base import (
    TRUNCATION_MARKER,
    BackendCapabilities,
    ExecHandle,
    ExecutorBackend,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STDOUT = 64 * 1024  # 64 KiB
_STREAM_CHUNK = 4096


@dataclass
class SeparatedResult:
    """Result from run_separated(): distinct stdout, stderr bytes and exit code.

    stdout and stderr are each hard-capped at max_stdout_bytes of the backend.
    exit_code is the actual process return code (None only if the process was
    cancelled before exiting, in which case callers should treat it as -1).
    """

    stdout: bytes
    stderr: bytes
    exit_code: int


class LocalBackend:
    """Execute commands as local subprocesses.

    This backend refactors the logic that previously lived in
    arcrun.builtins.execute.  It is always trusted (built-in; never
    loaded via entry_points).

    Thread-safety: each call to run() / run_separated() creates a fresh
    subprocess handle; there is no shared mutable state between concurrent calls.
    """

    name: str = "local"
    capabilities: BackendCapabilities = BackendCapabilities(
        supports_file_copy=False,
        supports_persistent_workspace=False,
        supports_port_forward=False,
        supports_bind_mount=True,
        supports_separated_streams=True,
        cold_start_budget_ms=10,
        max_stdout_bytes=_DEFAULT_MAX_STDOUT,
        isolation="none",
    )

    # Store live processes keyed by handle_id so cancel() can find them.
    # Uses a plain dict; access is always on the event loop so no lock needed.
    _procs: dict[str, asyncio.subprocess.Process]

    def __init__(self, max_stdout_bytes: int = _DEFAULT_MAX_STDOUT) -> None:
        self.capabilities = BackendCapabilities(
            supports_file_copy=False,
            supports_persistent_workspace=False,
            supports_port_forward=False,
            supports_bind_mount=True,
            supports_separated_streams=True,
            cold_start_budget_ms=10,
            max_stdout_bytes=max_stdout_bytes,
            isolation="none",
        )
        self._procs = {}

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> ExecHandle:
        """Launch a shell command in a new process group (merged stdout+stderr).

        Uses start_new_session=True so the child becomes a session leader
        (os.setsid equivalent).  This ensures killpg can target the entire
        pgroup including any grandchildren.

        Streams are merged (stderr → stdout) for display-oriented consumers.
        For callers that need separate stdout and stderr, use run_separated().
        """
        handle_id = str(uuid.uuid4())

        stdin_data = stdin.encode() if stdin is not None else None
        stdin_pipe = (
            asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL
        )

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr → stdout for simpler streaming
            stdin=stdin_pipe,
            cwd=cwd,
            env=env,
            # start_new_session=True is equivalent to os.setsid() in preexec_fn.
            # It places the child (and its descendants) into a fresh process group,
            # which we can then SIGKILL cleanly without racing on the PID reuse window.
            start_new_session=True,
        )

        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        self._procs[handle_id] = proc

        return ExecHandle(
            handle_id=handle_id,
            backend_name=self.name,
            meta={"timeout": timeout, "pid": proc.pid},
        )

    async def run_separated(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> SeparatedResult:
        """Run a shell command and collect stdout and stderr into separate buffers.

        Unlike run() + stream() (which merges stderr into stdout), this method
        uses asyncio.subprocess with distinct PIPE for each stream and gathers
        both concurrently.  The subprocess is always fully awaited — no dangling
        process handles.

        Returns a SeparatedResult with:
        - stdout: raw bytes (capped at max_stdout_bytes)
        - stderr: raw bytes (capped at max_stdout_bytes)
        - exit_code: actual process return code

        Callers use this when they need separate stderr (e.g., execute_python
        wants to surface the distinction between stdout and stderr to the LLM).
        """
        max_bytes = self.capabilities.max_stdout_bytes

        stdin_data = stdin.encode() if stdin is not None else None
        stdin_pipe = (
            asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL
        )

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # keep stderr separate
            stdin=stdin_pipe,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            timed_out = True
            await _kill_process_group(proc)
            stdout_bytes = b""
            stderr_bytes = b"Error: execution timed out"

        exit_code: int
        if timed_out:
            exit_code = -1
        else:
            exit_code = proc.returncode if proc.returncode is not None else -1

        return SeparatedResult(
            stdout=stdout_bytes[:max_bytes],
            stderr=stderr_bytes[:max_bytes],
            exit_code=exit_code,
        )

    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]:
        """Drain stdout+stderr (merged) as raw byte chunks.

        Hard-truncates at capabilities.max_stdout_bytes and appends
        TRUNCATION_MARKER so callers know data was dropped.
        """
        proc = self._procs.get(handle.handle_id)
        if proc is None or proc.stdout is None:
            return

        total = 0
        max_bytes = self.capabilities.max_stdout_bytes

        try:
            while True:
                chunk = await proc.stdout.read(_STREAM_CHUNK)
                if not chunk:
                    break
                remaining = max_bytes - total
                if remaining <= 0:
                    yield TRUNCATION_MARKER
                    # Drain remaining output silently to avoid blocking the subprocess.
                    await _drain_silently(proc.stdout)
                    break
                if len(chunk) > remaining:
                    yield chunk[:remaining]
                    yield TRUNCATION_MARKER
                    await _drain_silently(proc.stdout)
                    break
                yield chunk
                total += len(chunk)
        finally:
            # Ensure the process is fully reaped.
            await proc.wait()
            self._procs.pop(handle.handle_id, None)

    async def cancel(self, handle: ExecHandle, *, grace: float = 5.0) -> None:
        """Send SIGTERM to the process group, wait grace seconds, then SIGKILL.

        Using os.killpg targets the entire pgroup so orphaned grandchildren
        (e.g., background shell jobs) are also terminated.
        """
        proc = self._procs.get(handle.handle_id)
        if proc is None:
            return  # already reaped

        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return  # process already exited

        await _send_to_pgroup(pgid, signal.SIGTERM)

        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except TimeoutError:
            # SIGTERM was not enough; escalate to SIGKILL.
            await _send_to_pgroup(pgid, signal.SIGKILL)
            try:
                await proc.wait()
            except Exception:
                logger.debug("proc.wait() raised during SIGKILL cleanup", exc_info=True)

        self._procs.pop(handle.handle_id, None)

    async def close(self) -> None:
        """Cancel all live processes and clear the handle registry."""
        for handle_id, _proc in list(self._procs.items()):
            handle = ExecHandle(handle_id=handle_id, backend_name=self.name)
            await self.cancel(handle, grace=2.0)
        self._procs.clear()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _drain_silently(stream: asyncio.StreamReader) -> None:
    """Read and discard all remaining bytes from stream."""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break


async def _send_to_pgroup(pgid: int, sig: signal.Signals) -> None:
    """Send sig to process group pgid; swallow ProcessLookupError (already gone)."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM → short wait → SIGKILL for a timed-out run_separated() process."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    await _send_to_pgroup(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        await _send_to_pgroup(pgid, signal.SIGKILL)
        try:
            await proc.wait()
        except Exception:
            logger.debug("proc.wait() raised during SIGKILL after timeout", exc_info=True)


# Verify the class satisfies the Protocol at import time (cheap, no I/O).
assert isinstance(LocalBackend(), ExecutorBackend)  # noqa: S101 — design-time assertion
