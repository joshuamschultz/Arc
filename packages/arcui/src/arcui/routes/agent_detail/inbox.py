"""Authenticated durable inbox routes for the Agent Detail mail view."""

from __future__ import annotations

from json import JSONDecodeError
from typing import Any

from arcstore.inbox import HandoffStatus, ParticipantRole, TraceMetadata
from arcstore.inbox_projection import participant, thread_id_for
from arcteam.mail import MailSendRequest, MailThreadClosedError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did


def _service(request: Request) -> Any | None:
    # The Agent Inbox is an ArcTeam capability.  Gateway sessions and a bare
    # ArcUI process do not provide a mailbox by accident.
    return getattr(request.app.state, "agent_mail", None)


def _reader(request: Request) -> Any | None:
    did = _agent_did(request, request.path_params["id"])
    return participant(did) if did else None


def _operator_sender(service: Any) -> Any | None:
    """Resolve the explicit operator participant; never reuse the agent DID."""
    did = getattr(service, "sender_did", None)
    return participant(did, role=ParticipantRole.HUMAN) if isinstance(did, str) and did else None


def _observe_authorized(request: Request, service: Any, *, target: str) -> JSONResponse | None:
    """Gate sensitive fleet mail on the key-backed operator identity."""
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.observe",
            outcome="denied",
            detail="viewer role",
        )
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    if _operator_sender(service) is None:
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.observe",
            outcome="denied",
            detail="operator mail identity unavailable",
        )
        return JSONResponse({"error": "operator_mail_identity_unavailable"}, status_code=503)
    return None


def _clearance(request: Request) -> str:
    return str(getattr(request.app.state, "inbox_clearance", "UNCLASSIFIED"))


def _limit(request: Request) -> int:
    try:
        return min(100, max(1, int(request.query_params.get("limit", "50"))))
    except ValueError:
        return 50


def _idempotency_key(request: Request) -> str | None:
    key = request.headers.get("Idempotency-Key")
    return key if key and key.strip() else None


async def get_inbox_threads(request: Request) -> JSONResponse:
    """List one agent's durable threads through keyset pagination."""
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    denied = _observe_authorized(request, service, target=f"inbox:{request.path_params['id']}")
    if denied is not None:
        return denied
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
    emit_mutation_audit(
        request,
        target=f"inbox:{request.path_params['id']}",
        operation="inbox.observe",
        outcome="applied",
    )
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
    thread_id = request.path_params["thread_id"]
    denied = _observe_authorized(request, service, target=f"inbox:{thread_id}")
    if denied is not None:
        return denied
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
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
    emit_mutation_audit(
        request, target=f"inbox:{thread_id}", operation="inbox.observe", outcome="applied"
    )
    return JSONResponse(
        {
            "messages": [message.model_dump(mode="json") for message in page.items],
            "next_cursor": page.page_info.next_cursor,
            "handoffs": [handoff.model_dump(mode="json") for handoff in handoffs],
        }
    )


async def get_inbox_search(request: Request) -> JSONResponse:
    """Search an agent inbox through the same authorized durable service."""
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    denied = _observe_authorized(
        request, service, target=f"inbox:{request.path_params['id']}:search"
    )
    if denied is not None:
        return denied
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    query = request.query_params.get("q", "")
    try:
        messages = await service.search(
            reader,
            query,
            limit=_limit(request),
            classification_max=_clearance(request),
        )
    except (PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    emit_mutation_audit(
        request,
        target=f"inbox:{request.path_params['id']}:search",
        operation="inbox.observe",
        outcome="applied",
        detail="search",
    )
    return JSONResponse({"messages": [message.model_dump(mode="json") for message in messages]})


async def post_inbox_read(request: Request) -> JSONResponse:
    """Record a reader-specific receipt; viewers cannot mutate mailbox state."""
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    denied = _observe_authorized(
        request, service, target=f"inbox:message:{request.path_params['message_id']}"
    )
    if denied is not None:
        return denied
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    target = f"inbox:message:{request.path_params['message_id']}"
    try:
        message = await service.mark_read(request.path_params["message_id"], reader=reader)
    except (KeyError, ValueError) as exc:
        emit_mutation_audit(
            request, target=target, operation="inbox.mark_read", outcome="denied", detail=str(exc)
        )
        return JSONResponse({"error": str(exc)}, status_code=404)
    emit_mutation_audit(request, target=target, operation="inbox.mark_read", outcome="applied")
    return JSONResponse({"message": message.model_dump(mode="json")})


#: Compose bodies are bounded like every other mail body (the bus caps at 64KB).
_MAX_SUBJECT = 200


def _closed(request: Request, *, target: str, operation: str, exc: Exception) -> JSONResponse:
    """The one-reply rule refused the mail: say where the conversation continues."""
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="denied", detail="thread_closed"
    )
    return JSONResponse({"error": "mail_thread_closed", "detail": str(exc)}, status_code=409)


