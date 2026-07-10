"""Per-agent runtime context for built-in capabilities.

The ``@tool`` decorator stamps a plain async function — there's no
constructor where the tool can capture state. So workspace path,
allowed read paths, vault resolver, and the agent's
:class:`CapabilityLoader` instance live here as module-level state,
configured once by the agent at startup.

Setting these is *not* a global event bus or shared singleton across
multiple agents; one agent process owns one set of values. If two
agents ever shared one process they would step on each other — but
the existing arcagent runtime model is single-agent-per-process, so
this matches.

Tools call :func:`workspace` / :func:`allowed_paths` / :func:`loader`
/ :func:`get_secret` lazily at execute time. If unset, they raise
:class:`RuntimeError` with a clear message rather than silently
falling back — a misconfigured agent must fail loudly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arctrust.identity import AgentIdentity

    from arcagent.capabilities.capability_loader import CapabilityLoader

_logger = logging.getLogger("arcagent.builtins.capabilities.runtime")

_workspace: Path | None = None
_allowed_paths: list[Path] | None = None
_loader: CapabilityLoader | None = None
_vault_resolver: Any = None
_identity: AgentIdentity | None = None
_protected_paths: frozenset[Path] = frozenset()
_audit_sink: Any = None
_egress_proxy: Any = None
_tier: str = "personal"


def configure(
    *,
    workspace: Path,
    allowed_paths: list[Path] | None = None,
    loader: CapabilityLoader | None = None,
    vault_resolver: Any = None,
    identity: AgentIdentity | None = None,
    protected_paths: frozenset[Path] | None = None,
    audit_sink: Any = None,
    egress_proxy: Any = None,
    tier: str | None = None,
) -> None:
    """Bind per-agent runtime state. Called once at agent startup.

    Subsequent calls overwrite — used by tests to reset between
    runs. Production agents should call exactly once during startup.
    """
    global _workspace, _allowed_paths, _loader, _vault_resolver, _identity
    global _protected_paths, _audit_sink, _egress_proxy, _tier
    _workspace = workspace.resolve()
    _allowed_paths = allowed_paths
    _loader = loader
    _vault_resolver = vault_resolver
    _identity = identity
    if protected_paths is not None:
        _protected_paths = protected_paths
    if audit_sink is not None:
        _audit_sink = audit_sink
    if egress_proxy is not None:
        _egress_proxy = egress_proxy
    if tier is not None:
        _tier = tier


def sign_artifact_file(artifact: Path, content: bytes) -> bool:
    """Sign an agent-authored artifact with the agent's own DID key.

    Returns True iff ``artifact`` now carries a valid signature over
    ``content``. Returns False — NEVER raises — when the agent has no
    signing identity (verify-only, or an unconfigured test harness) or the
    underlying signing operation itself fails (crypto error, disk error).

    Doctrine (packages/arcagent/CLAUDE.md, task #28 "fail honest"): a False
    return MUST be surfaced to the model by the caller (via
    :func:`audit_unsigned_artifact`) — the write already happened, so a
    signing failure must not look like a plain success. The loader's
    per-tier gate then decides whether an unsigned artifact may still run
    (only personal may relax).
    """
    if _identity is None or not _identity.can_sign:
        return False
    from arcagent.capabilities import artifact_signing

    try:
        artifact_signing.write_signature(
            artifact,
            content,
            signer_did=_identity.did,
            private_key=_identity.signing_seed,
        )
    except Exception:  # reason: signing must never crash the tool — caller reports UNSIGNED
        _logger.exception("Signing failed for %s", artifact)
        return False
    return True


def resign_if_previously_signed(artifact: Path, content: bytes) -> bool | None:
    """Refresh a stale signature after a GENERIC tool mutates a signed artifact.

    Task #28 root cause: create_skill/create_tool/update_skill/update_tool all
    sign on write, but ``write``/``edit`` are general-purpose tools that know
    nothing about the Sign pillar — an agent that hand-edits an already-signed
    ``SKILL.md`` or ``capabilities/*.py`` with the plain ``edit``/``write``
    tool (instead of the matching self-modification tool) silently leaves a
    stale ``.arcsig`` sidecar: bytes changed, signature didn't. The next
    load-time verify fails closed (the reported symptom: "artifact_sha256 no
    longer matches content").

    The presence of a ``.arcsig`` sidecar IS the signal that a file
    participates in the Sign pillar — checking for it (rather than a
    hardcoded ``capabilities/`` path prefix) means write/edit never start
    signing ordinary workspace files, only keep a promise that already
    existed.

    Returns None when the artifact was never signed (nothing to do — no
    warning needed). Returns True/False when a signature already existed and
    the refresh succeeded/failed; False MUST be surfaced to the model by the
    caller (via :func:`audit_unsigned_artifact`).
    """
    from arcagent.capabilities.artifact_signing import sidecar_path

    if not sidecar_path(artifact).exists():
        return None
    return sign_artifact_file(artifact, content)


def audit_unsigned_artifact(artifact: Path, *, tool_name: str) -> str:
    """Audit an unsigned (or now-unsigned) artifact; return a warning suffix.

    Doctrine (task #28, "fail honest"): every caller whose
    :func:`sign_artifact_file`/:func:`resign_if_previously_signed` call
    returned False MUST append the returned string to its success message —
    never report plain "Created"/"Updated"/"Written" when the artifact will
    in fact be denied at next load.
    """
    caller = _identity.did if _identity is not None else "did:arc:unknown"
    if _audit_sink is not None:
        try:
            _audit_sink(
                "tool.artifact_unsigned",
                {"tool": tool_name, "actor_did": caller, "path": str(artifact)},
            )
        except Exception:  # reason: fail-open — audit must not mask the warning
            _logger.exception("Unsigned-artifact audit sink raised; continuing")
    return (
        f" WARNING: {artifact.name} is UNSIGNED and will be denied at next load "
        "(TOFU) unless this tier relaxes the signature requirement."
    )


def workspace() -> Path:
    """Return the current agent's workspace root.

    Raises ``RuntimeError`` if :func:`configure` has not been called.
    """
    if _workspace is None:
        raise RuntimeError(
            "builtin tool called before runtime is configured; "
            "agent must call _runtime.configure(workspace=...) at startup"
        )
    return _workspace


def allowed_paths() -> list[Path] | None:
    """Return the list of additional readable paths (e.g. memory dirs)."""
    return _allowed_paths


def protected_paths() -> frozenset[Path]:
    """Return the session-immutable operator-protected path set (SPEC-035)."""
    return _protected_paths


def egress() -> Any:
    """Return the per-agent :class:`EgressProxy` (REQ-013), or None if unwired.

    The single mediation point for outbound network calls: external-comms tools
    must route through this proxy so egress is allowlist-gated and audited. No
    tool opens its own socket.
    """
    return _egress_proxy


def tier() -> str:
    """Return the deployment tier (personal/enterprise/federal)."""
    return _tier


class _ArcRunAuditAdapter:
    """Adapt arcrun's ``AuditSink.write(AuditEvent)`` to the (event, payload) sink.

    arcrun emits backend-selection audit as ``arctrust.AuditEvent`` objects via a
    ``.write`` sink; arcagent's telemetry consumes ``(action, payload)``. This
    thin adapter bridges the two so REQ-025 backend-selection records reach the
    agent's audit trail without arcrun learning about arcagent telemetry.
    """

    def __init__(self, sink: Any) -> None:
        self._sink = sink

    def write(self, event: Any) -> None:
        payload = {
            "actor_did": getattr(event, "actor_did", ""),
            "target": getattr(event, "target", ""),
            "outcome": getattr(event, "outcome", ""),
            "tier": getattr(event, "tier", None),
            **dict(getattr(event, "extra", {}) or {}),
        }
        self._sink(getattr(event, "action", "code_exec.backend.selected"), payload)


def _readonly_protected_subpaths() -> list[Path]:
    """Protected files inside the workspace, as workspace-RELATIVE paths.

    arcrun's backend mounts each as ``{workspace}/{sub}:/workspace/{sub}:ro``, so
    ``sub`` must be relative to the workspace root (REQ-023 read-only mounts).
    """
    ws = workspace()
    subs: list[Path] = []
    for path in _protected_paths:
        try:
            relative = path.relative_to(ws)
        except ValueError:
            continue
        if path.exists():
            subs.append(relative)
    return subs


async def run_sandboxed_bash(command: str, *, timeout: int = 120) -> str:
    """Run a shell command through arcrun's tier-routed isolation backend.

    SPEC-035 REQ-020/022/023/025. Enterprise → container, federal → VM (SPEC-036).
    The workspace is bind-mounted read-write; protected files are mounted
    read-only (goal-lock survives the sandbox); host ``~/.arc``/``.audit`` are
    never mounted (operator seed + WORM chains unreachable). Fails closed when
    the required isolation is unavailable.
    """
    import json

    from arcrun import run_shell
    from arcrun.builtins import ExecutionIsolationError

    from arcagent.core.errors import ToolError

    caller = _identity.did if _identity is not None else "did:arc:unknown"
    audit = _ArcRunAuditAdapter(_audit_sink) if _audit_sink is not None else None
    try:
        raw = await run_shell(
            command,
            tier=_tier,
            workspace=workspace(),
            readonly_subpaths=_readonly_protected_subpaths(),
            caller_did=caller,
            audit_sink=audit,
            timeout=float(timeout),
        )
    except ExecutionIsolationError as exc:
        raise ToolError(
            code="TOOL_SANDBOX_UNAVAILABLE",
            message=f"Sandboxed bash refused: {exc}",
            details={"tier": _tier, "reason": str(exc)},
        ) from exc

    result = json.loads(raw)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    output = "\n".join(part for part in (stdout, stderr) if part)
    exit_code = result.get("exit_code", 0)
    if exit_code != 0:
        return f"Exit code: {exit_code}\n{output}"
    return output if output else "(no output)"


def resolve_workspace_path(
    file_path: str, *, tool_name: str, allow_symlinks: bool = False
) -> Path:
    """Resolve ``file_path`` within the agent's own workspace boundary.

    Single choke point for every built-in tool that accepts a caller-supplied
    path — file tools (read/write/edit/ls/find/grep) and self-modification
    tools (create_tool/update_tool/create_skill/update_skill) alike route
    through here, never through :mod:`arcagent.tools._validation` directly.
    That's what makes cross-agent escapes (SPEC-035-adjacent incident: an
    agent's own ``write`` tool installed files in a SIBLING agent's
    workspace) both impossible by construction and audited in one place.

    Thin binding of :func:`arcagent.tools._validation.resolve_workspace_path`
    to the per-agent runtime state (workspace, allowed paths, identity,
    audit sink). Raises ``ToolError`` on denial; the denial is audited
    before it is raised.
    """
    from arcagent.tools._validation import resolve_workspace_path as _resolve

    caller = _identity.did if _identity is not None else "did:arc:unknown"
    return _resolve(
        file_path,
        workspace(),
        allow_symlinks=allow_symlinks,
        allowed_paths=_allowed_paths,
        tool_name=tool_name,
        caller_did=caller,
        audit_sink=_audit_sink,
    )


def check_protected(resolved: Path, file_path: str, *, tool_name: str) -> None:
    """Deny + audit a mutation of a protected path (REQ-001/004).

    Thin binding of :func:`arcagent.tools._validation.enforce_protected_path`
    to the per-agent runtime state (protected set, identity, audit sink).
    """
    from arcagent.tools._validation import enforce_protected_path

    caller = _identity.did if _identity is not None else "did:arc:unknown"
    enforce_protected_path(
        resolved,
        _protected_paths,
        tool_name=tool_name,
        file_path=file_path,
        caller_did=caller,
        audit_sink=_audit_sink,
    )


def check_secret_content(content: str, file_path: str, *, tool_name: str) -> None:
    """Deny + audit a write whose payload looks like a live credential.

    Thin binding of :func:`arcagent.tools._secret_guard.enforce_no_secret_content`
    to the per-agent runtime state (identity, audit sink) — the same
    "delegate the audit-then-raise shape" pattern as :func:`check_protected`.
    """
    from arcagent.tools._secret_guard import enforce_no_secret_content

    caller = _identity.did if _identity is not None else "did:arc:unknown"
    enforce_no_secret_content(
        content,
        tool_name=tool_name,
        file_path=file_path,
        caller_did=caller,
        audit_sink=_audit_sink,
    )


def check_shell_command(command: str, *, tool_name: str = "bash") -> None:
    """Advisory host-bash goal-lock: deny obvious writes to protected paths.

    Best-effort only (OQ-2) — a host shell can evade naive parsing. Real
    enforcement at enterprise/federal is the sandbox read-only mount (REQ-023).
    """
    from arcagent.tools._validation import scan_shell_for_protected_writes

    hit = scan_shell_for_protected_writes(command, workspace(), _protected_paths)
    if hit is not None:
        check_protected(hit, str(hit), tool_name=tool_name)


def loader() -> CapabilityLoader:
    """Return the agent's :class:`CapabilityLoader`.

    Required by ``reload``, ``create_tool``, etc. Raises if unset.
    """
    if _loader is None:
        raise RuntimeError("self-modification tool called before loader is configured")
    return _loader


def get_secret(name: str) -> str | None:
    """Resolve a secret by name.

    Lookup order:

      1. Vault backend (if configured in [vault] of arcagent.toml)
      2. Environment variable (name uppercased, hyphens → underscores)

    Returns ``None`` if neither path resolves.
    """
    if _vault_resolver is not None:
        try:
            raw_val = _vault_resolver.get_secret(name)
        except Exception:  # reason: fail-open — continue
            raw_val = None
        if raw_val:
            return str(raw_val)
    env_name = name.upper().replace("-", "_")
    return os.environ.get(env_name)


def reset() -> None:
    """Clear all runtime state. Test-only helper."""
    global _workspace, _allowed_paths, _loader, _vault_resolver, _identity
    global _protected_paths, _audit_sink, _egress_proxy, _tier
    _workspace = None
    _allowed_paths = None
    _loader = None
    _vault_resolver = None
    _identity = None
    _protected_paths = frozenset()
    _audit_sink = None
    _egress_proxy = None
    _tier = "personal"


__all__ = [
    "allowed_paths",
    "audit_unsigned_artifact",
    "check_protected",
    "check_secret_content",
    "check_shell_command",
    "configure",
    "egress",
    "get_secret",
    "loader",
    "protected_paths",
    "reset",
    "resign_if_previously_signed",
    "resolve_workspace_path",
    "run_sandboxed_bash",
    "sign_artifact_file",
    "tier",
    "workspace",
]
