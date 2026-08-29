"""`PUT`/`DELETE` `/api/agents/{id}/files/read` — operator-gated workspace file save + delete.

COMP-012 / REQ-099 (write) and H-018 (delete). Extends the read surface in
``config.py`` (``get_file_read``) with write and delete verbs on the same
file resource so an operator can edit or remove agent-defining documents
(identity.md and other workspace files) from the UI without SSH. Two guards
run before any byte is written or removed, mirroring what the agent's own
file tools enforce:

1. Canonical-path confinement — the resolved (symlink/.. collapsed) target
   must stay under the selected agent root (``workspace`` or the whole agent
   dir). Source of truth for the read side is
   ``arcgateway.fs_reader._validate_path``; the same check is replicated here
   because that module ships no write path.
2. Secret-content guard (write only) — the payload is scanned with arcllm's
   structured ``SECRET_PATTERNS`` plus the one keyword heuristic from
   ``arcagent.tools._secret_guard.find_secret`` (the source of truth; arcui
   cannot import that private module and stay in-package, so the CALL is
   replicated with the heuristic kept in sync). A match refuses the save —
   credentials never touch the filesystem (arcagent doctrine).

Delete adds a third guard the write route does not need: ADR-029 agent-state
protection (see ``_protection_level``). ``identity.md``, ``policy.md``, the
per-agent config TOMLs, any signed prompt overlay under ``context/**`` (plus
its ``.arcsig`` sidecar), any private key file (``*.key``, wherever it
resolves), and the audit chain (``workspace/audit/**``, the sibling
``.audit/**``) can never be deleted from the dashboard. ``memory/``,
``sessions/``, and ``context.md`` are the agent's own memory/operating
state — deletable only with an explicit ``confirm_protected=true`` query
param, which the UI gates behind a second, stronger confirmation dialog.

The delete path also closes a symlink/TOCTOU gap: ``_confine`` resolves the
target's realpath exactly ONCE, and every check plus the eventual ``unlink``
act on that same resolved :class:`Path` object — a workspace entry that is
actually a symlink to a protected or out-of-bounds file is judged (and, if
refused, audited) by what it truly points at, never by the request string a
caller can spell however it likes.

Every write/delete (applied, denied, or errored) is recorded through the
COMP-010 ``emit_mutation_audit`` helper, and once the target has been
resolved the audit ``target`` field carries the RESOLVED path — not the raw
request string — so a symlink attempt shows in the audit trail as what it
actually pointed at. The UI never signs: if a saved file has an ``.arcsig``
sidecar, the write response flags the signature as stale so the agent knows
it must re-sign — arcui holds no agent identity.
"""

from __future__ import annotations

import re
from pathlib import Path

import arctrust
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.query_validators import safe_choice
from arcui.routes.agent_detail._common import (
    _VALID_ROOTS,
    _agent_root,
    _is_key_material,
    _resolve_root_path,
)
from arcui.schemas import ErrorResponse, FileDeleteResponse, FileWriteResponse

# Detached-signature sidecar convention (arcagent.capabilities.artifact_signing
# SIDECAR_SUFFIX): a signed artifact ``X`` has an ``X.arcsig`` beside it.
_SIDECAR_SUFFIX = ".arcsig"

