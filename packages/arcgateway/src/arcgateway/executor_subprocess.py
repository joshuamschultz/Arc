"""SubprocessExecutor — federal-tier process-isolated agent runner.

Extracted from executor.py to keep the core executor module within the
arcgateway core LOC budget (ADR-004 / G1.6).

Public API is re-exported from arcgateway.executor so existing imports
``from arcgateway.executor import SubprocessExecutor, ResourceLimits``
continue to work unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import warnings
from asyncio.subprocess import PIPE, Process
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel

from arcgateway.executor import Delta, InboundEvent

_logger = logging.getLogger("arcgateway.executor")


class ResourceLimits(BaseModel):
    """Resource limits applied to each arc-agent-worker subprocess.

    These are the federal-tier defaults — aggressive ceilings that prevent a
    single runaway agent from starving the host. Operators may relax limits
    per deployment via gateway config.

    Limits map directly to POSIX resource.setrlimit constants:
      memory_mb        -> RLIMIT_AS  (virtual address space ceiling)
      cpu_seconds      -> RLIMIT_CPU (CPU time in seconds before SIGXCPU)
      file_descriptors -> RLIMIT_NOFILE (max open file descriptors)

    On non-POSIX systems (Windows) these limits are not applied and a warning
    is emitted. Arc is federal/Unix-first (per CLAUDE.md) but the gateway
    package must not crash on developer machines.

    macOS note: RLIMIT_AS raises ValueError on Darwin when the requested value
    is lower than the current hard limit (which the kernel fixes at RLIM_INFINITY).
    The preexec_fn catches ValueError per-limit so that RLIMIT_CPU and
    RLIMIT_NOFILE are still enforced even when RLIMIT_AS cannot be. On Linux
    (the federal deployment target) all three limits are enforced.

    Attributes:
        memory_mb: Max virtual address space in megabytes. Default 512 MB.
        cpu_seconds: Max CPU time in seconds. Default 60 s.
        file_descriptors: Max open file descriptors. Default 256.
    """

    memory_mb: int = 512
    cpu_seconds: int = 60
    file_descriptors: int = 256


def _make_preexec_fn(limits: ResourceLimits) -> Any:
    """Build a preexec_fn callable that applies resource limits in the child process.

    The function is called in the child process after fork() but before exec().
    We use a separate factory so the returned callable captures only the
    serialisable ``limits`` object (no file handles, sockets, or event-loop state).

    On non-POSIX platforms the factory returns None and a warning is emitted.
    Callers must check the return value before passing to asyncio.create_subprocess_exec.

    Each limit is applied independently. If a limit cannot be set (e.g. RLIMIT_AS
    on macOS Darwin raises ValueError because the kernel hard limit is RLIM_INFINITY
    and cannot be lowered), the failure is written to stderr and skipped. This
    prevents the preexec_fn from crashing the subprocess spawn on developer
    machines while still enforcing all available limits on Linux.

    Args:
        limits: ResourceLimits specifying memory, CPU, and FD ceilings.

    Returns:
        A zero-argument callable for use as preexec_fn, or None on non-POSIX.
    """
    if os.name != "posix":
        warnings.warn(
            "SubprocessExecutor: resource limits (setrlimit) are not supported "
            "on non-POSIX systems. Worker subprocess will run without limits. "
            "Arc is federal/Unix-first; deploy on Linux for full isolation.",
            stacklevel=2,
        )
        return None

    memory_bytes = limits.memory_mb * 1024 * 1024
    cpu_seconds = limits.cpu_seconds
    file_descriptors = limits.file_descriptors

    def _apply_limits() -> None:
        """Apply POSIX resource limits in the child process.

        Called by the OS after fork(), before exec(). The child has its own
        address space so modifying limits here does not affect the parent.

        Each limit is applied with an individual try/except. RLIMIT_AS raises
        ValueError on macOS (Darwin's hard limit is RLIM_INFINITY and cannot
        be lowered). RLIMIT_CPU and RLIMIT_NOFILE work on both macOS and Linux.
        On Linux (federal target) all three limits are fully enforced.
        """
        import resource  # posix-only; import inside fn to avoid ImportError on Windows
        import sys as _sys

        _to_apply = [
            # (resource constant, value pair, human-readable name)
            (resource.RLIMIT_AS, (memory_bytes, memory_bytes), "RLIMIT_AS"),
            (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds), "RLIMIT_CPU"),
            (resource.RLIMIT_NOFILE, (file_descriptors, file_descriptors), "RLIMIT_NOFILE"),
        ]
        for res_id, limit_pair, name in _to_apply:
            try:
                resource.setrlimit(res_id, limit_pair)
            except (ValueError, OSError) as _exc:
                # Write to stderr (stdout is the IPC channel). RLIMIT_AS raises
                # ValueError on macOS where the kernel hard limit is RLIM_INFINITY.
                # On Linux (federal) all three limits succeed.
                _sys.stderr.write(f"arc-agent-worker: could not set {name}={limit_pair}: {_exc}\n")

    return _apply_limits


class SubprocessExecutor:
    """Federal-tier executor: spawns arc-agent-worker in an isolated subprocess.

    Provides full OS-level process isolation with per-session:
    - Own DID anchor (passed via ``--did`` CLI arg to the worker).
    - Own httpx connection pool (no cross-session TCP connections).
    - Own ToolRegistry (plugins load fresh in each subprocess).
    - Own audit chain (worker audit events stay inside the subprocess).
    - Resource limits via POSIX resource.setrlimit (memory, CPU, FDs).

    JSON-lines IPC protocol:
      Parent writes one InboundEvent JSON line to worker stdin.
      Worker writes one or more Delta JSON lines to stdout.
      Worker stdout is closed after each event's done sentinel.
      Parent reads until done Delta (is_final=True) then collects the subprocess.

    Audit event ``gateway.session.executor_choice`` is emitted when this
    executor is selected (T1.6.4). The event is logged at INFO level so it
    appears in the gateway's structured audit log stream.

    Attributes:
        _worker_cmd: Command vector for the worker subprocess. Defaults to
            ``["arc-agent-worker"]`` (the installed console script).
        _resource_limits: Resource ceilings applied to each worker subprocess.
    """

    _DEFAULT_WORKER_CMD: ClassVar[list[str]] = ["arc-agent-worker"]

    def __init__(
        self,
        worker_cmd: list[str] | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> None:
        """Initialise SubprocessExecutor.

        Args:
            worker_cmd: Command vector to spawn the worker. Defaults to
                ``["arc-agent-worker"]`` (the installed console script).
                Override to ``[sys.executable, "-m", "arccli.agent_worker"]``
                when running from source without an installed wheel.
            resource_limits: Per-subprocess resource ceilings. Defaults to
                federal-tier values (512 MB RAM, 60 s CPU, 256 FDs).
        """
        self._worker_cmd: list[str] = (
            worker_cmd if worker_cmd is not None else list(self._DEFAULT_WORKER_CMD)
        )
        self._resource_limits: ResourceLimits = (
            resource_limits if resource_limits is not None else ResourceLimits()
        )

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Spawn arc-agent-worker subprocess and stream JSON-lines output.

        Emits ``gateway.session.executor_choice`` audit event before spawning.
        Each call spawns a fresh OS subprocess — no subprocess is reused across
        sessions, guaranteeing full isolation.

        Args:
            event: Normalised inbound event from a platform adapter.

        Returns:
            AsyncIterator[Delta] streaming the worker's output.

        Raises:
            RuntimeError: If the worker subprocess fails to start.
        """
        _logger.info(
            "gateway.session.executor_choice session_key=%s executor_type=SubprocessExecutor "
            "agent_did=%s resource_limits=%s",
            event.session_key,
            event.agent_did,
            self._resource_limits.model_dump(),
        )
        # Canonical arctrust.audit emit — executor choice is a security decision.
        from arcgateway.audit import emit_event as _arc_emit

        _arc_emit(
            action="gateway.session.executor_choice",
            target=event.session_key,
            outcome="allow",
            extra={
                "executor_type": "SubprocessExecutor",
                "agent_did": event.agent_did,
                "resource_limits": self._resource_limits.model_dump(),
            },
        )
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Internal async generator that spawns and drives the worker subprocess.

        Separated from run() so run() stays a regular coroutine (see Executor
        Protocol docstring for the rationale behind this separation).

        The subprocess lifecycle per event:
          1. Spawn via asyncio.create_subprocess_exec with stdin/stdout PIPE.
          2. Write event JSON line to stdin; close stdin to signal EOF to worker.
          3. Read stdout line-by-line; parse each as Delta.
          4. Yield each Delta to the caller.
          5. Wait for subprocess to exit; log warning if exit code != 0.
          6. Emit audit terminator Delta with subprocess PID in content.

        Args:
            event: Inbound event to process.
        """
        cmd = [*self._worker_cmd, "--did", event.agent_did]
        preexec_fn = _make_preexec_fn(self._resource_limits)

        proc: Process = await self._spawn_proc(cmd, preexec_fn)

        _logger.info(
            "gateway.session.executor_choice pid=%d session_key=%s agent_did=%s "
            "resource_limits_memory_mb=%d resource_limits_cpu_seconds=%d "
            "resource_limits_file_descriptors=%d",
            proc.pid,
            event.session_key,
            event.agent_did,
            self._resource_limits.memory_mb,
            self._resource_limits.cpu_seconds,
            self._resource_limits.file_descriptors,
        )

        event_line = event.model_dump_json() + "\n"
        assert proc.stdin is not None  # noqa: S101 — asyncio.create_subprocess_exec with PIPE guarantees stdin
        proc.stdin.write(event_line.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        assert proc.stdout is not None  # noqa: S101 — asyncio.create_subprocess_exec with PIPE guarantees stdout
        async for delta in self._read_deltas(proc.stdout, event.session_key, proc.pid):
            yield delta

        exit_code = await proc.wait()
        if exit_code != 0:
            _logger.warning(
                "arc-agent-worker exited non-zero: pid=%d exit_code=%d session_key=%s",
                proc.pid,
                exit_code,
                event.session_key,
            )

        # Emit audit terminator so the parent audit chain records subprocess boundary.
        yield Delta(
            kind="done",
            content=f"[subprocess-audit] pid={proc.pid} exit_code={exit_code}",
            is_final=True,
            turn_id=event.session_key,
        )

    async def _spawn_proc(
        self,
        cmd: list[str],
        preexec_fn: Any,
    ) -> Process:
        """Spawn the worker subprocess.

        Extracted from _stream() to keep the subprocess creation logic
        separately testable.

        Args:
            cmd: Command vector (e.g. ["arc-agent-worker", "--did", "did:..."]).
            preexec_fn: Zero-argument callable to run in child before exec(),
                or None on non-POSIX systems.

        Returns:
            Running asyncio.subprocess.Process.

        Raises:
            RuntimeError: If the process cannot be started.
        """
        kwargs: dict[str, Any] = {
            "stdin": PIPE,
            "stdout": PIPE,
            "stderr": None,  # inherit parent stderr so worker logs appear in gateway logs
        }
        if preexec_fn is not None:
            kwargs["preexec_fn"] = preexec_fn

        try:
            return await asyncio.create_subprocess_exec(*cmd, **kwargs)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"SubprocessExecutor: worker command not found: {cmd[0]!r}. "
                "Install arccli (pip install arccli) or set worker_cmd to "
                f"[sys.executable, '-m', 'arccli.agent_worker']. Error: {exc}"
            ) from exc

    async def _read_deltas(
        self,
        stdout: asyncio.StreamReader,
        session_key: str,
        pid: int,
    ) -> AsyncIterator[Delta]:
        """Parse JSON-lines from worker stdout into Delta objects.

        Malformed lines are logged and converted to error Deltas rather than
        propagating exceptions — protocol robustness requirement (T1.6.5).

        The done-sentinel from the worker is NOT re-yielded here; instead
        _stream() emits its own audit-augmented done sentinel after the
        subprocess exits. This preserves the subprocess PID in the audit trail.

        Args:
            stdout: asyncio.StreamReader connected to the worker's stdout.
            session_key: For log context.
            pid: Worker subprocess PID for log context.
        """
        while True:
            line_bytes = await stdout.readline()
            if not line_bytes:
                break  # EOF from worker stdout

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue

            try:
                data: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                _logger.warning(
                    "SubprocessExecutor: malformed JSON from worker pid=%d session=%s "
                    "error=%s line=%r",
                    pid,
                    session_key,
                    exc,
                    line[:120],
                )
                # Yield an error token so the caller knows something went wrong
                yield Delta(
                    kind="token",
                    content=f"[subprocess-error] malformed JSON from worker: {exc}",
                    is_final=False,
                    turn_id=session_key,
                )
                continue

            # Worker's done sentinel: stop reading (we'll emit our own audit done)
            if data.get("is_final"):
                break

            try:
                delta = Delta.model_validate(data)
            except Exception as exc:  # Pydantic validation error — don't crash
                _logger.warning(
                    "SubprocessExecutor: invalid Delta from worker pid=%d session=%s error=%s",
                    pid,
                    session_key,
                    exc,
                )
                continue

            yield delta
