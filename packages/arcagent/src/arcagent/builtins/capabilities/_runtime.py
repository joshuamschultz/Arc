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

from arcprompt import PromptSource, StockPromptSource

from arcagent.tools._dynamic_loader import DEFAULT_IMPORT_POLICY, ImportPolicy

if TYPE_CHECKING:
    from arctrust.identity import AgentIdentity

    from arcagent.capabilities.capability_loader import CapabilityLoader
    from arcagent.capabilities.skill_files import SkillFiles

_logger = logging.getLogger("arcagent.builtins.capabilities.runtime")

_workspace_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "arcagent_builtin_workspace", default=None
)
_allowed_paths_var: contextvars.ContextVar[list[Path] | None] = contextvars.ContextVar(
    "arcagent_builtin_allowed_paths", default=None
)
# The directory the LLM's file/exec tools operate in (bash cwd + relative-path root).
# None → falls back to the workspace. Set to a TRUSTED project dir only for agents that
# opt in (tools.operate_in_launch_dir), so a coding agent works in your project while its
# own state (memory/sessions/identity) still lives in the workspace. It is always a subset
# of workspace + allowed_paths — never a new access path (see agent_lifecycle).
_working_dir_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "arcagent_builtin_working_dir", default=None
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
# Operator capability roots (``<agent_root>/capabilities``, the global root) the
# generic tools may never touch; ``<workspace>/capabilities/skills`` is always
# added on top (J4 B5) — see :func:`protected_trees`.
_protected_trees_var: contextvars.ContextVar[frozenset[Path]] = contextvars.ContextVar(
    "arcagent_builtin_protected_trees", default=frozenset()
)
# Verified, jailed reader of skill bundle files (read_skill_file / run_skill_script).
_skill_files_var: contextvars.ContextVar[SkillFiles | None] = contextvars.ContextVar(
    "arcagent_builtin_skill_files", default=None
)
# ArcRun isolation relaxation (personal only) for run_skill_script.
_isolation_relax_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arcagent_builtin_isolation_relax", default=None
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
# The agent's overlay-aware prompt lookup (COMP-030); None → stock (unconfigured).
_prompt_source_var: contextvars.ContextVar[PromptSource | None] = contextvars.ContextVar(
    "arcagent_builtin_prompt_source", default=None
)


