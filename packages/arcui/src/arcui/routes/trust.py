"""Operator-gated capability-trust surface — the arcui half of ``arc trust``.

``GET  /api/trust/gated``       — list gated (non-loaded) capabilities across the
                                  server's agents (any authed role).
``POST /api/trust/approve``     — pin a gated capability's source hash (operator).
``POST /api/trust/disapprove``  — remove a pin (operator).

This module is a pure view: it DISCOVERS gated capabilities via
``arcagent.capabilities.inventory`` (arcagent owns loading) and MUTATES the
approval store via ``arctrust`` (arctrust owns trust/approval). It resolves
agents from the roster, gates mutations on the operator role, records the
approver as the on-box operator DID, and audits every mutation — mirroring
``routes/approvals.py`` exactly. The ``arcagent`` inventory seam is imported
lazily inside handlers because arcagent depends on arcui (the UIBridgeSink), so a
top-level import would close a dependency cycle; ``arctrust`` (a lower layer that
imports no siblings) is imported at module top.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctrust import OperatorKey, default_operator_key_path
from arctrust import approve as _approve_pin
from arctrust import disapprove as _disapprove_pin
from arctrust.policy import OperatorApprovalAuthority
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.trust")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _now() -> str:
    """RFC3339 UTC timestamp minted at the route boundary (injected into logic)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_agent(request: Request, agent_id: str) -> tuple[Path, str] | None:
    """Map ``agent_id`` to ``(agent_root, label)`` via the injected roster provider."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return Path(entry.workspace_path), entry.display_name
    return None


def _operator_did() -> str:
    """The on-box deployment operator DID recorded as the approver.

    Read-only load (never bootstraps a key — an unpinned operator is no
    operator); a missing key raises and the caller fails the mutation with 500,
    exactly like ``routes/approvals.py._operator_authority``.
    """
    signer = OperatorKey.load(default_operator_key_path(), generate_if_absent=False).into_signer()
    return OperatorApprovalAuthority(signer).did


async def list_gated(request: Request) -> JSONResponse:
    """GET /api/trust/gated — gated capabilities across the server's agents."""
    from arcagent.capabilities.inventory import list_gated as _list_gated

    provider = getattr(request.app.state, "roster_provider", None)
    entries = provider() if provider is not None else []
    gated: list[dict[str, Any]] = []
    for entry in entries:
        agent_root = Path(entry.workspace_path)
        try:
            items = await _list_gated(
                agent_root, agent_id=entry.agent_id, agent_label=entry.display_name
            )
        except Exception:  # reason: fleet resilience — one bad agent never sinks the list
            logger.warning("trust inventory failed for %s; contributing none", entry.agent_id)
            continue
        gated.extend(item.model_dump(mode="json") for item in items)
    return JSONResponse({"gated": gated})


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
    """POST /api/trust/approve — pin a gated capability's source hash (operator)."""
    from arcagent.capabilities.inventory import (
        list_gated as _list_gated,
    )
    from arcagent.capabilities.inventory import (
        pin_name_for,
        read_capability_source,
    )

    parsed = await _read_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    agent_id, name = parsed
    target = f"trust:{agent_id}:{name}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="trust.approve",
            outcome="denied", detail="viewer role",
        )
        return _error("operator_role_required", 403)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved

    try:
        approver = _operator_did()
    except (FileNotFoundError, OSError) as exc:
        logger.exception("operator key unavailable for trust approval")
        emit_mutation_audit(
            request, target=target, operation="trust.approve",
            outcome="denied", detail="operator key unavailable",
        )
        return _error(f"operator_key_unavailable: {type(exc).__name__}", 500)

    # Discover the gated capability (arcagent), pin its hash (arctrust), re-scan.
    gated = await _list_gated(agent_root, agent_id=agent_id, agent_label=label)
    item = next((entry for entry in gated if entry.name == name), None)
    if item is None:
        detail = f"no gated capability named {name!r} for this agent"
        emit_mutation_audit(
            request, target=target, operation="trust.approve", outcome="denied", detail=detail
        )
        return _error(detail, 404)
    source = read_capability_source(Path(item.path))
    if source is None:
        detail = f"cannot read capability source at {item.path}"
        emit_mutation_audit(
            request, target=target, operation="trust.approve", outcome="denied", detail=detail
        )
        return _error(detail, 404)

    _approve_pin(
        agent_root / "arcagent.toml",
        name=pin_name_for(item),
        source=source,
        approver=approver,
        timestamp=_now(),
    )
    after = await _list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    resolved_item = next((entry for entry in after if entry.name == name), item)
    emit_mutation_audit(request, target=target, operation="trust.approve", outcome="applied")
    return JSONResponse(resolved_item.model_dump(mode="json"))


async def disapprove(request: Request) -> JSONResponse:
    """POST /api/trust/disapprove — remove a capability's approval pin (operator)."""
    from arcagent.capabilities.inventory import list_gated as _list_gated
    from arcagent.capabilities.inventory import pin_name_for

    parsed = await _read_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    agent_id, name = parsed
    target = f"trust:{agent_id}:{name}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="trust.disapprove",
            outcome="denied", detail="viewer role",
        )
        return _error("operator_role_required", 403)

    resolved = _resolve_agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    agent_root, label = resolved

    # Resolve the loader's pin name from the inventory when present; else treat
    # the given name as the pin name directly (clears a pin for a deleted artifact).
    inventory = await _list_gated(
        agent_root, agent_id=agent_id, agent_label=label, include_loaded=True
    )
    item = next((entry for entry in inventory if entry.name == name), None)
    pin_name = pin_name_for(item) if item is not None else name
    _disapprove_pin(agent_root / "arcagent.toml", name=pin_name)
    emit_mutation_audit(request, target=target, operation="trust.disapprove", outcome="applied")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/trust/gated", list_gated, methods=["GET"]),
    Route("/api/trust/approve", approve, methods=["POST"]),
    Route("/api/trust/disapprove", disapprove, methods=["POST"]),
]

__all__ = ["approve", "disapprove", "list_gated", "routes"]