# ADR-029 agent-state protection (H-018). Paths below are relative to the
# AGENT root (``team/<agent>/``), not the ``workspace`` alias, because two of
# the protected trees — the sibling ``.audit/`` directory
# (``arcagent.core.agent_security.trace_checkpoint_chain_path`` /
# ``prior_audit_chains_exist``) and the signed-prompt-overlay ``context/``
# directory (``arcagent.core.prompt_context._resolver_for_root`` —
# ``<agent_root>/context``, deliberately OUTSIDE the workspace subtree the
# agent's own file tools are confined to) — live OUTSIDE ``workspace/``
# entirely, next to it. Computing one relative path against the agent root
# and checking it against every tree means the guard fires identically
# whether the operator addressed the file via ``root=workspace`` or
# ``root=agent``.
#
# BLOCKED — never deletable from the dashboard, no confirmation overrides it:
#   - workspace/identity.md  — the agent's immutable-goals document (ASI01).
#   - workspace/policy.md    — the policy-pipeline bullets document. A
#                              PolicyPipeline with no configured file
#                              default-ALLOWs (fail-closed-only-when-set —
#                              see arctrust's configured-gate rule), so
#                              deleting this is not a "reset to default," it
#                              is an authorization DOWNGRADE from whatever
#                              restrictive policy was in force to allow-all.
#                              That is the same blast radius as identity.md,
#                              not "the agent's own operating state" — an
#                              operator who wants to change policy uses the
#                              policy EDIT path (audited as "policy
#                              changed"), never a file delete.
#   - arcagent.toml / arcllm.toml / arcrun.toml — the agent's own config,
#                              sitting directly in the agent root (reachable
#                              via ``root=agent``, same directory
#                              ``arcagent.core.config_loading`` loads from).
#                              Deleting one does not "reset to default"; it
#                              breaks the agent's next start (or, worse,
#                              silently changes DID/model/loop behavior via
#                              whatever a sibling_chain fallback resolves
#                              to instead).
#   - context/**              — signed prompt overlays (arcprompt COMP-007,
#                              ``<agent_root>/context/<package>/<name>.md``)
#                              plus their ``.arcsig`` sidecars. Deleting an
#                              overlay silently changes (or, sidecar-only,
#                              breaks) the effective signed system prompt —
#                              a control-plane artifact, not ordinary
#                              content (build-principles.md).
#   - *.key (anywhere)         — private Ed25519 key material. Checked by
#                              suffix via ``_is_key_material`` so the block
#                              holds regardless of location (see that
#                              function's docstring). Keys are
#                              non-exportable by construction (LLM07).
#   - *.arcsig (anywhere)      — detached signature sidecars for any signed
#                              artifact, not just prompt overlays.
#   - workspace/audit/**       — the in-workspace policy audit chain
#                              (``agent_security.policy_audit_log_path``'s
#                              workspace-relative fallback).
#   - .audit/**                — the sibling trace-checkpoint/skills WORM
#                              chain and dynamic-approval directory.
_BLOCKED_EXACT: frozenset[str] = frozenset(
    {
        "workspace/identity.md",
        "workspace/policy.md",
        "arcagent.toml",
        "arcllm.toml",
        "arcrun.toml",
        "workspace/audit",
        ".audit",
    }
)
_BLOCKED_PREFIXES: tuple[str, ...] = ("workspace/audit/", ".audit/", "context/")

# CONFIRM — the agent's own memory/operating state. Deletable only when the
# caller passes ``confirm_protected=true`` (a distinct, stronger confirmation
# in the UI from the ordinary delete confirm):
#   - workspace/memory/**    — arcmemory's entire durable store (SPEC-041).
#   - workspace/sessions/**  — session transcripts (fs_reader-served today).
#   - workspace/context.md   — the workpad's self-managed run-stable cockpit.
_CONFIRM_EXACT: frozenset[str] = frozenset(
    {"workspace/memory", "workspace/sessions", "workspace/context.md"}
)
_CONFIRM_PREFIXES: tuple[str, ...] = ("workspace/memory/", "workspace/sessions/")

# Keyword-anchored generic-token heuristic, kept in sync with
# ``arcagent.tools._secret_guard._GENERIC_TOKEN_RE`` (the source of truth). It
# catches an unprefixed token pasted next to its own label (the live-incident
# shape arcllm's structured patterns deliberately cannot see — ADR-423).
_GENERIC_TOKEN_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"(?<![a-z])(?:api[_-]?key|api[_-]?token|access[_-]?token|secret|password|client[_-]?secret)"
    r"(?![a-z])\s*[:=]\s*['\"]?[A-Za-z0-9_\-.]{16,}['\"]?"
    r"|"
    r"\bbearer\s+[A-Za-z0-9_\-.]{16,}\b"
    r")"
)


def _find_secret(content: str) -> str | None:
    """Return a label for the first secret-shaped match in ``content``, else None."""
    for secret_type, pattern in arctrust.SECRET_PATTERNS:
        if pattern.search(content):
            return secret_type
    if _GENERIC_TOKEN_RE.search(content):
        return "GENERIC_API_TOKEN"
    return None


