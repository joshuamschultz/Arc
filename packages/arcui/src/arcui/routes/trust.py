"""Operator-gated capability-trust surface — the arcui half of ``arc trust``.

``GET  /api/trust/gated``       — list gated (non-loaded) capabilities across the
                                  server's agents (any authed role).
``GET  /api/trust/source``      — one gated capability's artifact text (any authed
                                  role) so it can be read before it is trusted.
``POST /api/trust/approve``     — sign a gated capability (operator).
``POST /api/trust/disapprove``  — withdraw that signature (operator).

This module is a pure view: it DISCOVERS gated capabilities via
``arcagent.capabilities.inventory`` (arcagent owns loading) and MUTATES trust
through ``arcagent``'s signing seam (SPEC-066 COMP-010) — the same one
``arc trust approve`` drives, so a browser approval and a CLI approval are one
code path. Approval SIGNS: a source-hash pin alone leaves the capability behind
the loader's signature floor, which is the gap SPEC-066 exists to close. It
resolves agents from the roster, gates mutations on the operator role, records
the approver as the on-box operator DID, and audits every mutation — mirroring
``routes/approvals.py`` exactly. ``arcagent`` is reached through its root facade
only (SPEC-023 §2.2, enforced by ``test_arcui_imports_arcagent_only_via_
approved_seams`` and ``test_arcui_uses_only_public_arcagent_names``), so a view
module cannot quietly widen that surface. ``arctrust`` (a lower layer that
imports no siblings) is imported at module top.

REQ-321: approval is operator-authenticated HTTP only. It is not registered as a
tool on any registry and is not routed on ``/ws/chat`` — an agent must never be
able to authorize its own capability.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

import arcagent
from arctrust import OperatorKey, arc_home, default_operator_key_path
from arctrust import disapprove as _disapprove_pin
from arctrust.policy import OperatorApprovalAuthority
from arctrust.signer import VAULT_TRANSIT
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, operator_actor_did, operator_audit_sink
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.trust")


class _OperatorSeedUnavailableError(RuntimeError):
    """The deployment custodies the operator seed outside this process."""


#: What the operator is told when custody puts the seed out of reach. Names the
#: cause AND the two ways out, because the refusal is otherwise indistinguishable
#: from a broken deployment and the operator's next move is not obvious.
_VAULT_CUSTODY_REFUSAL = (
    f"operator_key_not_in_process: custody={VAULT_TRANSIT}. Signing a capability "
    "needs the operator seed in this process. Either approve from a deployment "
    'that holds the seed, or set [security] custody = "in_process" in the machine '
    "arcagent.toml under your arc home."
)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _resolve_agent(request: Request, agent_id: str) -> tuple[Path, str] | None:
    """Map ``agent_id`` to ``(agent_root, label)`` via the injected roster provider."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return Path(entry.workspace_path), entry.display_name
    return None


def _machine_custody() -> str:
    """The deployment's operator-key custody model, from the machine ``[security]``.

    Read from the same arc home the operator key itself resolves under, and
    validated by ``SecurityConfig`` so the tier crypto floor applies (federal and
    enterprise imply ``vault_transit``). An absent or unparseable config is the
    personal default, matching ``arccli.commands.operator._machine_security``.
    """
    config = arc_home() / "arcagent.toml"
    block: dict[str, Any] = {}
    if config.exists():
        try:
            block = tomllib.loads(config.read_text(encoding="utf-8")).get("security", {})
        except (OSError, tomllib.TOMLDecodeError):
            block = {}
    return str(arcagent.SecurityConfig(**block).custody)


def _operator_key() -> OperatorKey:
    """The on-box operator key that signs an approved capability.

    Read-only load (never bootstraps a key — an unpinned operator is no
    operator); a missing key raises and the caller fails the mutation with 500,
    exactly like ``routes/approvals.py._operator_authority``.

    Under ``vault_transit`` custody the seed never enters this process, so there
    is nothing here that can sign. Refuse rather than reach for whatever key
    happens to be on disk: a stale file left behind by a custody change would
    otherwise mint an authority the deployment deliberately moved to a vault.
    """
    if _machine_custody() == VAULT_TRANSIT:
        raise _OperatorSeedUnavailableError(f"custody={VAULT_TRANSIT}")
    return OperatorKey.load(default_operator_key_path(), generate_if_absent=False)


def _operator_did(key: OperatorKey) -> str:
    """The deployment operator DID recorded as signer and approver."""
    return OperatorApprovalAuthority(key.into_signer()).did


async def list_gated(request: Request) -> JSONResponse:
    """GET /api/trust/gated — gated capabilities across the server's agents."""
    provider = getattr(request.app.state, "roster_provider", None)
    entries = provider() if provider is not None else []
    gated: list[dict[str, Any]] = []
    for entry in entries:
        agent_root = Path(entry.workspace_path)
        try:
            items = await arcagent.list_gated(
                agent_root, agent_id=entry.agent_id, agent_label=entry.display_name
            )
        except Exception:  # reason: fleet resilience — one bad agent never sinks the list
            logger.warning("trust inventory failed for %s; contributing none", entry.agent_id)
            continue
        gated.extend(item.model_dump(mode="json") for item in items)
    return JSONResponse({"gated": gated})


