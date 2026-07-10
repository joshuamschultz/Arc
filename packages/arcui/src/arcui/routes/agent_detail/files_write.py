"""`PUT /api/agents/{id}/files/read` — operator-gated workspace file save.

COMP-012 / REQ-099. Extends the read surface in ``config.py``
(``get_file_read``) with a write verb on the same file resource so an
operator can edit agent-defining documents (identity.md and other workspace
files) from the UI without SSH. Two guards run before any byte is written,
mirroring what the agent's own file tools enforce:

1. Canonical-path confinement — the resolved (symlink/.. collapsed) target
   must stay under the selected agent root (``workspace`` or the whole agent
   dir). Source of truth for the read side is
   ``arcgateway.fs_reader._validate_path``; the same check is replicated here
   because that module ships no write path.
2. Secret-content guard — the payload is scanned with arcllm's structured
   ``SECRET_PATTERNS`` plus the one keyword heuristic from
   ``arcagent.tools._secret_guard.find_secret`` (the source of truth; arcui
   cannot import that private module and stay in-package, so the CALL is
   replicated with the heuristic kept in sync). A match refuses the save —
   credentials never touch the filesystem (arcagent doctrine).

Every save (applied, denied, or errored) is recorded through the COMP-010
``emit_mutation_audit`` helper. The UI never signs: if the saved file has an
``.arcsig`` sidecar, the response flags the signature as stale so the agent
knows it must re-sign — arcui holds no agent identity.
"""

from __future__ import annotations

import re
from pathlib import Path

from arcllm._secrets import SECRET_PATTERNS
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.query_validators import safe_choice
from arcui.routes.agent_detail._common import _VALID_ROOTS, _agent_root, _resolve_root_path
from arcui.schemas import ErrorResponse, FileWriteResponse

# Detached-signature sidecar convention (arcagent.capabilities.artifact_signing
# SIDECAR_SUFFIX): a signed artifact ``X`` has an ``X.arcsig`` beside it.
_SIDECAR_SUFFIX = ".arcsig"

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
    for secret_type, pattern in SECRET_PATTERNS:
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


__all__: list[str] = ["put_file_write"]
