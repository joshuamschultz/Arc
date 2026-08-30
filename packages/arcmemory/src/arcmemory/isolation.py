"""Cross-agent isolation guard for the ``build_brain`` factory (ASI03 / LLM02).

HONEST THREAT MODEL. In-process Python has no hard security boundary: any code
already running inside this interpreter can reach into another object's state.
This guard is therefore NOT a wall against a deliberate in-process attacker. Its
real job is to kill whole classes of ACCIDENTAL cross-agent wiring bugs — the same
class as the confirmed cross-agent memory bleed — before ``build_brain`` hands back
an :class:`~arcmemory.brain.ArcMemoryBrain` bound to the wrong {workspace, agent_did}.
Three cheap checks, each catching a different accidental bug:

1. **claimed == proven** — the context ``agent_did`` must equal the provided
   identity's ``did``. Catches "right identity object, wrong ``agent_did`` string."
2. **proven is self-consistent** — ``identity.did`` must hash-match
   ``identity.public_key`` (:func:`arctrust.identity.did_matches_pubkey`). Catches a
   hand-built or copied identity whose DID and key disagree, with no in-process
   signature challenge (overkill for an accidental-bug guard).
3. **proven owns the workspace** — a workspace is bound to one owner public key on
   its first build (an ``owner.pub`` marker under ``memory/``). A later build for a
   different key is refused. Catches "right identity, wrong workspace path" — the
   half of the bleed surface that check 1 alone misses.

Bootstrap edge (where such guards usually break). arcagent establishes the agent's
in-memory identity (``core/agent.py`` step 3) long before the first ``build_brain``
(``core/agent.py`` ``setup_capabilities`` → ``agent_lifecycle.configure_module_runtimes``
→ ``runtime_mod.configure``), so checks 1 and 2 never fire on the legit path. But
arcagent does NOT pre-write any owner marker into the workspace, so the marker is
adopted here on the first build:

* marker PRESENT → its key must exactly match the building identity's key, always;
* marker ABSENT + no memory data → fresh workspace: bind it to this identity;
* marker ABSENT + memory data present → OWN-DATA ownership. The workspace's recorded
  owners (the ``scope`` column of ``memory/index.db``) are read. Fail closed for a
  VICTIM workspace: it holds scoped data but NONE of it is this agent's (rebinding it
  would adopt another agent's private recall), and likewise for data with NO
  attributable scope rows at all. But a workspace this agent DOES own — at least one
  scope row is its own — is adopted in place even when PRE-EXISTING foreign scope rows
  are also present (the known cross-agent-bleed bug that predates the marker). Those
  foreign rows are never rebound and the read-time no-read-up scope gate already keeps
  them out of this agent's recall; bricking a contaminated own-workspace at build time
  is the wrong layer. That is how the deployed fleet — which predates the marker and
  carries historical bleed — upgrades without a stumble, while a victim workspace is
  still never rebound to this identity. Foreign rows are logged LOUD for cleanup.

On any failure this raises :class:`MemoryIsolationError` and emits a
``memory.isolation_fault`` audit event — the SAME failure vocabulary the
runtime-resolution guard in ``arcagent.modules.memory._runtime`` uses. arcmemory sits
BELOW arcagent in the import DAG and must never import it (``tests/architecture/
test_no_arcagent_import.py``), so the error type is mirrored here rather than imported;
both are ``RuntimeError`` subclasses with the same name and semantics, and both emit the
identical audit action, so callers and compliance sinks see one vocabulary, not two.
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
from pathlib import Path
from typing import Any, NoReturn

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from arctrust.identity import did_matches_pubkey

_logger = logging.getLogger("arcmemory.isolation")

# Owner-binding marker: the workspace's owning Ed25519 public key, written under the
# agent's own ``memory/`` home on first build (agent state → direct workspace I/O).
_OWNER_MARKER = "owner.pub"

# Tables in ``memory/index.db`` that carry a per-agent ``scope`` — the on-disk record
# of which agent a workspace's memory data belongs to. Constant allow-list; never
# interpolated from caller input.
_SCOPED_TABLES = ("episodic", "chunks", "edges", "insight_trigger")


class MemoryIsolationError(RuntimeError):
    """A brain build could not be tied to the workspace's owning identity.

    Raised fail-closed rather than return a Brain bound to the wrong
    {workspace, agent_did, identity} (ASI03 / LLM02).

    MIRROR PAIR — keep in lockstep with
    ``arcagent.modules.memory._runtime.MemoryIsolationError`` and its
    ``memory.isolation_fault`` audit event. The two are deliberately the same name,
    same ``RuntimeError`` base, and same audit action, but are SEPARATE types because
    arcmemory sits below arcagent in the import DAG and must never import it
    (``tests/architecture/test_no_arcagent_import.py``). This is the build-time guard;
    ``_runtime`` is the runtime-resolution guard. Evolve them together — a change to
    the error shape or the audit vocabulary here belongs there too.
    """


def enforce_brain_isolation(
    *,
    workspace: Path,
    agent_did: str,
    identity: Any,
    audit_sink: AuditSink | None,
    tier: str,
) -> None:
    """Refuse to build a Brain for a mismatched {workspace, agent_did, identity}.

    Runs the three checks described in the module docstring. Returns ``None`` when
    the build is safe; otherwise raises :class:`MemoryIsolationError` (after logging
    the fault and emitting a ``memory.isolation_fault`` audit event).
    """
    # Check 1 — memory requires identity, and the claimed DID must be the proven one.
    if identity is None:
        _fail_closed(
            workspace, agent_did, "", audit_sink, tier,
            "memory requires identity but none was provided",
        )
    claimed_did = str(getattr(identity, "did", "") or "")
    if claimed_did != agent_did:
        _fail_closed(
            workspace, agent_did, claimed_did, audit_sink, tier,
            "context agent_did does not match the provided identity DID",
        )
    # Check 2 — the proven identity must be self-consistent (DID ← pubkey hash).
    public_key = bytes(getattr(identity, "public_key", b"") or b"")
    if not did_matches_pubkey(agent_did, public_key):
        _fail_closed(
            workspace, agent_did, claimed_did, audit_sink, tier,
            "identity DID does not match its own public key",
        )
    # Check 3 — the proven identity must own the workspace.
    _enforce_workspace_ownership(workspace, agent_did, public_key, audit_sink, tier)


def _enforce_workspace_ownership(
    workspace: Path,
    agent_did: str,
    public_key: bytes,
    audit_sink: AuditSink | None,
    tier: str,
) -> None:
    memory_dir = Path(workspace) / "memory"
    marker = memory_dir / _OWNER_MARKER
    if marker.exists():
        try:
            owner_key = marker.read_bytes()
        except OSError as exc:
            _fail_closed(
                workspace, agent_did, "", audit_sink, tier,
                f"workspace owner marker is unreadable: {type(exc).__name__}",
            )
        # Constant-time compare — the key is not secret, but it gates isolation.
        if not hmac.compare_digest(owner_key, public_key):
            _fail_closed(
                workspace, agent_did, "", audit_sink, tier,
                "workspace is owned by a different identity",
            )
        return
    # No marker yet. A truly empty workspace is bound to this identity on this first
    # build. A workspace that ALREADY holds memory data is adopted ONLY under
    # ZERO-TOLERANCE ownership: every recorded scope row must be this agent's, and at
    # least one attributable row must exist. A single foreign scope row anywhere, or
    # data with no attributable rows at all, is the victim / unattributable shape and
    # fails closed — another agent's private recall must never be rebound to this
    # identity, and ownership that cannot be established is not ownership.
    if not _has_memory_data(memory_dir):
        _write_owner_marker(marker, public_key, workspace, agent_did, audit_sink, tier)
        return
    scopes = _recorded_scopes(memory_dir)
    if not scopes:
        _fail_closed(
            workspace, agent_did, "", audit_sink, tier,
            "workspace holds memory data with no attributable owner",
        )
    own = {s for s in scopes if s == agent_did or s.startswith(f"{agent_did}:")}
    if not own:
        # A victim workspace: it holds scoped memory data, but NONE of it is this
        # agent's — rebinding it would adopt another agent's private recall.
        _fail_closed(
            workspace, agent_did, sorted(scopes)[0], audit_sink, tier,
            "workspace memory data is owned only by a different agent DID",
        )
    # This agent owns its own data here. Adopt in place even when PRE-EXISTING foreign
    # scope rows are also present (the known cross-agent-bleed bug that predates this
    # guard): those rows are NOT rebound to this agent, and the read-time no-read-up
    # scope gate already keeps them out of this agent's recall. Bricking a contaminated
    # OWN workspace over data the read gate isolates is the wrong layer — build_brain's
    # job is to bind the brain to THIS agent's scope, which it can do safely. The bleed
    # is logged LOUD for cleanup, never silently rebound.
    foreign = {s for s in scopes if s not in own}
    if foreign:
        _logger.warning(
            "memory workspace %s holds %d pre-existing foreign scope value(s) "
            "(cross-agent bleed) — adopting for %s; foreign rows stay read-gated, not "
            "rebound (first: %s)",
            workspace,
            len(foreign),
            agent_did,
            sorted(foreign)[0],
        )
    _write_owner_marker(marker, public_key, workspace, agent_did, audit_sink, tier)


def _has_memory_data(memory_dir: Path) -> bool:
    """Whether the workspace holds any memory data (anything under ``memory/`` other
    than the owner marker itself and its write-replace temp)."""
    if not memory_dir.exists():
        return False
    ignore = {_OWNER_MARKER, f".{_OWNER_MARKER}.tmp"}
    return any(entry.name not in ignore for entry in memory_dir.iterdir())


def _recorded_scopes(memory_dir: Path) -> set[str]:
    """Every distinct ``scope`` recorded in the workspace's index (the on-disk record
    of which agent the memory data belongs to). Empty when there is no index, it is
    unopenable, or it holds no scoped rows."""
    db_path = memory_dir / "index.db"
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        # An unopenable/corrupt index yields no attributable owner — with data present
        # the caller then fails closed, which is the correct suspicious-shape handling.
        return set()
    try:
        return _scopes_from(conn)
    finally:
        conn.close()


def _scopes_from(conn: sqlite3.Connection) -> set[str]:
    scopes: set[str] = set()
    for table in _SCOPED_TABLES:
        try:
            rows = conn.execute(f"SELECT DISTINCT scope FROM {table}").fetchall()  # noqa: S608  # reason: table names are a constant allow-list, never caller input
        except sqlite3.Error:
            continue  # table absent in this schema version — skip
        scopes.update(scope for (scope,) in rows if isinstance(scope, str) and scope)
    return scopes


def _write_owner_marker(
    marker: Path,
    public_key: bytes,
    workspace: Path,
    agent_did: str,
    audit_sink: AuditSink | None,
    tier: str,
) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a crash never leaves a half-written owner key that a
        # later exact-match compare would (correctly) reject.
        tmp = marker.parent / f".{marker.name}.tmp"
        tmp.write_bytes(public_key)
        tmp.chmod(0o644)
        tmp.replace(marker)
    except OSError as exc:
        _fail_closed(
            workspace, agent_did, "", audit_sink, tier,
            f"could not bind workspace owner marker: {type(exc).__name__}",
        )


def _fail_closed(
    workspace: Path,
    current_did: str,
    resolved_did: str,
    audit_sink: AuditSink | None,
    tier: str,
    reason: str,
) -> NoReturn:
    """Log + audit the isolation fault, then raise. Never returns."""
    _logger.error(
        "memory build isolation fault: %s (workspace=%s current=%r resolved=%r)",
        reason,
        workspace,
        current_did,
        resolved_did,
    )
    _emit_isolation_fault(
        audit_sink,
        actor_did=current_did,
        target=str(workspace),
        tier=tier,
        detail={
            "reason": reason,
            "current_did": current_did,
            "resolved_did": resolved_did,
            "phase": "build_brain",
        },
    )
    raise MemoryIsolationError(reason)


def _emit_isolation_fault(
    audit_sink: AuditSink | None,
    *,
    actor_did: str,
    target: str,
    tier: str,
    detail: dict[str, Any],
) -> None:
    sink: AuditSink = audit_sink if audit_sink is not None else NullSink()
    emit(
        AuditEvent(
            actor_did=actor_did or "unknown",
            action="memory.isolation_fault",
            target=target,
            outcome="deny",
            tier=tier or None,
            extra=detail,
        ),
        sink,
    )


__all__ = ["MemoryIsolationError", "enforce_brain_isolation"]
