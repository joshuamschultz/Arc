"""`/api/agents/{id}/prompts/*` — list / inspect / override / reset system prompts (COMP-010).

The read side lists every stock prompt across every installed Arc package
(``arcprompt.PromptCatalog``) and marks each ``stock`` or ``overridden`` by the
presence of an overlay under ``<agent_root>/context/<package>/<name>.md``. The
detail read returns the stock body, the effective body, and a server-computed
unified diff (stdlib ``difflib`` — no diff library ships to the browser).

The write side is operator-gated and mirrors ``files_write.py``: the same secret
scan refuses a pasted credential; the overlay path is confined under the agent's
``context/`` root; the overlay is authored via ``arcprompt.render_prompt`` and
signed through the :mod:`arcui.prompt_signing` seam so the audited signer DID is
the RESOLVED principal, never a constant. Every write routes through the optional
``prompt:write`` policy gate first (COMP-014): no configured pipeline → allow; a
configured DENY → refuse and audit. Reset removes the overlay so the prompt
resolves to stock on the agent's next run.

Overlay root note: ``<agent_root>/context`` is the ``agent`` root (not the
workspace subtree agent file tools are confined to), so this surface is the only
one that can author an override — by design (COMP-007).
"""

from __future__ import annotations

import difflib
from pathlib import Path

import yaml
from arcagent.core.prompt_context import read_agent_tier
from arcprompt import (
    PromptCatalog,
    PromptMissing,
    load_stock_document,
    parse_prompt,
    render_prompt,
)
from arctrust.policy import Decision, PolicyContext, ToolCall
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui import prompt_signing
from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did, _agent_root, logger
from arcui.routes.agent_detail.files_write import _SIDECAR_SUFFIX, _confine, _find_secret
from arcui.schemas import (
    ErrorResponse,
    PromptDetailResponse,
    PromptListItem,
    PromptListResponse,
    PromptResetResponse,
    PromptWriteResponse,
    RubricDimension,
    RubricResponse,
    RubricUpdate,
)

_OVERLAY_DIRNAME = "context"

# The one structured-form prompt: its body is a YAML rubric, not prose. Keyed by
# (package, name) so any agent's copy is editable, but never a single agent.
_RUBRIC_PACKAGE = "arcskill"
_RUBRIC_NAME = "judge_rubric"


def _is_rubric(package: str, name: str) -> bool:
    return package == _RUBRIC_PACKAGE and name == _RUBRIC_NAME


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _store_unreadable(exc: Exception) -> JSONResponse:
    """Surface a catalog/overlay read failure verbatim, distinct from empty (200)."""
    logger.warning("prompts route: catalog unreadable: %s", exc)
    return _error(str(exc), 503)


def _overlay_path(agent_root: Path, package: str, name: str) -> Path:
    return agent_root / _OVERLAY_DIRNAME / package / f"{name}.md"


def _overlay_body_or_stock(overlay: Path, stock_body: str) -> str:
    """Effective body: the overlay's when present and parseable, else stock."""
    if not overlay.is_file():
        return stock_body
    try:
        return parse_prompt(overlay.read_bytes(), source="overlay").body
    except Exception:  # reason: a broken overlay falls back to stock for DISPLAY only
        return stock_body


def _unified_diff(stock_body: str, effective_body: str) -> str:
    return "".join(
        difflib.unified_diff(
            stock_body.splitlines(keepends=True),
            effective_body.splitlines(keepends=True),
            fromfile="stock",
            tofile="effective",
        )
    )


