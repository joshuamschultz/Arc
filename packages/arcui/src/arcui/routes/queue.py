"""Authenticated, tenant-scoped operator controls for the injected call queue."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import arcagent
import arctrust
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, emit_read_audit

_MAX_PAGE = 100
_MAX_BODY = 4096


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _audit(request: Request, *, operation: str, outcome: str, mutation: bool) -> bool:
    if getattr(request.app.state, "audit", None) is None:
        return False
    if mutation and getattr(request.app.state, "audit_worm", None) is None:
        return False
    try:
        if mutation:
            emit_mutation_audit(request, target="queue", operation=operation, outcome=outcome)
        else:
            emit_read_audit(request, target="queue", operation=operation, outcome=outcome)
    except Exception:
        return False
    return True


def _scope(
    request: Request, *, operation: str, mutation: bool = False
) -> arcagent.QueueReadScope | JSONResponse:
    did = getattr(request.state, "account_did", None)
    tenant = getattr(request.app.state, "queue_tenant_id", None)
    if getattr(request.state, "role", None) != "operator" or not isinstance(did, str):
        if not _audit(request, operation=operation, outcome="denied", mutation=mutation):
            return _error("queue_audit_unavailable", 503)
        return _error("operator_account_required", 403)
    if not isinstance(tenant, str) or not tenant:
        return _error("queue_scope_unavailable", 503)
    try:
        identity = arctrust.parse_did(did)
        if identity["org"] != tenant or not identity["hash"]:
            raise ValueError("account tenant mismatch")
        return arcagent.QueueReadScope(tenant_id=tenant)
    except (ValueError, ValidationError):
        if not _audit(request, operation=operation, outcome="denied", mutation=mutation):
            return _error("queue_audit_unavailable", 503)
        return _error("operator_scope_denied", 403)


def _queue(
    request: Request, *, operation: str, mutation: bool = False
) -> arcagent.CallQueueCoordinator | JSONResponse:
    coordinator = getattr(request.app.state, "queue_coordinator", None)
    if coordinator is None:
        _audit(request, operation=operation, outcome="error", mutation=mutation)
        return _error("queue_unavailable", 503)
    tenant = getattr(request.app.state, "queue_tenant_id", None)
    if coordinator.tenant_scope != tenant:
        _audit(request, operation=operation, outcome="denied", mutation=mutation)
        return _error("queue_scope_unavailable", 503)
    if getattr(coordinator.store, "requires_recovery_owner", False):
        if getattr(coordinator.store, "tenant_scope", None) != tenant:
            _audit(request, operation=operation, outcome="denied", mutation=mutation)
            return _error("queue_scope_unavailable", 503)
    return coordinator


async def _body(request: Request) -> dict[str, Any] | None:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        return None
    try:
        if int(request.headers.get("content-length", "0")) > _MAX_BODY:
            return None
        chunks = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > _MAX_BODY:
                    return None
        payload = json.loads(chunks)
    except (ValueError, TypeError, TimeoutError):
        return None
    return payload if isinstance(payload, dict) else None


def _revision(body: dict[str, Any]) -> int | None:
    value = body.get("expected_revision")
    return value if type(value) is int and value >= 0 else None


async def jobs(request: Request) -> JSONResponse:
    """Return one authenticated tenant page of payload-free job metadata."""
    scope = _scope(request, operation="queue.jobs.read")
    if isinstance(scope, JSONResponse):
        return scope
    queue = _queue(request, operation="queue.jobs.read")
    if isinstance(queue, JSONResponse):
        return queue
    if not _audit(request, operation="queue.jobs.read", outcome="attempted", mutation=False):
        return _error("queue_audit_unavailable", 503)
    if set(request.query_params) - {"owner_id", "state", "limit", "cursor"}:
        _audit(request, operation="queue.jobs.read", outcome="denied", mutation=False)
        return _error("unsupported_queue_filter", 400)
    try:
        limit = int(request.query_params.get("limit", "100"))
        if not 1 <= limit <= _MAX_PAGE:
            raise ValueError("invalid limit")
        scope = arcagent.QueueReadScope(
            tenant_id=scope.tenant_id,
            owner_id=request.query_params.get("owner_id"),
            state=request.query_params.get("state"),
        )
        page = await queue.metadata_page(
            scope, cursor=request.query_params.get("cursor"), limit=limit
        )
    except (ValueError, ValidationError):
        _audit(request, operation="queue.jobs.read", outcome="denied", mutation=False)
        return _error("invalid_queue_filter", 400)
    except Exception:
        _audit(request, operation="queue.jobs.read", outcome="error", mutation=False)
        return _error("queue_unavailable", 503)
    if not _audit(request, operation="queue.jobs.read", outcome="ok", mutation=False):
        return _error("queue_audit_unavailable", 503)
    return JSONResponse(
        {"jobs": [asdict(job) for job in page.jobs], "next_cursor": page.next_cursor}
    )


async def control(request: Request) -> JSONResponse:
    """Return versioned queue admission settings."""
    scope = _scope(request, operation="queue.control.read")
    if isinstance(scope, JSONResponse):
        return scope
    queue = _queue(request, operation="queue.control.read")
    if isinstance(queue, JSONResponse):
        return queue
    if not _audit(request, operation="queue.control.read", outcome="attempted", mutation=False):
        return _error("queue_audit_unavailable", 503)
    try:
        current = queue.control()
    except Exception:
        _audit(request, operation="queue.control.read", outcome="error", mutation=False)
        return _error("queue_unavailable", 503)
    if not _audit(request, operation="queue.control.read", outcome="ok", mutation=False):
        return _error("queue_audit_unavailable", 503)
    return JSONResponse(asdict(current))


async def _change_control(request: Request, action: str) -> JSONResponse:
    scope = _scope(request, operation=f"queue.{action}", mutation=True)
    if isinstance(scope, JSONResponse):
        return scope
    queue = _queue(request, operation=f"queue.{action}", mutation=True)
    if isinstance(queue, JSONResponse):
        return queue
    body = await _body(request)
    revision = _revision(body) if body is not None else None
    if revision is None:
        _audit(request, operation=f"queue.{action}", outcome="denied", mutation=True)
        return _error("expected_revision_required", 400)
    if not _audit(request, operation=f"queue.{action}", outcome="attempted", mutation=True):
        return _error("queue_audit_unavailable", 503)
    try:
        if action == "pause":
            current = await queue.pause(expected_revision=revision)
        elif action == "resume":
            current = await queue.resume(expected_revision=revision)
        else:
            if body is None:
                return _error("invalid_queue_limits", 400)
            if set(body) != {
                "expected_revision",
                "max_concurrent",
                "max_queued",
                "wait_timeout",
                "history_limit",
            }:
                _audit(request, operation=f"queue.{action}", outcome="denied", mutation=True)
                return _error("invalid_queue_limits", 400)
            limits = arcagent.QueueLimits(
                max_concurrent=body["max_concurrent"],
                max_queued=body["max_queued"],
                wait_timeout=body["wait_timeout"],
                history_limit=body["history_limit"],
            )
            current = await queue.configure(limits, expected_revision=revision)
    except (ValueError, ValidationError):
        _audit(request, operation=f"queue.{action}", outcome="denied", mutation=True)
        return _error("invalid_queue_limits", 400)
    except Exception:
        try:
            stale = queue.control().revision != revision
        except Exception:
            stale = False
        _audit(
            request,
            operation=f"queue.{action}",
            outcome="conflict" if stale else "error",
            mutation=True,
        )
        return _error(
            "stale_queue_revision" if stale else "queue_unavailable", 409 if stale else 503
        )
    if not _audit(request, operation=f"queue.{action}", outcome="applied", mutation=True):
        return _error("queue_outcome_uncertain", 503)
    return JSONResponse(asdict(current))


async def pause(request: Request) -> JSONResponse:
    """Pause admission when the expected controller revision still matches."""
    return await _change_control(request, "pause")


async def resume(request: Request) -> JSONResponse:
    """Resume admission when the expected controller revision still matches."""
    return await _change_control(request, "resume")


async def limits(request: Request) -> JSONResponse:
    """Update bounded admission settings with a revision fence."""
    return await _change_control(request, "configure")


async def cancel(request: Request) -> JSONResponse:
    """Request cancellation without treating a running provider call as stopped."""
    scope = _scope(request, operation="queue.cancel", mutation=True)
    if isinstance(scope, JSONResponse):
        return scope
    queue = _queue(request, operation="queue.cancel", mutation=True)
    if isinstance(queue, JSONResponse):
        return queue
    body = await _body(request)
    if body is None or set(body) != {"call_id", "expected_version"}:
        _audit(request, operation="queue.cancel", outcome="denied", mutation=True)
        return _error("invalid_cancel_request", 400)
    call_id, version = body["call_id"], body["expected_version"]
    if (
        not isinstance(call_id, str)
        or not 1 <= len(call_id) <= 256
        or type(version) is not int
        or version < 0
    ):
        _audit(request, operation="queue.cancel", outcome="denied", mutation=True)
        return _error("invalid_cancel_request", 400)
    if not _audit(request, operation="queue.cancel", outcome="attempted", mutation=True):
        return _error("queue_audit_unavailable", 503)
    try:
        result = await queue.cancel_scoped(call_id, scope=scope, expected_version=version)
    except Exception:
        _audit(request, operation="queue.cancel", outcome="error", mutation=True)
        return _error("queue_unavailable", 503)
    if not _audit(request, operation="queue.cancel", outcome=result.status, mutation=True):
        return _error("queue_outcome_uncertain", 503)
    if result.status == "unavailable":
        return _error("call_unavailable", 404)
    return JSONResponse(asdict(result), status_code=409 if result.status == "conflict" else 200)


routes = [
    Route("/api/queue/jobs", jobs, methods=["GET"]),
    Route("/api/queue/control", control, methods=["GET"]),
    Route("/api/queue/pause", pause, methods=["POST"]),
    Route("/api/queue/resume", resume, methods=["POST"]),
    Route("/api/queue/limits", limits, methods=["PUT"]),
    Route("/api/queue/cancel", cancel, methods=["POST"]),
]

__all__ = ["routes"]