def _compose_fields(payload: object) -> tuple[str | None, str] | str:
    """Validate ``{"subject"?: str, "body": str}``; return the fields or an error."""
    if not isinstance(payload, dict):
        return "expected JSON object"
    body = payload.get("body")
    subject = payload.get("subject")
    if not isinstance(body, str) or not body.strip():
        return "body must be a non-empty string"
    if subject is not None and (
        not isinstance(subject, str) or not subject.strip() or len(subject) > _MAX_SUBJECT
    ):
        return f"subject must be a non-empty string of at most {_MAX_SUBJECT} characters"
    return (subject.strip() if isinstance(subject, str) else None), body


async def post_inbox_compose(request: Request) -> JSONResponse:
    """Mail a new message from the operator to one agent (alpha-2 item 3).

    Operator role only; the ``Idempotency-Key`` header makes a retried submit
    return the same message instead of mailing twice. The mail is signed by the
    operator key and wakes the agent; its one reply lands in the same thread.
    """
    target = f"inbox:{request.path_params['id']}:compose"
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.compose",
            outcome="denied",
            detail="viewer role",
        )
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    sender = _operator_sender(service)
    if sender is None:
        return JSONResponse({"error": "operator_mail_identity_unavailable"}, status_code=503)
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    try:
        payload = await request.json()
    except (JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    fields = _compose_fields(payload)
    if isinstance(fields, str):
        return JSONResponse({"error": fields}, status_code=400)
    subject, body = fields
    idempotency_key = _idempotency_key(request)
    if idempotency_key is None:
        return JSONResponse({"error": "Idempotency-Key header is required"}, status_code=400)
    try:
        sent = await service.send(
            MailSendRequest(
                sender="user://operator",
                sender_did=sender.participant_id,
                to=(reader.participant_id,),
                subject=subject,
                body=body,
                idempotency_key=f"compose:{idempotency_key}",
                classification=_clearance(request),
            )
        )
    except (KeyError, PermissionError, ValueError) as exc:
        emit_mutation_audit(
            request, target=target, operation="inbox.compose", outcome="denied", detail=str(exc)
        )
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        emit_mutation_audit(
            request, target=target, operation="inbox.compose", outcome="deferred", detail=str(exc)
        )
        return JSONResponse({"error": str(exc)}, status_code=503)
    emit_mutation_audit(
        request,
        target=target,
        operation="inbox.compose",
        outcome="applied",
        detail=f"message={sent.message_id}; status={sent.status}",
    )
    return JSONResponse(
        {
            "message_id": sent.message_id,
            "conversation_id": sent.thread_id,
            "thread_id": thread_id_for(reader.participant_id, sent.thread_id),
            "status": sent.status,
        },
        status_code=201,
    )


async def post_inbox_reply(request: Request) -> JSONResponse:
    """Append an operator-authorized reply to the durable thread record.

    The operator may answer any fleet thread it can observe, including one
    between two agents it does not belong to: it joins the conversation through
    the observed agent's copy (role-gated here, audited below). The reply obeys
    the one-reply rule; a closed thread answers 409 naming the team channel.
    """
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
    denied = _observe_authorized(request, service, target=target)
    if denied is not None:
        return denied
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    sender = _operator_sender(service)
    if sender is None:
        return JSONResponse({"error": "operator_mail_identity_unavailable"}, status_code=503)
    try:
        payload = await request.json()
    except (JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    body = payload.get("body")
    reply_to_id = payload.get("reply_to_id")
    if not isinstance(body, str) or not body.strip():
        return JSONResponse({"error": "body must be a non-empty string"}, status_code=400)
    if reply_to_id is not None and (not isinstance(reply_to_id, str) or not reply_to_id):
        return JSONResponse({"error": "reply_to_id must be a non-empty string"}, status_code=400)
    idempotency_key = _idempotency_key(request)
    if idempotency_key is None:
        return JSONResponse({"error": "Idempotency-Key header is required"}, status_code=400)
    try:
        message = await service.reply(
            thread_id,
            sender=sender,
            body=body,
            reply_to_id=reply_to_id,
            idempotency_key=idempotency_key,
            classification_max=_clearance(request),
            observed_as=reader,
        )
    except MailThreadClosedError as exc:
        return _closed(request, target=target, operation="inbox.reply", exc=exc)
    except RuntimeError as exc:
        emit_mutation_audit(
            request, target=target, operation="inbox.reply", outcome="deferred", detail=str(exc)
        )
        return JSONResponse({"error": str(exc)}, status_code=503)
    except (KeyError, PermissionError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.reply",
            outcome="denied",
            detail=str(exc),
        )
        return JSONResponse({"error": str(exc)}, status_code=400)
    emit_mutation_audit(
        request,
        target=target,
        operation="inbox.reply",
        outcome="applied",
        detail=f"sender={sender.participant_id}; observed_as={reader.participant_id}",
    )
    return JSONResponse({"message": message.model_dump(mode="json")}, status_code=201)


async def post_inbox_handoff(request: Request) -> JSONResponse:
    """Create a classified internal handoff trace from an authenticated operator."""
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    target = f"inbox:{request.path_params['thread_id']}"
    denied = _observe_authorized(request, service, target=target)
    if denied is not None:
        return denied
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    try:
        body = await request.json()
    except (JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    targets = body.get("to") if isinstance(body, dict) else None
    if not isinstance(targets, list) or not all(
        isinstance(item, str) and item for item in targets
    ):
        return JSONResponse({"error": "to must be a non-empty participant list"}, status_code=400)
    idempotency_key = _idempotency_key(request)
    if idempotency_key is None:
        return JSONResponse({"error": "Idempotency-Key header is required"}, status_code=400)
    sender = _operator_sender(service)
    if sender is None:
        return JSONResponse({"error": "operator_mail_identity_unavailable"}, status_code=503)
    try:
        handoff = await service.create_handoff(
            request.path_params["thread_id"],
            sender=sender,
            recipients=tuple(participant(item, role=ParticipantRole.AGENT) for item in targets),
            source_message_id=body.get("source_message_id"),
            trace=TraceMetadata(
                trace_id=body.get("trace_id"),
                source_session_id=body.get("source_session_id"),
                classification=_clearance(request),
            ),
            idempotency_key=idempotency_key,
        )
    except RuntimeError as exc:
        emit_mutation_audit(
            request,
            target=f"inbox:{request.path_params['thread_id']}",
            operation="inbox.handoff",
            outcome="deferred",
            detail=str(exc),
        )
        return JSONResponse({"error": str(exc)}, status_code=503)
    except (KeyError, PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    emit_mutation_audit(
        request,
        target=f"inbox:{request.path_params['thread_id']}",
        operation="inbox.handoff",
        outcome="applied",
    )
    return JSONResponse({"handoff": handoff.model_dump(mode="json")}, status_code=201)


async def post_inbox_handoff_resolution(request: Request) -> JSONResponse:
    """Accept or decline an addressed handoff, with an immutable audit event."""
    handoff_id = request.path_params["handoff_id"]
    target = f"inbox:handoff:{handoff_id}"
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request, target=target, operation="inbox.handoff.resolve", outcome="denied"
        )
        return JSONResponse({"error": "operator_role_required"}, status_code=403)
    service, reader = _service(request), _reader(request)
    if service is None:
        return JSONResponse({"error": "durable_inbox_unavailable"}, status_code=503)
    denied = _observe_authorized(request, service, target=target)
    if denied is not None:
        return denied
    if reader is None:
        return JSONResponse({"error": "agent_not_found"}, status_code=404)
    actor = _operator_sender(service)
    if actor is None:
        return JSONResponse({"error": "operator_mail_identity_unavailable"}, status_code=503)
    try:
        payload = await request.json()
        status = HandoffStatus(payload["status"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "status must be accepted or declined"}, status_code=400)
    try:
        handoff = await service.resolve_handoff(
            handoff_id,
            recipient=reader,
            actor_did=actor.participant_id,
            status=status,
        )
    except RuntimeError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.handoff.resolve",
            outcome="deferred",
            detail=str(exc),
        )
        return JSONResponse({"error": str(exc)}, status_code=503)
    except (KeyError, PermissionError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="inbox.handoff.resolve",
            outcome="denied",
            detail=str(exc),
        )
        return JSONResponse({"error": str(exc)}, status_code=403)
    emit_mutation_audit(
        request,
        target=target,
        operation="inbox.handoff.resolve",
        outcome="applied",
        detail=(
            f"status={status.value}; actor={actor.participant_id}; "
            f"recipient={reader.participant_id}"
        ),
    )
    return JSONResponse({"handoff": handoff.model_dump(mode="json")})
