"""Per-agent runtime context for built-in capabilities.

The ``@tool`` decorator stamps a plain async function — there's no
constructor where the tool can capture state. So workspace path,
allowed read paths, vault resolver, and the agent's
:class:`CapabilityLoader` instance live here, configured once by the
agent at startup.

State is held in :class:`contextvars.ContextVar`, NOT plain module
globals (task 27 fix). The embedded gateway (SPEC-023, canonical at
every tier) runs many ``ArcAgent`` instances concurrently in ONE
process — ``bootstrap._make_agent_factory`` + ``arcui.embedded_agents``
keep up to 32 loaded agents alive, and ``SessionRouter.handle()`` spawns
one ``asyncio.Task`` per session, so sessions for DIFFERENT agents
interleave on the same event loop. A plain module global configured by
``.startup()`` is silently overwritten by whichever agent's task most
recently called :func:`configure`, corrupting every OTHER already-loaded
agent's in-flight tool calls with the wrong workspace, audit sink, and
— critically — the wrong signing IDENTITY (OWASP ASI03: one agent's
tool call literally executing with another agent's private key). A
:class:`~contextvars.ContextVar` gives each ``asyncio.Task`` (and its
children) its own isolated value: :func:`configure` inside one agent's
turn is invisible to a sibling agent's concurrently-running turn, with
no change needed at any of the ~15 call sites that already call
:func:`configure`/:func:`workspace`/etc.

Task 27 FOLLOW-UP (hotfix, same day): a bare ContextVar is only visible to
the task that set it and any CHILD task spawned from within it — never to
a SIBLING task. ``SessionRouter.handle()`` spawns a brand-new, SIBLING
``asyncio.Task`` for every inbound turn. An agent's first-ever turn (cache
miss) happens to run ``ArcAgent.startup()`` — and thus :func:`configure`
— inside that turn's own task, so turn 1 works. But the agent is then
cached (:mod:`arcui.embedded_agents`) and every SUBSEQUENT turn gets a
fresh sibling task that never re-runs :func:`configure` — every builtin
tool call would raise "not configured" starting on message 2, forever.
Fix: :func:`snapshot` captures the fully-configured state ONCE, right
after :func:`setup_capabilities` finishes calling :func:`configure`
(agent_lifecycle.py); the immutable :class:`RuntimeSnapshot` is stored on
the ``ArcAgent`` instance; :func:`bind` — cheap, idempotent, construction-
free — re-applies it via ``.set()`` at the top of every turn-dispatch
entry point (``dispatch_stream``, ``start_tracked_run``, ``resume_stream``),
regardless of which literal task is running that turn. Background tasks
(``@background_task`` loops) are unaffected — they inherit the startup
task's context automatically via ``asyncio.create_task()``'s context-copy
and don't need re-binding (see capability_registry.py:register_task).

Tools call :func:`workspace` / :func:`allowed_paths` / :func:`loader`
/ :func:`get_secret` lazily at execute time. If unset, they raise
:class:`RuntimeError` with a clear message rather than silently
falling back — a misconfigured agent must fail loudly.
"""

from __future__ import annotations

import contextvars
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.tools._dynamic_loader import DEFAULT_IMPORT_POLICY, ImportPolicy

if TYPE_CHECKING:
    from arctrust.identity import AgentIdentity

    from arcagent.capabilities.capability_loader import CapabilityLoader

_logger = logging.getLogger("arcagent.builtins.capabilities.runtime")