async def read_source(request: Request) -> JSONResponse:
    """GET /api/trust/source?agent_id&name — the artifact text behind a gated row.

    Any authed role may read: reviewing a capability is what the approve gate
    demands first, and a viewer who cannot read it cannot review it. Only the
    operator may act on what they read.

    Fetched per row on demand rather than folded into ``/api/trust/gated``: that
    list polls every few seconds, so bundling artifact text would re-ship every
    capability's executable source to every open dashboard continuously.

    The ``hash`` returned is the same sha256 the row carries, so a surface can
    prove the text it rendered is the text an approval would sign.

    The artifact path comes from the inventory, never from the caller — a client
    names a capability, not a file, so no request can steer this read outside
    the agent's own capability tree.
    """
    agent_id = request.query_params.get("agent_id", "")
    name = request.query_params.get("name", "")
    if not agent_id or not name:
        return _error("agent_id and name are required", 400)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved
    try:
        gated = await arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label)
    except Exception:  # reason: an artifact bad enough to break the scan is unreviewable
        logger.warning("trust inventory failed for %s; source unavailable", agent_id)
        return _error(f"cannot read capability source for {name!r}", 404)
    item = next((entry for entry in gated if entry.name == name), None)
    if item is None:
        return _error(f"no gated capability named {name!r} for this agent", 404)

    source = arcagent.read_capability_source(Path(item.path))
    if source is None:
        return _error(f"cannot read capability source at {item.path}", 404)
    return JSONResponse(
        {
            "agent_id": agent_id,
            "name": name,
            "kind": item.kind,
            "path": item.path,
            "hash": item.hash,
            "source": source,
        }
    )


async def _read_body(request: Request) -> tuple[str, str] | JSONResponse:
    """Parse ``{agent_id, name}`` from the request body, or return a 400."""
    import json

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, ValueError):
        return _error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a JSON object", 400)
    agent_id = body.get("agent_id")
    name = body.get("name")
    if not isinstance(agent_id, str) or not isinstance(name, str) or not agent_id or not name:
        return _error("agent_id and name are required", 400)
    return agent_id, name


async def approve(request: Request) -> JSONResponse:
    """POST /api/trust/approve — sign a gated capability (operator)."""
    parsed = await _read_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    agent_id, name = parsed
    target = f"trust:{agent_id}:{name}"

    if not _is_operator(request):
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved

    try:
        signing_key = _operator_key()
    except _OperatorSeedUnavailableError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="denied",
            detail=f"operator seed not in this process ({exc})",
        )
        return _error(_VAULT_CUSTODY_REFUSAL, 500)
    except (FileNotFoundError, OSError) as exc:
        logger.exception("operator key unavailable for trust approval")
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="denied",
            detail="operator key unavailable",
        )
        return _error(f"operator_key_unavailable: {type(exc).__name__}", 500)
    approver = _operator_did(signing_key)

    # Discover the gated capability (arcagent), sign it (arcagent), re-scan.
    gated = await arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label)
    item = next((entry for entry in gated if entry.name == name), None)
    if item is None:
        detail = f"no gated capability named {name!r} for this agent"
        emit_mutation_audit(
            request, target=target, operation="trust.approve", outcome="denied", detail=detail
        )
        return _error(detail, 404)
    if arcagent.read_capability_source(Path(item.path)) is None:
        detail = f"cannot read capability source at {item.path}"
        emit_mutation_audit(
            request, target=target, operation="trust.approve", outcome="denied", detail=detail
        )
        return _error(detail, 404)

    try:
        # ``audit_sink`` is the chain this process already holds, so the
        # capability-level ``capability.signed`` record (REQ-323) lands beside
        # the HTTP-level mutation record below rather than opening a second one.
        arcagent.sign_capability(
            Path(item.path),
            signer_did=approver,
            private_key=signing_key.seed,
            config_path=agent_root / "arcagent.toml",
            audit_sink=operator_audit_sink(request),
        )
    except (OSError, ValueError) as exc:
        # A half-applied signing is a capability the operator believes is
        # trusted and is not — so it is audited, never silently 500'd.
        logger.exception("signing failed for %s", item.path)
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.approve",
            outcome="error",
            detail=f"signing failed: {type(exc).__name__}",
        )
        return _error(f"capability_signing_failed: {type(exc).__name__}", 500)
    after = await arcagent.list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    resolved_item = next((entry for entry in after if entry.name == name), item)
    emit_mutation_audit(request, target=target, operation="trust.approve", outcome="applied")
    return JSONResponse(resolved_item.model_dump(mode="json"))


async def disapprove(request: Request) -> JSONResponse:
    """POST /api/trust/disapprove — withdraw a capability's signature (operator)."""
    parsed = await _read_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    agent_id, name = parsed
    target = f"trust:{agent_id}:{name}"

    if not _is_operator(request):
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.disapprove",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved

    # Revoke against the artifact when the inventory still sees it — that removes
    # the signature, the trusted key, and the pin together. When the artifact is
    # gone there is nothing to unsign, so clear the orphaned pin by name alone.
    inventory = await arcagent.list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    item = next((entry for entry in inventory if entry.name == name), None)
    config_path = agent_root / "arcagent.toml"
    try:
        if item is None:
            _disapprove_pin(config_path, name=name)
        else:
            arcagent.revoke_capability(
                Path(item.path),
                config_path=config_path,
                operator_did=operator_actor_did(request),
                audit_sink=operator_audit_sink(request),
            )
    except (OSError, ValueError) as exc:
        logger.exception("revocation failed for %s on %s", name, agent_id)
        emit_mutation_audit(
            request,
            target=target,
            operation="trust.disapprove",
            outcome="error",
            detail=f"revocation failed: {type(exc).__name__}",
        )
        return _error(f"capability_revocation_failed: {type(exc).__name__}", 500)
    emit_mutation_audit(request, target=target, operation="trust.disapprove", outcome="applied")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/trust/gated", list_gated, methods=["GET"]),
    Route("/api/trust/source", read_source, methods=["GET"]),
    Route("/api/trust/approve", approve, methods=["POST"]),
    Route("/api/trust/disapprove", disapprove, methods=["POST"]),
]

__all__ = ["approve", "disapprove", "list_gated", "read_source", "routes"]