def _confine(base: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``base``; return the canonical path, or None on escape.

    Mirrors ``arcgateway.fs_reader._validate_path``: reject absolute paths,
    collapse symlinks/.. via ``resolve()``, then require the result to stay
    under ``base``. Kept identical to the read-side confinement so the editor
    can never write where the reader cannot look.
    """
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return None
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _best_effort_resolved(base: Path, rel: str) -> str:
    """Best-effort resolved path, for AUDIT FORENSICS ONLY on a refused delete.

    Never consulted for the security decision — that is :func:`_confine`'s
    job alone. This exists so a denied audit event names what a ``..`` or
    symlink attempt actually resolved to (e.g. a path outside the agent
    root), not the untrusted request string a caller can spell any way it
    likes. Falls back to the raw ``rel`` on any resolution error so audit
    emission itself can never raise.
    """
    try:
        return (base / rel).resolve().as_posix()
    except OSError:
        return rel


def _protection_level(agent_root: Path, canonical: Path) -> str | None:
    """Return ``"blocked"``, ``"confirm"``, or ``None`` for a delete target (ADR-029).

    ``canonical`` must already be the fully resolved realpath (see
    :func:`_confine`) — this function performs no filesystem access of its
    own and must never re-resolve, so the path it judges is exactly the path
    the caller is about to unlink (the TOCTOU-closing invariant).

    Key material and signature sidecars are checked by name/suffix FIRST,
    independent of location, before the location-based (relative-to-agent-root)
    checks — see :data:`_BLOCKED_EXACT` for why. Computed from ``canonical``'s
    path relative to the AGENT root regardless of which ``root`` query param
    resolved it, so the guard fires identically whether the operator addressed
    the file via ``root=workspace`` or ``root=agent``.
    """
    if _is_key_material(canonical) or canonical.name.lower().endswith(_SIDECAR_SUFFIX):
        return "blocked"
    try:
        rel = canonical.relative_to(agent_root.resolve()).as_posix()
    except ValueError:
        return None
    if rel in _BLOCKED_EXACT or rel.startswith(_BLOCKED_PREFIXES):
        return "blocked"
    if rel in _CONFIRM_EXACT or rel.startswith(_CONFIRM_PREFIXES):
        return "confirm"
    return None


async def _content_from_body(request: Request) -> str | None:
    """Extract the string ``content`` field from the JSON body, or None."""
    try:
        body = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return None
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    return content if isinstance(content, str) else None


async def put_file_write(request: Request) -> JSONResponse:
    """PUT /api/agents/{id}/files/read — save a workspace file (operator only)."""
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    rel = request.query_params.get("path")
    if not rel:
        return _error("Missing path", 400)

    root_arg, err = safe_choice(
        request.query_params.get("root", "workspace"),
        _VALID_ROOTS,
        error_label="Invalid root",
    )
    if err is not None:
        return err

    target = f"{root_arg}:{rel}"

    # Operator gate first — a viewer never reaches the filesystem.
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request, target=target, operation="file_write", outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    base = _resolve_root_path(agent_root, root_arg)
    canonical = _confine(base, rel)
    if canonical is None:
        emit_mutation_audit(
            request,
            target=target,
            operation="file_write",
            outcome="denied",
            detail="path escapes agent directory",
        )
        return _error(f"path escapes agent directory: {rel}", 400)

    # A private key is non-exportable by principle and a detached ``.arcsig`` is
    # meaningless if hand-edited — neither is ever legitimately written through the
    # file editor (unlike identity.md / policy.md, which ARE the operator-edit path).
    # Checked on the RESOLVED path so a symlink alias cannot smuggle a write.
    if _is_key_material(canonical) or canonical.name.lower().endswith(_SIDECAR_SUFFIX):
        emit_mutation_audit(
            request,
            target=_best_effort_resolved(base, rel),
            operation="file_write",
            outcome="denied",
            detail="protected artifact: private key or signature sidecar is not hand-writable",
        )
        return _error("refusing to write a private key or signature sidecar", 403)

    content = await _content_from_body(request)
    if content is None:
        return _error("expected a JSON body with a string 'content' field", 400)

    secret_type = _find_secret(content)
    if secret_type is not None:
        emit_mutation_audit(
            request,
            target=target,
            operation="file_write",
            outcome="denied",
            detail=f"secret_content:{secret_type}",
        )
        return _error(
            f"Refusing to save '{rel}': content looks like a live credential "
            f"({secret_type}). Credentials never touch the filesystem.",
            400,
        )

    try:
        canonical.write_text(content, encoding="utf-8")
    except OSError as exc:
        emit_mutation_audit(
            request, target=target, operation="file_write", outcome="error", detail=str(exc)
        )
        return _error(f"could not write file: {exc}", 400)

    stat = canonical.stat()
    signature_stale = Path(f"{canonical}{_SIDECAR_SUFFIX}").exists()
    message = "Saved."
    if signature_stale:
        message = (
            "Saved. This file has an .arcsig signature which is now stale; the agent "
            "must re-sign it — the UI cannot sign (it holds no agent identity)."
        )

    emit_mutation_audit(
        request,
        target=target,
        operation="file_write",
        outcome="applied",
        detail=f"bytes={stat.st_size} arcsig_stale={signature_stale}",
    )
    return JSONResponse(
        FileWriteResponse(
            path=rel,
            size=stat.st_size,
            mtime=stat.st_mtime,
            signature_stale=signature_stale,
            message=message,
        ).model_dump(mode="json")
    )


async def delete_file(request: Request) -> JSONResponse:
    """DELETE /api/agents/{id}/files/read — remove one workspace/agent file (operator only).

    Guard order mirrors ``put_file_write``: operator gate, then path
    confinement. Delete adds one more gate the write route has no need for —
    :func:`_protection_level` (ADR-029) — before touching the filesystem.
    Directories are never removed here: the browser only ever selects a leaf
    file, so an existing directory at ``rel`` falls through to the same 404
    a missing file gets, rather than a recursive delete.

    Symlink / TOCTOU: :func:`_confine` resolves the realpath exactly ONCE,
    producing ``canonical``. Every check below (:func:`_protection_level`,
    ``is_file``) and the eventual ``unlink`` operate on that SAME
    :class:`Path` object — nothing here calls ``.resolve()`` a second time.
    A workspace entry that is actually a symlink is therefore judged, and
    deleted or refused, by what it truly points at, never by the request
    string. From the point ``canonical`` exists, every audit ``target`` is
    built from it (relative to the selected root) rather than from the raw
    ``rel`` query param, so a symlink attempt is recorded as what it
    actually resolved to.
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    rel = request.query_params.get("path")
    if not rel:
        return _error("Missing path", 400)

    root_arg, err = safe_choice(
        request.query_params.get("root", "workspace"),
        _VALID_ROOTS,
        error_label="Invalid root",
    )
    if err is not None:
        return err

    target = f"{root_arg}:{rel}"

    # Operator gate first — a viewer never reaches the filesystem, so there is
    # no resolved path yet to prefer over the raw request string.
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request, target=target, operation="file_delete", outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    base = _resolve_root_path(agent_root, root_arg)
    canonical = _confine(base, rel)
    if canonical is None:
        # Escape refused — no safe path was ever assigned to `canonical`, but
        # a best-effort resolve still tells the audit trail where a symlink
        # or `..` attempt actually pointed (e.g. outside the agent root
        # entirely), which the raw request string alone would hide.
        emit_mutation_audit(
            request,
            target=f"{root_arg}:{_best_effort_resolved(base, rel)}",
            operation="file_delete",
            outcome="denied",
            detail="path escapes agent directory",
        )
        return _error(f"path escapes agent directory: {rel}", 400)

    # From here on `canonical` is the single resolved realpath every
    # remaining check and the delete itself act on — recompute the audit
    # target from IT (not `rel`) so a symlink shows in the log as its real
    # destination.
    resolved_rel = canonical.relative_to(base.resolve()).as_posix()
    target = f"{root_arg}:{resolved_rel}"

    protection = _protection_level(agent_root, canonical)
    if protection == "blocked":
        emit_mutation_audit(
            request,
            target=target,
            operation="file_delete",
            outcome="denied",
            detail="protected_agent_state:blocked",
        )
        return _error(
            f"'{rel}' is protected agent state (identity, policy, agent config, a "
            "signed prompt overlay, key material, or the audit chain) and can never "
            "be deleted from the dashboard.",
            403,
        )

    if protection == "confirm" and request.query_params.get("confirm_protected") != "true":
        emit_mutation_audit(
            request,
            target=target,
            operation="file_delete",
            outcome="denied",
            detail="protected_agent_state:confirmation_required",
        )
        return _error(
            f"'{rel}' is part of the agent's own memory/session state. Pass "
            "confirm_protected=true to confirm you understand this deletes agent state, "
            "not ordinary workspace content.",
            409,
        )

    if not canonical.is_file():
        return _error(f"File not found: {rel}", 404)

    try:
        canonical.unlink()
    except OSError as exc:
        emit_mutation_audit(
            request, target=target, operation="file_delete", outcome="error", detail=str(exc)
        )
        return _error(f"could not delete file: {exc}", 400)

    Path(f"{canonical}{_SIDECAR_SUFFIX}").unlink(missing_ok=True)

    emit_mutation_audit(
        request,
        target=target,
        operation="file_delete",
        outcome="applied",
        detail=f"protected={protection or 'none'}",
    )
    return JSONResponse(
        FileDeleteResponse(path=rel, protected=protection, message="Deleted.").model_dump(
            mode="json"
        )
    )


__all__: list[str] = ["delete_file", "put_file_write"]