_workspace_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "arcagent_builtin_workspace", default=None
)
_allowed_paths_var: contextvars.ContextVar[list[Path] | None] = contextvars.ContextVar(
    "arcagent_builtin_allowed_paths", default=None
)
_loader_var: contextvars.ContextVar[CapabilityLoader | None] = contextvars.ContextVar(
    "arcagent_builtin_loader", default=None
)
_vault_resolver_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "arcagent_builtin_vault_resolver", default=None
)
_identity_var: contextvars.ContextVar[AgentIdentity | None] = contextvars.ContextVar(
    "arcagent_builtin_identity", default=None
)
_protected_paths_var: contextvars.ContextVar[frozenset[Path]] = contextvars.ContextVar(
    "arcagent_builtin_protected_paths", default=frozenset()
)
_audit_sink_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "arcagent_builtin_audit_sink", default=None
)
_egress_proxy_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "arcagent_builtin_egress_proxy", default=None
)
_tier_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arcagent_builtin_tier", default="personal"
)
# Fail-closed default: an unconfigured runtime authors under the enterprise
# blocklist, never allow-all (the authoring gate for create_tool/update_tool).
_import_policy_var: contextvars.ContextVar[ImportPolicy] = contextvars.ContextVar(
    "arcagent_builtin_import_policy", default=DEFAULT_IMPORT_POLICY
)


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
    import_policy: ImportPolicy | None = None,
) -> None:
    """Bind per-agent runtime state for the CURRENT asyncio task.

    Called by ``ArcAgent.startup()`` (twice — see agent_lifecycle.py) and
    read by every builtin tool via the accessor functions below. Scoped to
    the calling task's context, so concurrently-running turns for other
    agents in the same process never observe this agent's values.
    """
    _workspace_var.set(workspace.resolve())
    _allowed_paths_var.set(allowed_paths)
    _loader_var.set(loader)
    _vault_resolver_var.set(vault_resolver)
    _identity_var.set(identity)
    if protected_paths is not None:
        _protected_paths_var.set(protected_paths)
    if audit_sink is not None:
        _audit_sink_var.set(audit_sink)
    if egress_proxy is not None:
        _egress_proxy_var.set(egress_proxy)
    if tier is not None:
        _tier_var.set(tier)
    if import_policy is not None:
        _import_policy_var.set(import_policy)


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Immutable capture of all ten builtin-runtime ContextVars.

    Built once via :func:`snapshot` after startup's two :func:`configure`
    calls have both run; re-applied via :func:`bind` at the top of every
    turn so state survives the sibling ``asyncio.Task`` per-turn dispatch
    creates (see module docstring, "Task 27 FOLLOW-UP").
    """

    workspace: Path
    allowed_paths: list[Path] | None
    loader: CapabilityLoader | None
    vault_resolver: Any
    identity: AgentIdentity | None
    protected_paths: frozenset[Path]
    audit_sink: Any
    egress_proxy: Any
    tier: str
    import_policy: ImportPolicy


def snapshot() -> RuntimeSnapshot:
    """Capture the CURRENT task's bound state into an immutable snapshot.

    Call once, after startup's :func:`configure` calls have both run.
    Uses :func:`workspace` (not a raw ``.get()``) so an unconfigured
    caller fails loudly here rather than producing a snapshot with a
    missing workspace that only breaks later.
    """
    return RuntimeSnapshot(
        workspace=workspace(),
        allowed_paths=_allowed_paths_var.get(),
        loader=_loader_var.get(),
        vault_resolver=_vault_resolver_var.get(),
        identity=_identity_var.get(),
        protected_paths=_protected_paths_var.get(),
        audit_sink=_audit_sink_var.get(),
        egress_proxy=_egress_proxy_var.get(),
        tier=_tier_var.get(),
        import_policy=_import_policy_var.get(),
    )


def bind(snap: RuntimeSnapshot) -> None:
    """Idempotently bind a previously-built snapshot into the CURRENT task.

    Cheap — nine ``.set()`` calls, no construction, no I/O. Called at the
    top of every turn-dispatch entry point so a turn running in a fresh
    sibling ``asyncio.Task`` (not a descendant of the task that ran
    :func:`configure`) still sees this agent's state.
    """
    _workspace_var.set(snap.workspace)
    _allowed_paths_var.set(snap.allowed_paths)
    _loader_var.set(snap.loader)
    _vault_resolver_var.set(snap.vault_resolver)
    _identity_var.set(snap.identity)
    _protected_paths_var.set(snap.protected_paths)
    _audit_sink_var.set(snap.audit_sink)
    _egress_proxy_var.set(snap.egress_proxy)
    _tier_var.set(snap.tier)
    _import_policy_var.set(snap.import_policy)


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
    identity = _identity_var.get()
    if identity is None or not identity.can_sign:
        return False
    from arcagent.capabilities import artifact_signing

    try:
        artifact_signing.write_signature(
            artifact,
            content,
            signer_did=identity.did,
            private_key=identity.signing_seed,
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
    identity = _identity_var.get()
    caller = identity.did if identity is not None else "did:arc:unknown"
    audit_sink = _audit_sink_var.get()
    if audit_sink is not None:
        try:
            audit_sink(
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
    ws = _workspace_var.get()
    if ws is None:
        raise RuntimeError(
            "builtin tool called before runtime is configured; "
            "agent must call _runtime.configure(workspace=...) at startup"
        )
    return ws


def allowed_paths() -> list[Path] | None:
    """Return the list of additional readable paths (e.g. memory dirs)."""
    return _allowed_paths_var.get()


def protected_paths() -> frozenset[Path]:
    """Return the session-immutable operator-protected path set (SPEC-035)."""
    return _protected_paths_var.get()


def egress() -> Any:
    """Return the per-agent :class:`EgressProxy` (REQ-013), or None if unwired.

    The single mediation point for outbound network calls: external-comms tools
    must route through this proxy so egress is allowlist-gated and audited. No
    tool opens its own socket.
    """
    return _egress_proxy_var.get()


def tier() -> str:
    """Return the deployment tier (personal/enterprise/federal)."""
    return _tier_var.get()


def import_policy() -> ImportPolicy:
    """Return the tier-resolved import policy for authoring workspace tools.

    The authoring gate (``create_tool``/``update_tool``) validates against this,
    so the authoring decision matches the loader's — a tool refused here is never
    one the loader would run. Unconfigured → the fail-closed enterprise default.
    """
    return _import_policy_var.get()


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
    for path in _protected_paths_var.get():
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

    identity = _identity_var.get()
    caller = identity.did if identity is not None else "did:arc:unknown"
    audit_sink = _audit_sink_var.get()
    audit = _ArcRunAuditAdapter(audit_sink) if audit_sink is not None else None
    tier_value = _tier_var.get()
    try:
        raw = await run_shell(
            command,
            tier=tier_value,
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
            details={"tier": tier_value, "reason": str(exc)},
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

    identity = _identity_var.get()
    caller = identity.did if identity is not None else "did:arc:unknown"
    return _resolve(
        file_path,
        workspace(),
        allow_symlinks=allow_symlinks,
        allowed_paths=_allowed_paths_var.get(),
        tool_name=tool_name,
        caller_did=caller,
        audit_sink=_audit_sink_var.get(),
    )


def check_protected(resolved: Path, file_path: str, *, tool_name: str) -> None:
    """Deny + audit a mutation of a protected path (REQ-001/004).

    Thin binding of :func:`arcagent.tools._validation.enforce_protected_path`
    to the per-agent runtime state (protected set, identity, audit sink).
    """
    from arcagent.tools._validation import enforce_protected_path

    identity = _identity_var.get()
    caller = identity.did if identity is not None else "did:arc:unknown"
    enforce_protected_path(
        resolved,
        _protected_paths_var.get(),
        tool_name=tool_name,
        file_path=file_path,
        caller_did=caller,
        audit_sink=_audit_sink_var.get(),
    )


def check_secret_content(content: str, file_path: str, *, tool_name: str) -> None:
    """Deny + audit a write whose payload looks like a live credential.

    Thin binding of :func:`arcagent.tools._secret_guard.enforce_no_secret_content`
    to the per-agent runtime state (identity, audit sink) — the same
    "delegate the audit-then-raise shape" pattern as :func:`check_protected`.
    """
    from arcagent.tools._secret_guard import enforce_no_secret_content

    identity = _identity_var.get()
    caller = identity.did if identity is not None else "did:arc:unknown"
    enforce_no_secret_content(
        content,
        tool_name=tool_name,
        file_path=file_path,
        caller_did=caller,
        audit_sink=_audit_sink_var.get(),
    )


def check_shell_command(command: str, *, tool_name: str = "bash") -> None:
    """Advisory host-bash goal-lock: deny obvious writes to protected paths.

    Best-effort only (OQ-2) — a host shell can evade naive parsing. Real
    enforcement at enterprise/federal is the sandbox read-only mount (REQ-023).
    """
    from arcagent.tools._validation import scan_shell_for_protected_writes

    hit = scan_shell_for_protected_writes(command, workspace(), _protected_paths_var.get())
    if hit is not None:
        check_protected(hit, str(hit), tool_name=tool_name)


def loader() -> CapabilityLoader:
    """Return the agent's :class:`CapabilityLoader`.

    Required by ``reload``, ``create_tool``, etc. Raises if unset.
    """
    current = _loader_var.get()
    if current is None:
        raise RuntimeError("self-modification tool called before loader is configured")
    return current


def get_secret(name: str) -> str | None:
    """Resolve a secret by name.

    Lookup order:

      1. Vault backend (if configured in [vault] of arcagent.toml)
      2. Environment variable (name uppercased, hyphens → underscores)

    Returns ``None`` if neither path resolves.
    """
    vault_resolver = _vault_resolver_var.get()
    if vault_resolver is not None:
        try:
            raw_val = vault_resolver.get_secret(name)
        except Exception:  # reason: fail-open — continue
            raw_val = None
        if raw_val:
            return str(raw_val)
    env_name = name.upper().replace("-", "_")
    return os.environ.get(env_name)


def reset() -> None:
    """Clear all runtime state. Test-only helper."""
    _workspace_var.set(None)
    _allowed_paths_var.set(None)
    _loader_var.set(None)
    _vault_resolver_var.set(None)
    _identity_var.set(None)
    _protected_paths_var.set(frozenset())
    _audit_sink_var.set(None)
    _egress_proxy_var.set(None)
    _tier_var.set("personal")
    _import_policy_var.set(DEFAULT_IMPORT_POLICY)


__all__ = [
    "RuntimeSnapshot",
    "allowed_paths",
    "audit_unsigned_artifact",
    "bind",
    "check_protected",
    "check_secret_content",
    "check_shell_command",
    "configure",
    "egress",
    "get_secret",
    "import_policy",
    "loader",
    "protected_paths",
    "reset",
    "resign_if_previously_signed",
    "resolve_workspace_path",
    "run_sandboxed_bash",
    "sign_artifact_file",
    "snapshot",
    "tier",
    "workspace",
]
