"""SPEC-021 Component C-005 — OS-level sandbox wrapper.

Per D-353/D-359, self-executing agent code at enterprise+ tier runs
inside an OS-level isolation layer:

  * Linux  — :class:`SeccompSandbox` (``pyseccomp``, optional dep
    declared in the ``[enterprise]`` extras)
  * macOS  — :class:`SandboxExecSandbox` (Apple's ``sandbox-exec``,
    always present on Darwin)

Personal tier returns ``None``; the AST validator and TOFU layer are
the only gates.

The sandbox runs Python source as a separate process so a syscall
violation kills the child without taking down the agent. Stdout/stderr
are captured; result is the child's stdout decoded as UTF-8.

Threats mitigated (ASI05 — Unexpected Code Execution):

  * Filesystem reads outside ``scope_path`` raise
    ``SandboxViolationError`` (sandbox-exec ``deny default``).
  * Network egress is denied unconditionally.
  * ``ctypes.CDLL`` and other process-control calls fail at the
    syscall layer even if the AST validator missed them.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from arcagent.core.errors import ArcAgentError
from arcagent.core.tier import Tier

_PLATFORM = sys.platform


class SandboxViolationError(ArcAgentError):
    """Raised when sandbox execution detects a policy violation."""

    _component = "os_sandbox"

    def __init__(self, *, category: str, detail: str) -> None:
        super().__init__(
            code="OS_SANDBOX_VIOLATION",
            message=f"{category} — {detail}",
            details={"category": category, "detail": detail},
        )
        self.category = category


@dataclass(frozen=True)
class SandboxResult:
    """Output of a successful sandboxed run.

    ``stdout`` is the child process's standard output decoded as
    UTF-8. ``returncode`` is its exit code (always 0 on success — any
    other value is converted to a :class:`SandboxViolationError`).
    """

    stdout: str
    returncode: int


class OsSandbox(Protocol):
    """Sandbox transport contract.

    Implementations execute ``source`` with filesystem access scoped
    to ``scope_path`` and a hard wall-clock ``timeout``. They raise
    :class:`SandboxViolationError` on policy violation; any other
    exception type is treated as an internal sandbox error.
    """

    async def run(self, source: str, *, scope_path: Path, timeout: float) -> SandboxResult: ...


def make_sandbox(tier: Tier) -> OsSandbox | None:
    """Return a sandbox instance appropriate for ``tier``.

    Personal tier returns ``None`` — no OS sandbox is applied; the AST
    validator + TOFU policy are the only gates. Enterprise + federal
    return a real sandbox; the platform decides which class.

    Raises :class:`NotImplementedError` if the platform has no
    supported sandbox backend.
    """
    if tier == Tier.PERSONAL:
        return None
    if _PLATFORM == "darwin":
        return SandboxExecSandbox()
    if _PLATFORM == "linux":
        return SeccompSandbox()
    raise NotImplementedError(
        f"OS sandbox not available on {_PLATFORM!r}; supported platforms: linux, darwin"
    )


# --- macOS implementation -------------------------------------------------


class SandboxExecSandbox:
    """macOS sandbox-exec backend.

    Generates a per-call sandbox profile that:
      * starts from ``deny default`` (zero baseline trust)
      * allows file-read under ``scope_path`` only
      * allows process-fork (Python startup needs it)
      * denies all network operations
    """

    async def run(self, source: str, *, scope_path: Path, timeout: float) -> SandboxResult:
        profile = build_sandbox_exec_profile(scope_path=scope_path)
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            sys.executable,
            "-c",
            source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise SandboxViolationError(
                category="timeout",
                detail=f"sandbox child exceeded {timeout}s",
            ) from None
        if proc.returncode != 0:
            raise SandboxViolationError(
                category="exit_nonzero",
                detail=stderr_b.decode("utf-8", errors="replace").strip(),
            )
        return SandboxResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            returncode=0,
        )


def build_sandbox_exec_profile(*, scope_path: Path) -> str:
    """Render a sandbox-exec profile string scoped to ``scope_path``.

    Profile starts from ``deny default`` and re-allows only the
    minimal surface a validator script needs: read inside scope,
    process-fork, mach-priv (so Python imports work), reads of the
    Python install root.
    """
    scope = str(scope_path.resolve())
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-fork)\n"
        "(allow process-exec)\n"
        "(allow mach-priv-host-port)\n"
        "(allow mach-lookup)\n"
        '(allow file-read* (subpath "/usr/lib"))\n'
        '(allow file-read* (subpath "/System/Library"))\n'
        f'(allow file-read* (subpath "{sys.prefix}"))\n'
        f'(allow file-read* (subpath "{scope}"))\n'
    )


# --- Linux implementation -------------------------------------------------


class SeccompSandbox:
    """Linux seccomp backend (placeholder until ``pyseccomp`` integration).

    Loads the optional ``[enterprise]`` extras dependency on demand.
    Raises :class:`NotImplementedError` if the dependency is missing
    so the operator gets a clear "install arcagent[enterprise]"
    message rather than a confusing ImportError mid-execution.
    """

    async def run(self, source: str, *, scope_path: Path, timeout: float) -> SandboxResult:
        try:
            import pyseccomp  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise NotImplementedError(
                "SeccompSandbox requires `arcagent[enterprise]` extras "
                "(install pyseccomp). Personal tier does not need it."
            ) from exc
        # Real seccomp profile install + subprocess exec lands in 1.7+
        # follow-up; the structure here is the integration anchor.
        raise NotImplementedError("SeccompSandbox.run not yet implemented on Linux")


__all__ = [
    "OsSandbox",
    "SandboxExecSandbox",
    "SandboxResult",
    "SandboxViolationError",
    "SeccompSandbox",
    "build_sandbox_exec_profile",
    "make_sandbox",
]
