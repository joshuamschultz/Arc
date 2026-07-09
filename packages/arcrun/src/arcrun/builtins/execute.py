"""ExecuteTool — tier-routed, isolation-backed Python execution.

`execute_python` selects an isolation backend **by tier** and delegates code
execution to it — federal → VM (hard floor), enterprise → container, personal →
container by default. A personal-tier operator may relax down to a bare host
subprocess ("sandbox off"), but only via explicit config and always audited.
Enterprise/federal never fall back to a weaker path; unavailable required
isolation fails closed.

Boundary: arcrun stays execution-only. ``tier``/``relax`` arrive as parameters
from the caller (arccli). This module never sources tier, imports arcagent/arcllm,
or contains agent logic.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from arcrun.backends import (
    DockerBackend,
    LocalBackend,
    SupportsSeparatedRun,
    VmBackend,
    load_backend,
)
from arcrun.backends._audit import emit_backend_selected, emit_isolation_downgraded
from arcrun.backends.vm import VmUnavailableError
from arcrun.types import Tool, ToolContext

_DEFAULT_ENV = {
    # /usr/local/bin FIRST: the official python container image installs
    # python3 there, and omitting it hid the interpreter from `docker exec`.
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "en_US.UTF-8",
}

# Personal-tier relax values that turn the sandbox fully OFF → LocalBackend.
_OFF_VALUES = frozenset({"off", "none", "local"})

# Backend-name → declared isolation level (for audit record content).
_ISOLATION = {"vm": "vm", "docker": "container", "local": "none"}


class ExecutionIsolationError(Exception):
    """Base for tier-routing refusals — a distinct type, never a 'none' backend."""


class IsolationUnavailableError(ExecutionIsolationError):
    """Federal requires VM isolation but the platform cannot provide it (fail closed)."""


class IsolationRelaxationError(ExecutionIsolationError):
    """A relax value is not permitted for the given tier (below the tier floor)."""


def resolve_execution_backend(
    tier: str,
    *,
    relax: str | None,
    platform_supports_vm: bool,
) -> str:
    """Map ``(tier, relax, platform_supports_vm)`` → loader backend name.

    Pure and side-effect-free: no host probe, no audit. ``platform_supports_vm``
    is an injected fact so the decision is testable and TOCTOU-free; the caller
    emits audit and the backend's own KVM check is defence-in-depth.

    Returns one of ``"vm"``, ``"docker"``, ``"local"``.

    Raises:
        IsolationUnavailableError: federal tier with no VM support (refuse).
        IsolationRelaxationError:  a relax value below the tier's isolation floor.
        ValueError:                an unknown tier string.
    """
    tier_norm = tier.lower()
    relax_norm = relax.lower() if relax is not None else None

    if tier_norm == "federal":
        if relax_norm is not None:
            raise IsolationRelaxationError(
                f"federal tier cannot be relaxed below its VM floor (relax={relax!r})."
            )
        if not platform_supports_vm:
            raise IsolationUnavailableError(
                "federal tier requires VM isolation but the platform has no "
                "/dev/kvm (or is not Linux); refusing to downgrade."
            )
        return "vm"

    if tier_norm == "enterprise":
        if relax_norm is not None and relax_norm != "container":
            raise IsolationRelaxationError(
                f"enterprise tier cannot be relaxed below its container floor (relax={relax!r})."
            )
        return "docker"

    if tier_norm == "personal":
        if relax_norm is None or relax_norm == "container":
            return "docker"
        if relax_norm in _OFF_VALUES:
            return "local"
        raise IsolationRelaxationError(
            f"unknown relax value {relax!r}; use 'container', 'local', or 'off'."
        )

    raise ValueError(f"unknown tier {tier!r}; expected personal, enterprise, or federal.")


def platform_supports_vm(kvm_path: str = "/dev/kvm") -> bool:
    """Upstream capability probe: Linux with an accessible ``/dev/kvm``.

    Kept out of the pure router so the routing decision stays side-effect-free.
    """
    return sys.platform.startswith("linux") and Path(kvm_path).exists()


# Private alias so run_shell can probe the host without its ``platform_supports_vm``
# parameter shadowing the module-level probe function of the same name.
_probe_platform_vm = platform_supports_vm


def _downgrade_reason(relax: str | None) -> str | None:
    """Reason string when a tier-permitted downgrade happened, else None.

    The only tier-permitted downgrade is a personal operator explicitly relaxing
    isolation OFF (sandbox off). A no-KVM host is NOT a downgrade for personal or
    enterprise — their floor is the container, which they still get; federal
    refuses rather than downgrades, so it never reaches here.
    """
    if relax is not None and relax.lower() in _OFF_VALUES:
        return f"personal operator relaxed isolation to {relax!r} (sandbox off)"
    return None


def make_execute_tool(
    *,
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    extra_env: dict[str, str] | None = None,
    tier: str = "personal",
    relax: str | None = None,
    caller_did: str | None = None,
    audit_sink: Any | None = None,
) -> Tool:
    """Create a tier-routed Python execution tool.

    Selection happens once at build time (single audit-emission point): the tool
    resolves its backend, emits ``code_exec.backend.selected`` (and, for a
    tier-permitted downgrade, ``code_exec.isolation.downgraded``), then delegates
    every execution to that backend.

    Args:
        timeout_seconds: Maximum wall-clock execution time.
        max_output_bytes: Cap on captured stdout/stderr.
        extra_env: Extra environment variables for the guest/subprocess.
        tier: Deployment tier — routes isolation: federal→vm, enterprise→container,
            personal→container (default). Sourced by the caller, never here.
        relax: Personal-only isolation relaxation ('container' | 'local' | 'off').
            Rejected at enterprise/federal below their floor.
        caller_did: Identity attributed to the backend-selection audit event.
        audit_sink: arctrust AuditSink; receives selection/downgrade events.

    Raises:
        IsolationUnavailableError: federal tier on a host with no VM support.
        IsolationRelaxationError:  a relax value below the tier floor.
    """
    supports_vm = platform_supports_vm()
    try:
        backend_name = resolve_execution_backend(
            tier, relax=relax, platform_supports_vm=supports_vm
        )
    except ExecutionIsolationError:
        emit_backend_selected(
            tier=tier,
            resolved="<refused>",
            isolation="none",
            caller_did=caller_did,
            relax=relax,
            relax_reason=relax or "",
            platform_supports_vm=supports_vm,
            outcome="refuse",
            sink=audit_sink,
        )
        raise

    isolation = _ISOLATION[backend_name]
    emit_backend_selected(
        tier=tier,
        resolved=backend_name,
        isolation=isolation,
        caller_did=caller_did,
        relax=relax,
        relax_reason=relax or "",
        platform_supports_vm=supports_vm,
        outcome="allow",
        sink=audit_sink,
    )
    reason = _downgrade_reason(relax)
    if reason is not None:
        emit_isolation_downgraded(
            tier=tier,
            resolved=backend_name,
            reason=reason,
            caller_did=caller_did,
            sink=audit_sink,
        )

    env = {**_DEFAULT_ENV, **(extra_env or {})}
    # Local runs the host interpreter directly (sandbox off); guests run their own.
    interpreter = sys.executable if backend_name == "local" else "python3"

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        code = params["code"]
        start = time.time()

        backend = load_backend(backend_name, tier=tier, audit_sink=audit_sink)
        # All built-in backends implement run_separated; guard keeps mypy honest.
        if not isinstance(backend, SupportsSeparatedRun):  # pragma: no cover
            raise ExecutionIsolationError(
                f"backend {backend_name!r} does not support separated execution."
            )

        try:
            # Feed the source over stdin to ``python3 -`` so it is staged INSIDE
            # the isolation boundary (container/VM guest), never referenced by a
            # host path the guest cannot see. ``/tmp`` is the writable scratch dir
            # in every backend (container tmpfs, host, VM guest). The personal-off
            # path pipes to the same reader on the host.
            result = await backend.run_separated(
                f"{shlex.quote(interpreter)} -",
                cwd="/tmp",
                env=env,
                timeout=timeout_seconds,
                stdin=code,
            )
        finally:
            await backend.close()

        duration_ms = (time.time() - start) * 1000
        return json.dumps(
            {
                "stdout": result.stdout[:max_output_bytes].decode(errors="replace"),
                "stderr": result.stderr[:max_output_bytes].decode(errors="replace"),
                "exit_code": result.exit_code,
                "duration_ms": round(duration_ms, 1),
            }
        )

    return Tool(
        name="execute_python",
        description=(
            "Execute Python code in a tier-isolated backend (VM / container / "
            "host per deployment tier). Returns stdout, stderr, exit_code, and duration."
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


_DEFAULT_SHELL_MAX_OUTPUT = 65536


def _build_workspace_backend(
    backend_name: str,
    *,
    workspace: Path,
    readonly_subpaths: list[Path] | None,
    max_stdout_bytes: int,
) -> DockerBackend | VmBackend | LocalBackend:
    """Construct the resolved backend WITH the workspace mount + protected subpaths.

    Unlike ``load_backend`` (which builds no-arg built-ins for execute_python),
    run_shell needs the backend bound to the agent's workspace. The three names
    ``resolve_execution_backend`` returns are all trusted built-ins.
    """
    if backend_name == "vm":
        return VmBackend(
            max_stdout_bytes=max_stdout_bytes,
            workspace_mount=workspace,
            readonly_subpaths=readonly_subpaths,
        )
    if backend_name == "docker":
        return DockerBackend(
            max_stdout_bytes=max_stdout_bytes,
            workspace_mount=workspace,
            readonly_subpaths=readonly_subpaths,
        )
    # Personal-off (sandbox off): the host subprocess runs directly in the
    # workspace; there is no mount to apply.
    return LocalBackend(max_stdout_bytes=max_stdout_bytes)


async def run_shell(
    command: str,
    *,
    tier: str,
    workspace: Path,
    readonly_subpaths: list[Path] | None = None,
    caller_did: str | None = None,
    audit_sink: Any | None = None,
    platform_supports_vm: bool | None = None,
    relax: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Run a shell ``command`` inside the tier-routed isolation backend.

    Mirrors ``make_execute_tool``'s selection + audit shape but runs a shell
    command against the agent's workspace instead of piping ``python3 -``:
    the backend is bound to ``workspace`` (rw at ``/workspace``, protected
    subpaths read-only, host ``~/.arc`` absent) and the command runs with
    ``cwd=/workspace``.

    ``tier`` and ``platform_supports_vm`` are parameters — arcrun never sources
    them. Personal (host bash) is arcagent's job; run_shell is invoked for
    enterprise/federal, but a personal call still routes through
    ``resolve_execution_backend`` rather than being special-cased.

    Returns the same JSON result shape as execute_python: ``stdout``, ``stderr``,
    ``exit_code``, ``duration_ms``.

    Raises:
        IsolationUnavailableError: federal tier on a host with no VM support.
        IsolationRelaxationError:  a relax value below the tier floor.
    """
    supports_vm = _probe_platform_vm() if platform_supports_vm is None else platform_supports_vm
    try:
        backend_name = resolve_execution_backend(
            tier, relax=relax, platform_supports_vm=supports_vm
        )
    except ExecutionIsolationError:
        emit_backend_selected(
            tier=tier,
            resolved="<refused>",
            isolation="none",
            caller_did=caller_did,
            relax=relax,
            relax_reason=relax or "",
            platform_supports_vm=supports_vm,
            outcome="refuse",
            sink=audit_sink,
        )
        raise

    isolation = _ISOLATION[backend_name]
    emit_backend_selected(
        tier=tier,
        resolved=backend_name,
        isolation=isolation,
        caller_did=caller_did,
        relax=relax,
        relax_reason=relax or "",
        platform_supports_vm=supports_vm,
        outcome="allow",
        sink=audit_sink,
    )
    reason = _downgrade_reason(relax)
    if reason is not None:
        emit_isolation_downgraded(
            tier=tier,
            resolved=backend_name,
            reason=reason,
            caller_did=caller_did,
            sink=audit_sink,
        )

    backend = _build_workspace_backend(
        backend_name,
        workspace=workspace,
        readonly_subpaths=readonly_subpaths,
        max_stdout_bytes=_DEFAULT_SHELL_MAX_OUTPUT,
    )
    # Guests run under /workspace; the personal-off host subprocess uses the real path.
    cwd = str(workspace) if backend_name == "local" else "/workspace"

    start = time.time()
    try:
        result = await backend.run_separated(command, cwd=cwd, env=_DEFAULT_ENV, timeout=timeout)
    finally:
        await backend.close()

    duration_ms = (time.time() - start) * 1000
    return json.dumps(
        {
            "stdout": result.stdout.decode(errors="replace"),
            "stderr": result.stderr.decode(errors="replace"),
            "exit_code": result.exit_code,
            "duration_ms": round(duration_ms, 1),
        }
    )


__all__ = [
    "ExecutionIsolationError",
    "IsolationRelaxationError",
    "IsolationUnavailableError",
    "VmUnavailableError",
    "make_execute_tool",
    "platform_supports_vm",
    "resolve_execution_backend",
    "run_shell",
]
