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

from arcrun.backends import SupportsSeparatedRun, load_backend
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
                "federal tier cannot be relaxed below its VM floor (relax="
                f"{relax!r})."
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
                "enterprise tier cannot be relaxed below its container floor "
                f"(relax={relax!r})."
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


__all__ = [
    "ExecutionIsolationError",
    "IsolationRelaxationError",
    "IsolationUnavailableError",
    "VmUnavailableError",
    "make_execute_tool",
    "platform_supports_vm",
    "resolve_execution_backend",
]