def configure(
    *,
    workspace: Path,
    allowed_paths: list[Path] | None = None,
    working_dir: Path | None = None,
    loader: CapabilityLoader | None = None,
    vault_resolver: Any = None,
    identity: AgentIdentity | None = None,
    protected_paths: frozenset[Path] | None = None,
    audit_sink: Any = None,
    egress_proxy: Any = None,
    tier: str | None = None,
    import_policy: ImportPolicy | None = None,
    prompt_source: PromptSource | None = None,
    protected_trees: frozenset[Path] | None = None,
    skill_files: SkillFiles | None = None,
    isolation_relax: str | None = None,
) -> None:
    """Bind per-agent runtime state for the CURRENT asyncio task.

    Called by ``ArcAgent.startup()`` (twice — see agent_lifecycle.py) and
    read by every builtin tool via the accessor functions below. Scoped to
    the calling task's context, so concurrently-running turns for other
    agents in the same process never observe this agent's values.
    """
    _workspace_var.set(workspace.resolve())
    _allowed_paths_var.set(allowed_paths)
    _working_dir_var.set(working_dir.resolve() if working_dir is not None else None)
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
    if prompt_source is not None:
        _prompt_source_var.set(prompt_source)
    if protected_trees is not None:
        _protected_trees_var.set(protected_trees)
    if skill_files is not None:
        _skill_files_var.set(skill_files)
    if isolation_relax is not None:
        _isolation_relax_var.set(isolation_relax)


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Immutable capture of every builtin-runtime ContextVar.

    Built once via :func:`snapshot` after startup's two :func:`configure`
    calls have both run; re-applied via :func:`bind` at the top of every
    turn so state survives the sibling ``asyncio.Task`` per-turn dispatch
    creates (see module docstring, "Task 27 FOLLOW-UP").
    """

    workspace: Path
    allowed_paths: list[Path] | None
    working_dir: Path | None
    loader: CapabilityLoader | None
    vault_resolver: Any
    identity: AgentIdentity | None
    protected_paths: frozenset[Path]
    audit_sink: Any
    egress_proxy: Any
    tier: str
    import_policy: ImportPolicy
    prompt_source: PromptSource | None
    protected_trees: frozenset[Path]
    skill_files: SkillFiles | None
    isolation_relax: str | None


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
        working_dir=_working_dir_var.get(),
        loader=_loader_var.get(),
        vault_resolver=_vault_resolver_var.get(),
        identity=_identity_var.get(),
        protected_paths=_protected_paths_var.get(),
        audit_sink=_audit_sink_var.get(),
        egress_proxy=_egress_proxy_var.get(),
        tier=_tier_var.get(),
        import_policy=_import_policy_var.get(),
        prompt_source=_prompt_source_var.get(),
        protected_trees=_protected_trees_var.get(),
        skill_files=_skill_files_var.get(),
        isolation_relax=_isolation_relax_var.get(),
    )


def bind(snap: RuntimeSnapshot) -> None:
    """Idempotently bind a previously-built snapshot into the CURRENT task.

    Cheap — one ``.set()`` per ContextVar, no construction, no I/O. Called at the
    top of every turn-dispatch entry point so a turn running in a fresh
    sibling ``asyncio.Task`` (not a descendant of the task that ran
    :func:`configure`) still sees this agent's state.
    """
    _workspace_var.set(snap.workspace)
    _allowed_paths_var.set(snap.allowed_paths)
    _working_dir_var.set(snap.working_dir)
    _loader_var.set(snap.loader)
    _vault_resolver_var.set(snap.vault_resolver)
    _identity_var.set(snap.identity)
    _protected_paths_var.set(snap.protected_paths)
    _audit_sink_var.set(snap.audit_sink)
    _egress_proxy_var.set(snap.egress_proxy)
    _tier_var.set(snap.tier)
    _import_policy_var.set(snap.import_policy)
    _prompt_source_var.set(snap.prompt_source)
    _protected_trees_var.set(snap.protected_trees)
    _skill_files_var.set(snap.skill_files)
    _isolation_relax_var.set(snap.isolation_relax)


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


def warn_if_signature_invalidated(artifact: Path, *, tool_name: str) -> str:
    """Report a generic write over a signed artifact; never re-sign it (J4 B5).

    The agent key must never sign what a generic tool wrote: an agent that could
    rewrite a signed file and re-sign it with its own key launders any edit past
    the Sign pillar. A ``.arcsig`` beside the target means its signature is now
    stale, so the result says the file is UNSIGNED and the event is audited.
    The dedicated self-authoring tools (create/update skill/tool) are the only
    agent-key signers. Returns ``""`` when the file was never signed.
    """
    from arcagent.capabilities.artifact_signing import sidecar_path

    if not sidecar_path(artifact).exists():
        return ""
    return audit_unsigned_artifact(artifact, tool_name=tool_name)


def audit_unsigned_artifact(artifact: Path, *, tool_name: str) -> str:
    """Audit an unsigned (or now-unsigned) artifact; return a warning suffix.

    Doctrine (task #28, "fail honest"): every caller whose
    :func:`sign_artifact_file` call returned False (or whose generic write left
    a stale signature) MUST append the returned string to its success message —
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


def working_dir() -> Path:
    """Return where the LLM's file/exec tools operate (bash cwd + relative-path root).

    Falls back to the workspace when no launch dir was configured — so by default a tool
    behaves exactly as before (workspace-rooted). Only agents that opt in
    (``tools.operate_in_launch_dir``) get a project cwd here, and only for a path already
    inside ``workspace + allowed_paths`` (enforced in agent_lifecycle), so this never
    widens what the sandbox can reach.
    """
    wd = _working_dir_var.get()
    return wd if wd is not None else workspace()


def allowed_paths() -> list[Path] | None:
    """Return the list of additional readable paths (e.g. memory dirs)."""
    return _allowed_paths_var.get()


def authorized_roots() -> tuple[Path, ...]:
    """Canonical roots used for descriptor-relative built-in file I/O."""
    extras = _allowed_paths_var.get() or []
    return (workspace().resolve(), *(path.resolve() for path in extras))


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


def prompt_source() -> PromptSource:
    """Return the agent's prompt lookup (override first); stock when unconfigured."""
    return _prompt_source_var.get() or StockPromptSource()


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


def readonly_subpaths() -> list[Path]:
    """Protected files and skill trees inside the workspace, workspace-RELATIVE.

    arcrun's backend mounts each as ``{workspace}/{sub}:/workspace/{sub}:ro``, so
    ``sub`` must be relative to the workspace root (REQ-023 read-only mounts).
    The workspace skill tree is mounted read-only too (J4 B5), so a sandboxed
    shell cannot rewrite a signed skill even through shell indirection.
    """
    ws = workspace()
    subs: list[Path] = []
    for path in [*_protected_paths_var.get(), *protected_trees()]:
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

    import arcrun

    from arcagent.core.errors import ToolError

    identity = _identity_var.get()
    caller = identity.did if identity is not None else "did:arc:unknown"
    audit_sink = _audit_sink_var.get()
    audit = _ArcRunAuditAdapter(audit_sink) if audit_sink is not None else None
    tier_value = _tier_var.get()
    try:
        raw = await arcrun.run_shell(
            command,
            tier=tier_value,
            workspace=workspace(),
            readonly_subpaths=readonly_subpaths(),
            caller_did=caller,
            audit_sink=audit,
            timeout=float(timeout),
        )
    except arcrun.ExecutionIsolationError as exc:
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
        base_dir=working_dir(),
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
    check_outside_skill_trees(resolved, file_path, tool_name=tool_name)


def protected_trees() -> frozenset[Path]:
    """Signed capability folders generic tools may not touch (J4 B5).

    Always includes ``<workspace>/capabilities/skills``; the agent startup adds
    the operator capability roots via :func:`configure`.
    """
    from arcagent.tools._validation import WORKSPACE_SKILL_TREE

    return _protected_trees_var.get() | {(workspace() / WORKSPACE_SKILL_TREE).resolve()}


def check_outside_skill_trees(resolved: Path, file_path: str, *, tool_name: str) -> None:
    """Deny + audit a generic tool's read or write inside a signed skill folder.

    ``read`` uses this too: the bytes there are verified only through
    ``read_skill_file``, so a plain read would hand the model unverified text.
    """
    from arcagent.tools._validation import enforce_outside_protected_trees

    enforce_outside_protected_trees(
        resolved,
        protected_trees(),
        tool_name=tool_name,
        file_path=file_path,
        caller_did=caller_did(),
        audit_sink=_audit_sink_var.get(),
    )


def check_shell_skill_trees(command: str, *, tool_name: str = "bash") -> None:
    """Refuse a shell command that names a path inside a signed skill folder.

    Every tier: a script run through ``bash`` skips the run-time signature check
    ``run_skill_script`` enforces, and a write breaks the operator's approval.
    """
    from arcagent.tools._validation import scan_shell_for_protected_trees

    hit = scan_shell_for_protected_trees(command, working_dir(), protected_trees())
    if hit is not None:
        check_outside_skill_trees(hit, str(hit), tool_name=tool_name)


def skill_files() -> SkillFiles:
    """The agent's verified skill-file reader; raises if the agent did not wire it."""
    current = _skill_files_var.get()
    if current is None:
        raise RuntimeError("skill file tools called before the skill file reader is configured")
    return current


def isolation_relax() -> str | None:
    """Personal-tier ArcRun isolation relaxation for run_skill_script (None: tier floor)."""
    return _isolation_relax_var.get()


def arcrun_audit_sink() -> Any:
    """The agent audit sink adapted to arcrun's ``write(AuditEvent)`` shape, or None."""
    sink = _audit_sink_var.get()
    return _ArcRunAuditAdapter(sink) if sink is not None else None


def audit(event: str, payload: dict[str, Any]) -> None:
    """Emit one agent audit event (fail-open: audit never masks the tool result)."""
    sink = _audit_sink_var.get()
    if sink is None:
        return
    try:
        sink(event, payload)
    except Exception:  # reason: fail-open — audit must not mask the tool outcome
        _logger.exception("Audit sink raised for %s; continuing", event)


def caller_did() -> str:
    """The agent DID every tool call is made under (``did:arc:unknown`` unbound)."""
    identity = _identity_var.get()
    return identity.did if identity is not None else "did:arc:unknown"


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
    """Advisory host-bash guard: protected paths (REQ-001) + module root (REQ-335).

    Best-effort only (OQ-2) — a host shell can evade naive parsing. Real
    enforcement at enterprise/federal is the sandbox read-only mount (REQ-023),
    which bind-mounts the workspace and never the module root.
    """
    from arcagent.tools._validation import (
        enforce_outside_module_root,
        scan_shell_for_module_root,
        scan_shell_for_protected_writes,
    )

    ws = workspace()
    hit = scan_shell_for_protected_writes(command, ws, _protected_paths_var.get())
    if hit is not None:
        check_protected(hit, str(hit), tool_name=tool_name)
    module_hit = scan_shell_for_module_root(command, ws)
    if module_hit is not None:
        identity = _identity_var.get()
        enforce_outside_module_root(
            module_hit,
            tool_name=tool_name,
            file_path=str(module_hit),
            caller_did=identity.did if identity is not None else "did:arc:unknown",
            audit_sink=_audit_sink_var.get(),
        )


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
    _prompt_source_var.set(None)
    _protected_trees_var.set(frozenset())
    _skill_files_var.set(None)
    _isolation_relax_var.set(None)


__all__ = [
    "RuntimeSnapshot",
    "allowed_paths",
    "arcrun_audit_sink",
    "audit",
    "audit_unsigned_artifact",
    "authorized_roots",
    "bind",
    "caller_did",
    "check_outside_skill_trees",
    "check_protected",
    "check_secret_content",
    "check_shell_command",
    "check_shell_skill_trees",
    "configure",
    "egress",
    "get_secret",
    "import_policy",
    "isolation_relax",
    "loader",
    "prompt_source",
    "protected_paths",
    "protected_trees",
    "readonly_subpaths",
    "reset",
    "resolve_workspace_path",
    "run_sandboxed_bash",
    "sign_artifact_file",
    "skill_files",
    "snapshot",
    "tier",
    "warn_if_signature_invalidated",
    "workspace",
]
