"""Operator-gated task mutation — create / edit / delete / cancel.

``POST /api/team/tasks``, ``PATCH``/``DELETE /api/tasks/{id}``,
``POST /api/tasks/{id}/cancel``.

SPEC-056 Phase D (D4, FR-7). Mirrors ``agent_detail/files_write.py``'s
operator-gate -> guard -> write -> audit shape and ``team_chat.
create_channel_route``'s create-resource wire convention (201, raw resource
dict in the body, no envelope). Edit is at-rest only (NFR-4): an
``in_progress`` task is steered via an arcteam message to its owner, not
edited here — see SDD §6.
"""

from __future__ import annotations

import uuid
from typing import Any

from arcstore.tasks import Task
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

# arcui holds no agent identity; operator-originated writes are attributed to
# this fixed DID (mirrors `_CALLER_DID` in agent_detail/_common.py).
_CREATOR = "did:arc:ui:operator"

_CREATE_FIELDS = ("description", "priority", "owner_did", "tags")
# Fields a PATCH may write. A raw patch is never trusted wholesale (SEC-F4):
# status/id/created_at/run_id/blocked_by are managed by the store's own
# transitions, never by a client-supplied key.
_PATCH_FIELDS = ("title", *_CREATE_FIELDS)


def _valid_edits(task_id: str, edits: dict[str, Any]) -> bool:
    """Validate patched fields through the ``Task`` model (SEC-F2).

    A partial patch never constructs a full ``Task``, so it would otherwise
    bypass the model's injection/oversized/zero-width sanitizer. Build a probe
    task carrying the edited values and let the model's field validators run;
    a ``ValidationError`` means the patch is unsafe.
    """
    probe: dict[str, Any] = {"id": task_id, "creator_did": _CREATOR, "title": "probe"}
    probe.update(edits)
    try:
        Task(**probe)
    except ValidationError:
        return False
    return True


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return None
    return body if isinstance(body, dict) else None


async def create_task(request: Request) -> JSONResponse:
    """POST /api/team/tasks — create a task (operator only)."""
    if not _is_operator(request):
        emit_mutation_audit(
            request,
            target="task:new",
            operation="task.create",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    body = await _json_body(request)
    if body is None:
        return _error("expected a JSON object body", 400)
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        return _error("missing or blank title", 400)

    fields: dict[str, Any] = {"title": title, "creator_did": _CREATOR}
    for key in _CREATE_FIELDS:
        if key in body:
            fields[key] = body[key]
    task = Task(id=str(uuid.uuid4()), **fields)

    store = request.app.state.task_store
    created = await store.create(task)

    emit_mutation_audit(
        request, target=f"task:{created.id}", operation="task.create", outcome="applied"
    )
    return JSONResponse(created.model_dump(mode="json"), status_code=201)


async def patch_task(request: Request) -> JSONResponse:
    """PATCH /api/tasks/{id} — edit an at-rest task (operator only)."""
    task_id = request.path_params["id"]
    target = f"task:{task_id}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="task.update", outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    body = await _json_body(request)
    if body is None:
        return _error("expected a JSON object body", 400)
    # Allowlist (SEC-F4): only editable fields survive; a client-supplied
    # `status`/`id`/... key is dropped, never written.
    edits = {key: body[key] for key in _PATCH_FIELDS if key in body}
    if not edits:
        return _error("no editable fields", 400)
    if not _valid_edits(task_id, edits):
        return _error("invalid field value", 400)

    store = request.app.state.task_store
    updated, outcome = await store.edit(task_id, edits, actor_did=_CREATOR)
    if outcome == "not_found":
        return _error("not found", 404)
    if outcome in ("in_progress", "conflict"):
        emit_mutation_audit(
            request,
            target=target,
            operation="task.update",
            outcome="denied",
            detail="task_in_progress",
        )
        return _error("task_in_progress", 409)
    if updated is None:  # pragma: no cover — row vanished between write and re-read
        return _error("not found", 404)

    emit_mutation_audit(request, target=target, operation="task.update", outcome="applied")
    return JSONResponse(updated.model_dump(mode="json"))


async def delete_task(request: Request) -> Response:
    """DELETE /api/tasks/{id} — remove a task (operator only).

    Destructive and irreversible (LLM06/ASI09), so operator-gated like the
    other mutations and audited whichever way it resolves. run_id/status are
    irrelevant to deletion — an operator can drop a task in any state (a
    never-run backlog item, a stuck task); the store emits its own
    tamper-evident ``mutable.delete`` on top of this route audit.
    """
    task_id = request.path_params["id"]
    target = f"task:{task_id}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="task.delete", outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    store = request.app.state.task_store
    existed = await store.delete(task_id, actor_did=_CREATOR)
    if not existed:
        return _error("not found", 404)

    emit_mutation_audit(request, target=target, operation="task.delete", outcome="applied")
    return Response(status_code=204)


async def cancel_task(request: Request) -> Response:
    """POST /api/tasks/{id}/cancel — request an operator stop of a running task.

    Sets the store's cancel flag; the owning agent's reliability watcher observes
    it and stops the live run (ASI09 human-in-the-loop kill switch). Only an
    ``in_progress`` task can be cancelled (nothing is running otherwise) -> 409;
    a missing task -> 404. arcui never touches the run directly (it runs in a
    separate process) — the durable flag is the whole mechanism.
    """
    task_id = request.path_params["id"]
    target = f"task:{task_id}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="task.cancel", outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    store = request.app.state.task_store
    if await store.get(task_id) is None:
        return _error("not found", 404)
    updated = await store.request_cancel(task_id, actor_did=_CREATOR)
    if updated is None:
        emit_mutation_audit(
            request, target=target, operation="task.cancel", outcome="denied", detail="not_running"
        )
        return _error("task_not_running", 409)

    emit_mutation_audit(request, target=target, operation="task.cancel", outcome="applied")
    return JSONResponse(updated.model_dump(mode="json"))


routes = [
    Route("/api/team/tasks", create_task, methods=["POST"]),
    Route("/api/tasks/{id}", patch_task, methods=["PATCH"]),
    Route("/api/tasks/{id}", delete_task, methods=["DELETE"]),
    Route("/api/tasks/{id}/cancel", cancel_task, methods=["POST"]),
]

__all__ = ["cancel_task", "create_task", "delete_task", "patch_task", "routes"]