async def _evaluate_policy(
    request: Request, *, agent_root: Path, agent_did: str, package: str, name: str
) -> Decision | None:
    """Route a ``prompt:write`` action through the optional injected policy pipeline.

    Returns the pipeline's :class:`Decision`, or ``None`` when no pipeline is
    configured (configured-gate convention: no rule → allow). arcui holds no
    tier-conditional logic — the pipeline it is handed decides.
    """
    pipeline = getattr(request.app.state, "prompt_policy", None)
    if pipeline is None:
        return None
    session_id = getattr(request.state, "session_id", None) or "unknown"
    call = ToolCall(
        tool_name="prompt:write",
        arguments={"package": package, "name": name},
        agent_did=agent_did,
        session_id=session_id,
        classification="unclassified",
    )
    ctx = PolicyContext(
        tier=read_agent_tier(agent_root),  # type: ignore[arg-type]  # reason: validated to _Tier literal
        policy_version="",
        bundle_age_seconds=0.0,
    )
    decision: Decision = await pipeline.evaluate(call, ctx)
    return decision


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def get_prompts(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/prompts — every stock prompt, marked stock/overridden."""
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    try:
        refs = PromptCatalog().catalog()
    except Exception as exc:  # reason: 503-unreadable vs 200-empty (skill_versions convention)
        return _store_unreadable(exc)
    items = [
        PromptListItem(
            package=ref.package,
            name=ref.name,
            description=ref.description,
            status="overridden" if _overlay_path(agent_root, ref.package, ref.name).is_file()
            else "stock",
        )
        for ref in refs
    ]
    return JSONResponse(PromptListResponse(items=items).model_dump(mode="json"))


async def get_prompt_detail(request: Request) -> JSONResponse:
    """GET .../prompts/{package}/{name} — stock + effective bodies + unified diff."""
    agent_id = request.path_params["id"]
    package = request.path_params["package"]
    name = request.path_params["name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    try:
        stock_doc = load_stock_document(package, name)
    except PromptMissing:
        return _error(f"no stock prompt {package}/{name}", 404)
    except Exception as exc:  # reason: 503-unreadable vs 200-empty
        return _store_unreadable(exc)

    overlay = _overlay_path(agent_root, package, name)
    stock_body = stock_doc.body
    effective_body = _overlay_body_or_stock(overlay, stock_body)
    payload = PromptDetailResponse(
        package=package,
        name=name,
        description=stock_doc.description,
        status="overridden" if overlay.is_file() else "stock",
        stock=stock_body,
        effective=effective_body,
        diff=_unified_diff(stock_body, effective_body),
    )
    return JSONResponse(payload.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Write / reset (operator-gated mutations)
# ---------------------------------------------------------------------------


async def put_prompt(request: Request) -> JSONResponse:
    """PUT .../prompts/{package}/{name} — author + sign a prompt override (operator only)."""
    agent_id = request.path_params["id"]
    package = request.path_params["package"]
    name = request.path_params["name"]
    target = f"prompt:{package}/{name}"

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="prompt.write",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    try:
        stock_doc = load_stock_document(package, name)
    except PromptMissing:
        return _error(f"no stock prompt {package}/{name}", 404)

    content = await _content_from_body(request)
    if content is None:
        return _error("expected a JSON body with a string 'content' field", 400)

    agent_did = _agent_did(request, agent_id) or "did:arc:unknown"
    return await _author_signed_overlay(
        request,
        agent_root=agent_root,
        agent_did=agent_did,
        package=package,
        name=name,
        description=stock_doc.description,
        content=content,
        target=target,
    )


async def _author_signed_overlay(
    request: Request,
    *,
    agent_root: Path,
    agent_did: str,
    package: str,
    name: str,
    description: str,
    content: str,
    target: str,
) -> JSONResponse:
    """Secret-scan -> policy-gate -> confine -> operator-key sign -> write -> audit.

    The single signed-overlay write path. Both the prose-prompt editor and the
    structured-rubric editor produce a body string and hand it here; the security
    envelope (secret scan, ``prompt:write`` policy, operator-key signing via the
    :mod:`arcui.prompt_signing` seam, and the audit of the RESOLVED signer DID)
    lives in one place so neither surface can drift from it. Callers own only how
    ``content`` is produced and the up-front operator-role gate.
    """
    secret_type = _find_secret(content)
    if secret_type is not None:
        emit_mutation_audit(
            request,
            target=target,
            operation="prompt.write",
            outcome="denied",
            detail=f"secret_content:{secret_type}",
        )
        return _error(
            f"Refusing to override '{package}/{name}': content looks like a live credential "
            f"({secret_type}). Credentials never touch the filesystem.",
            400,
        )

    decision = await _evaluate_policy(
        request, agent_root=agent_root, agent_did=agent_did, package=package, name=name
    )
    if decision is not None and decision.is_deny():
        detail = f"policy_denied:{decision.layer}:{decision.rule_id}"
        emit_mutation_audit(
            request, target=target, operation="prompt.write", outcome="denied", detail=detail
        )
        return _error(f"prompt override denied by policy: {decision.reason}", 403)

    overlay_root = agent_root / _OVERLAY_DIRNAME
    canonical = _confine(overlay_root, f"{package}/{name}.md")
    if canonical is None:
        emit_mutation_audit(
            request,
            target=target,
            operation="prompt.write",
            outcome="denied",
            detail="path escapes context root",
        )
        return _error(f"invalid prompt path: {package}/{name}", 400)

    try:
        identity = prompt_signing.signer_for(request)
    except prompt_signing.SigningUnavailableError as exc:
        emit_mutation_audit(
            request, target=target, operation="prompt.write", outcome="error", detail=str(exc)
        )
        return _error(f"cannot sign override: {exc}", 500)

    overlay_bytes = render_prompt(content, name=name, description=description)
    signature = prompt_signing.sign(overlay_bytes, identity)
    try:
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(overlay_bytes)
        Path(f"{canonical}{_SIDECAR_SUFFIX}").write_text(signature.to_json(), encoding="utf-8")
    except OSError as exc:
        emit_mutation_audit(
            request, target=target, operation="prompt.write", outcome="error", detail=str(exc)
        )
        return _error(f"could not write override: {exc}", 400)

    emit_mutation_audit(
        request,
        target=target,
        operation="prompt.write",
        outcome="applied",
        detail=f"signer={identity.did} sha256={signature.artifact_sha256}",
    )
    return JSONResponse(
        PromptWriteResponse(
            package=package,
            name=name,
            signer_did=identity.did,
            sha256=signature.artifact_sha256,
            message="Override saved and signed. It takes effect on the agent's next run.",
        ).model_dump(mode="json")
    )


async def delete_prompt(request: Request) -> JSONResponse:
    """DELETE .../prompts/{package}/{name} — remove the override, resolve to stock."""
    agent_id = request.path_params["id"]
    package = request.path_params["package"]
    name = request.path_params["name"]
    target = f"prompt:{package}/{name}"

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="prompt.reset",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    overlay_root = agent_root / _OVERLAY_DIRNAME
    canonical = _confine(overlay_root, f"{package}/{name}.md")
    if canonical is None:
        return _error(f"invalid prompt path: {package}/{name}", 400)
    if not canonical.is_file():
        return _error(f"no override to reset for {package}/{name}", 404)

    try:
        canonical.unlink()
        Path(f"{canonical}{_SIDECAR_SUFFIX}").unlink(missing_ok=True)
    except OSError as exc:
        emit_mutation_audit(
            request, target=target, operation="prompt.reset", outcome="error", detail=str(exc)
        )
        return _error(f"could not remove override: {exc}", 400)

    emit_mutation_audit(request, target=target, operation="prompt.reset", outcome="applied")
    return JSONResponse(
        PromptResetResponse(
            package=package,
            name=name,
            message="Override removed. The prompt resolves to stock on the agent's next run.",
        ).model_dump(mode="json")
    )


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


# ---------------------------------------------------------------------------
# Structured rubric editor (arcskill/judge_rubric)
# ---------------------------------------------------------------------------


class _RubricError(ValueError):
    """The rubric YAML body is not a ``dimension -> {checklist, anti_inflation}`` mapping."""


def _parse_rubric(body: str) -> dict[str, RubricDimension]:
    """Parse a rubric YAML body into ordered, validated dimensions.

    Raises :class:`_RubricError` when the body is not a mapping of dimension
    name -> ``{checklist: [str], anti_inflation: str}``.
    """
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise _RubricError(f"rubric body is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise _RubricError("rubric body must be a YAML mapping of dimensions")
    try:
        return RubricUpdate.model_validate({"dimensions": loaded}).dimensions
    except ValidationError as exc:
        raise _RubricError(str(exc)) from exc


def _dump_rubric(dimensions: dict[str, RubricDimension]) -> str:
    """Serialize dimensions back to YAML, preserving dimension and key order."""
    obj = {
        dim: {"checklist": d.checklist, "anti_inflation": d.anti_inflation}
        for dim, d in dimensions.items()
    }
    return yaml.safe_dump(obj, sort_keys=False)


async def get_rubric(request: Request) -> JSONResponse:
    """GET .../prompts/{package}/{name}/rubric — the effective rubric as structured JSON."""
    agent_id = request.path_params["id"]
    package = request.path_params["package"]
    name = request.path_params["name"]
    if not _is_rubric(package, name):
        return _error(f"{package}/{name} is not a structured rubric", 404)

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    try:
        stock_doc = load_stock_document(package, name)
    except PromptMissing:
        return _error(f"no stock prompt {package}/{name}", 404)
    except Exception as exc:  # reason: 503-unreadable vs 200-empty (list convention)
        return _store_unreadable(exc)

    overlay = _overlay_path(agent_root, package, name)
    body = _overlay_body_or_stock(overlay, stock_doc.body)
    try:
        dimensions = _parse_rubric(body)
    except _RubricError as exc:  # reason: an unparseable rubric is unreadable, not empty
        return _store_unreadable(exc)

    payload = RubricResponse(
        package=package,
        name=name,
        status="overridden" if overlay.is_file() else "stock",
        dimensions=dimensions,
    )
    return JSONResponse(payload.model_dump(mode="json"))


async def put_rubric(request: Request) -> JSONResponse:
    """PUT .../prompts/{package}/{name}/rubric — validate structured JSON, sign the overlay."""
    agent_id = request.path_params["id"]
    package = request.path_params["package"]
    name = request.path_params["name"]
    target = f"prompt:{package}/{name}"
    if not _is_rubric(package, name):
        return _error(f"{package}/{name} is not a structured rubric", 404)

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="prompt.write",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    try:
        stock_doc = load_stock_document(package, name)
    except PromptMissing:
        return _error(f"no stock prompt {package}/{name}", 404)

    try:
        raw = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return _error("expected a JSON body with a 'dimensions' mapping", 400)
    try:
        update = RubricUpdate.model_validate(raw)
    except ValidationError as exc:
        return _error(f"invalid rubric: {exc}", 400)

    body = _dump_rubric(update.dimensions)
    agent_did = _agent_did(request, agent_id) or "did:arc:unknown"
    return await _author_signed_overlay(
        request,
        agent_root=agent_root,
        agent_did=agent_did,
        package=package,
        name=name,
        description=stock_doc.description,
        content=body,
        target=target,
    )


__all__ = [
    "delete_prompt",
    "get_prompt_detail",
    "get_prompts",
    "get_rubric",
    "put_prompt",
    "put_rubric",
]
