"""Custody repair — answer each credential key the migration could not place (J1-4).

``GET  /api/custody``         — the review state: which legacy keys wait for an
                                answer (NAMES ONLY), where a key may be mapped, and
                                which connections are waiting on them. Operator only.
``POST /api/custody/resolve`` — the operator's answer for each key (map it to a
                                connection field, keep it in the file, or drop it
                                on purpose). Operator only; audited.

A refused legacy credential-file migration no longer stops arcui from starting; this
is how the operator finishes it without a terminal. The route holds no migration
logic: it translates the answers into :class:`arcagent.KeyDecisions` and calls the
same ``Connections.migrate_secrets`` the CLI and startup call, so the read-back
proof and the never-drop guarantee are unchanged. A secret value never appears in
a request, a response, an audit row or a log line: keys are names, and the file is
read only inside the migration.
"""

from __future__ import annotations

import logging
from typing import Any

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.custody")

_TARGET = "custody:legacy-credential-file"
_MAX_DECISIONS = 500
_ACTIONS = ("map", "keep", "drop")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _connections(request: Request) -> arcagent.Connections | None:
    factory = getattr(request.app.state, "credential_connections", None)
    return None if factory is None else factory()


def _denied(request: Request, operation: str) -> JSONResponse | None:
    if getattr(request.state, "role", None) == "operator":
        return None
    emit_mutation_audit(
        request, target=_TARGET, operation=operation, outcome="denied", detail="viewer role"
    )
    return _error("operator_role_required", 403)


def _status_body(report: arcagent.MigrationReport) -> dict[str, Any]:
    unresolved = [{"key": key, "reason": why, "kept": False} for key, why in report.unresolved]
    kept = [{"key": key, "reason": "undeclared", "kept": True} for key in report.kept]
    targets = [
        {"connection": name, "field": field}
        for name, _, field in (coordinate.partition("/") for coordinate in report.targets)
    ]
    return {
        "state": "needs_review" if report.unresolved else "ok",
        "keys": sorted([*unresolved, *kept], key=lambda row: row["key"]),
        "targets": targets,
        "affected_connections": list(report.connections) if report.unresolved else [],
    }


async def custody_status(connections: arcagent.Connections) -> dict[str, Any]:
    """The review state from a dry run: nothing is written, no value is read out."""
    try:
        report = await connections.migrate_secrets(dry_run=True)
    except arcagent.ExtensionError as exc:
        return {
            "state": "blocked",
            "code": exc.code,
            "keys": [],
            "targets": [],
            "affected_connections": [],
        }
    return _status_body(report)


async def get_custody(request: Request) -> JSONResponse:
    """GET /api/custody — what is waiting for an answer (operator only)."""
    denial = _denied(request, "custody.read")
    if denial is not None:
        return denial
    connections = _connections(request)
    if connections is None:
        return _error("custody_unavailable", 503)
    return JSONResponse(await custody_status(connections))


def _parse_decisions(body: Any) -> tuple[arcagent.KeyDecisions | None, str]:
    """``(decisions, "")`` or ``(None, reason)``. Drop needs the key typed back."""
    rows = body.get("decisions") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_DECISIONS:
        return None, "invalid_body"
    mapped: dict[str, tuple[str, str]] = {}
    kept: set[str] = set()
    dropped: set[str] = set()
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        action = row.get("action") if isinstance(row, dict) else None
        if not isinstance(key, str) or not key or action not in _ACTIONS:
            return None, "invalid_body"
        if action == "keep":
            kept.add(key)
        elif action == "drop":
            if row.get("confirm") != key:
                return None, "drop_confirmation_mismatch"
            dropped.add(key)
        else:
            connection, field = row.get("connection"), row.get("field")
            if not isinstance(connection, str) or not isinstance(field, str):
                return None, "invalid_body"
            mapped[key] = (connection, field)
    return arcagent.KeyDecisions(
        mapped=mapped, kept=frozenset(kept), dropped=frozenset(dropped)
    ), ""


async def resolve_custody(request: Request) -> JSONResponse:
    """POST /api/custody/resolve — apply the operator's per-key answers."""
    denial = _denied(request, "custody.resolve")
    if denial is not None:
        return denial
    connections = _connections(request)
    if connections is None:
        return _error("custody_unavailable", 503)
    try:
        body = await request.json()
    except Exception:  # reason: a malformed body is a client error, not a 500
        body = None
    decisions, problem = _parse_decisions(body)
    if decisions is None:
        emit_mutation_audit(
            request, target=_TARGET, operation="custody.resolve", outcome="denied", detail=problem
        )
        return _error(problem, 400)
    detail = (
        f"map={len(decisions.mapped)} keep={len(decisions.kept)} drop={len(decisions.dropped)}"
    )
    try:
        report = await connections.migrate_secrets(decisions=decisions)
    except arcagent.ExtensionError as exc:
        emit_mutation_audit(
            request, target=_TARGET, operation="custody.resolve", outcome="denied", detail=exc.code
        )
        keys = exc.details.get("keys")
        refusal: dict[str, Any] = {"error": exc.code}
        if isinstance(keys, list):
            refusal["keys"] = [key for key in keys if isinstance(key, str)]
        return JSONResponse(refusal, status_code=409)
    emit_mutation_audit(
        request, target=_TARGET, operation="custody.resolve", outcome="applied", detail=detail
    )
    return JSONResponse(
        {
            **await custody_status(connections),
            "result": {
                "migrated": list(report.migrated),
                "dropped": list(report.dropped),
                "kept": list(report.kept),
            },
        }
    )


routes = [
    Route("/api/custody", get_custody, methods=["GET"]),
    Route("/api/custody/resolve", resolve_custody, methods=["POST"]),
]

__all__ = ["custody_status", "get_custody", "resolve_custody", "routes"]
