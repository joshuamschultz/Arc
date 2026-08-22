"""Authenticated durable inbox routes for the Agent Detail mail view."""

from __future__ import annotations

from typing import Any

from arcstore.inbox import ParticipantRole, TraceMetadata
from arcstore.inbox_projection import participant
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did


def _service(request: Request) -> Any | None:
    return getattr(request.app.state, "inbox_service", None)


def _reader(request: Request) -> Any | None:
    did = _agent_did(request, request.path_params["id"])
    return participant(did) if did else None


def _clearance(request: Request) -> str:
    return str(getattr(request.app.state, "inbox_clearance", "UNCLASSIFIED"))


def _limit(request: Request) -> int:
    try:
        return min(100, max(1, int(request.query_params.get("limit", "50"))))
    except ValueError:
        return 50


async def get_inbox_threads(request: Request) -> JSONResponse:
    """List one agent's durable threads through keyset pagination."""
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    try:
        inbox, threads, cursor = await service.list_threads(
            reader,
            cursor=request.query_params.get("cursor"),
            limit=_limit(request),
            classification_max=_clearance(request),
        )
    except (PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    return JSONResponse(
        {
            "inbox": inbox.model_dump(mode="json"),
            "threads": [thread.model_dump(mode="json") for thread in threads],
            "next_cursor": cursor,
        }
    )


async def get_inbox_messages(request: Request) -> JSONResponse:
    """Return authorized messages and their separate handoff trace view."""
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    thread_id = request.path_params["thread_id"]
    try:
        page = await service.list_messages(
            thread_id,
            reader=reader,
            cursor=request.query_params.get("cursor"),
            limit=_limit(request),
            classification_max=_clearance(request),
        )
        handoffs = await service.list_handoffs(
            thread_id, reader=reader, classification_max=_clearance(request)
        )
    except (KeyError, PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(
        {
            "messages": [message.model_dump(mode="json") for message in page.items],
            "next_cursor": page.page_info.next_cursor,
            "handoffs": [handoff.model_dump(mode="json") for handoff in handoffs],
        }
    )


async def post_inbox_read(request: Request) -> JSONResponse:
    """Record a reader-specific receipt; viewers cannot mutate mailbox state."""
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    try:
        message = await service.mark_read(request.path_params["message_id"], reader=reader)
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"message": message.model_dump(mode="json")})


async def post_inbox_reply(request: Request) -> JSONResponse:
    """Append an operator-authorized reply to the durable thread record."""
    thread_id = request.path_params["thread_id"]
    target = f"inbox:{thread_id}"
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.reply",
            outcome="denied",
            detail="viewer role",
        )
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    body = payload.get("body")
    reply_to_id = payload.get("reply_to_id")
    if not isinstance(body, str) or not body.strip():
        return JSONResponse({"error": "body must be a non-empty string"}, status_code=400)
    if reply_to_id is not None and (not isinstance(reply_to_id, str) or not reply_to_id):
        return JSONResponse({"error": "reply_to_id must be a non-empty string"}, status_code=400)
    try:
        message = await service.reply(
            thread_id,
            sender=reader,
            body=body,
            reply_to_id=reply_to_id,
            classification_max=_clearance(request),
        )
    except (KeyError, PermissionError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.reply",
            outcome="denied",
            detail=str(exc),
        )
        return JSONResponse({"error": str(exc)}, status_code=400)
    emit_mutation_audit(request, target=target, operation="inbox.reply", outcome="applied")
    return JSONResponse({"message": message.model_dump(mode="json")}, status_code=201)


async def post_inbox_handoff(request: Request) -> JSONResponse:
    """Create a classified internal handoff trace from an authenticated operator."""
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    targets = body.get("to") if isinstance(body, dict) else None
    if not isinstance(targets, list) or not all(
        isinstance(item, str) and item for item in targets
    ):
        return JSONResponse({"error": "to must be a non-empty participant list"}, status_code=400)
    try:
        handoff = await service.create_handoff(
            request.path_params["thread_id"],
            sender=reader,
            recipients=tuple(participant(item, role=ParticipantRole.AGENT) for item in targets),
            source_message_id=body.get("source_message_id"),
            trace=TraceMetadata(
                trace_id=body.get("trace_id"),
                source_session_id=body.get("source_session_id"),
                classification=_clearance(request),
            ),
        )
    except (KeyError, PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"handoff": handoff.model_dump(mode="json")}, status_code=201)
