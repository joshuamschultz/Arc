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

_CREATE_FIELDS = ("description", "priority", "owner_did", "tags", "requires_review")
# Fields a PATCH may write. A raw patch is never trusted wholesale (SEC-F4):
# status/id/created_at/run_id/blocked_by are managed by the store's own
# transitions, never by a client-supplied key.
_PATCH_FIELDS = ("title", *_CREATE_FIELDS)


def _first_error_message(exc: ValidationError) -> str:
    """Turn a Pydantic ``ValidationError`` into one actionable client message.

    The raw exception is noisy and leaks the model's internals; the operator
    only needs the offending field and why it was rejected (e.g. the injection
    guard). Pydantic prefixes ``ValueError`` text with ``"Value error, "`` —
    strip it so the sanitizer's own message reads cleanly.
    """
    err = exc.errors()[0]
    loc = ".".join(str(part) for part in err.get("loc", ())) or "body"
    msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{loc}: {msg}"


def _valid_edits(task_id: str, edits: dict[str, Any], *, allow_external_refs: bool) -> str | None:
    """Validate patched fields through the ``Task`` model (SEC-F2).

    A partial patch never constructs a full ``Task``, so it would otherwise
    bypass the model's injection/oversized/zero-width sanitizer. Build a probe
    task carrying the edited values and let the model's field validators run.
    Returns ``None`` when the patch is safe, else the client-facing reason.
    """
    probe: dict[str, Any] = {"id": task_id, "creator_did": _CREATOR, "title": "probe"}
    probe.update(edits)
    try:
        Task.model_validate(probe, context={"allow_external_refs": allow_external_refs})
    except ValidationError as exc:
        return _first_error_message(exc)
    return None


def _allow_external_refs(request: Request) -> bool:
    """Deployment ingest policy: may operator task text carry URLs/emails?

    Set by ``create_app`` from the deployment tier (federal → False). Absent on
    a bare test app → fail-closed to the federal default.
    """
    return bool(getattr(request.app.state, "allow_external_task_refs", False))


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

    fields: dict[str, Any] = {"id": str(uuid.uuid4()), "title": title, "creator_did": _CREATOR}
    for key in _CREATE_FIELDS:
        if key in body:
            fields[key] = body[key]
    # A rejected value (injection guard, over-length, tier-blocked URL) is a
    # client error, not a 500: validate here and surface the reason as a 400.
    try:
        task = Task.model_validate(
            fields, context={"allow_external_refs": _allow_external_refs(request)}
        )
    except ValidationError as exc:
        return _error(_first_error_message(exc), 400)

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
    reason = _valid_edits(task_id, edits, allow_external_refs=_allow_external_refs(request))
    if reason is not None:
        return _error(reason, 400)

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


async def move_task(request: Request) -> Response:
    """POST /api/tasks/{id}/move — operator board move to a new column.

    The human-controllable transition (e.g. ``backlog`` -> ``todo`` so the dispatch
    loop will run it). Delegates to the store's guarded ``set_status``: ``in_progress``
    is refused (only the dispatch claim enters it) and a currently-running task cannot be
    moved (409). Missing task -> 404; disallowed/raced move -> 409.
    """
    task_id = request.path_params["id"]
    target = f"task:{task_id}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="task.move", outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    body = await _json_body(request)
    status = str((body or {}).get("status") or "")
    if not status:
        return _error('expected {"status": <column>}', 400)

    store = request.app.state.task_store
    if await store.get(task_id) is None:
        return _error("not found", 404)
    updated = await store.set_status(task_id, status, actor_did=_CREATOR)
    if updated is None:
        emit_mutation_audit(
            request, target=target, operation="task.move", outcome="denied", detail=status
        )
        return _error("invalid_move", 409)

    emit_mutation_audit(
        request, target=target, operation="task.move", outcome="applied", detail=status
    )
    return JSONResponse(updated.model_dump(mode="json"))


async def _review_decision(request: Request, *, approve: bool) -> Response:
    """Shared body for the review gate's approve/reject routes (P3).

    Operator-gated; a task must be in ``review`` (409 otherwise) — approve moves
    it to ``done``, reject back to ``todo`` for re-dispatch. 404 if missing.
    """
    task_id = request.path_params["id"]
    operation = "task.approve" if approve else "task.reject"
    target = f"task:{task_id}"

    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="denied", detail="viewer role"
        )
        return _error("operator_role_required", 403)

    store = request.app.state.task_store
    task = await store.get(task_id)
    if task is None:
        return _error("not found", 404)

    # A workflow gate wears the ``review`` status but is not an ordinary review
    # task: rejecting it must fail the whole run (the runner acts on that
    # decision), never bounce it to ``todo`` where the dispatcher re-runs it and
    # it lands back in review — the loop an operator sees as "reject does
    # nothing". Gate resolution belongs to the runner's control plane, so it is
    # relayed there rather than flipped in the task store here.
    if str(task.metadata.get("node_kind", "")) == "gate":
        return await _resolve_gate(request, task_id, approve=approve, target=target)

    updated = (
        await store.approve_review(task_id, actor_did=_CREATOR)
        if approve
        else await store.reject_review(task_id, actor_did=_CREATOR)
    )
    if updated is None:
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="denied", detail="not_in_review"
        )
        return _error("task_not_in_review", 409)

    emit_mutation_audit(request, target=target, operation=operation, outcome="applied")
    return JSONResponse(updated.model_dump(mode="json"))


async def _resolve_gate(
    request: Request, task_id: str, *, approve: bool, target: str
) -> Response:
    """Relay a gate task's approve/reject to the runner's gate control plane.

    Approve continues the run; reject fails it and records the rejection the
    runner acts on — the terminal outcome the generic review flip could not
    produce. When no runner is embedded the plane is absent and the operator is
    told so (503) rather than silently falling back to the looping path.
    """
    from arcui.routes.workflows import _actor, _gate_plane, _relay

    plane = _gate_plane(request)
    if plane is None:
        return _error("gate_control_plane_unavailable", 503)
    decision = "approve" if approve else "fail_run"
    result = await plane.resolve_gate(
        task_id, decision=decision, notes="", actor=_actor(request)
    )
    return _relay(request, result, target=target, operation="gate.resolve", ok_status=200)


async def approve_task(request: Request) -> Response:
    """POST /api/tasks/{id}/approve — approve a review-gated task (review -> done)."""
    return await _review_decision(request, approve=True)


async def reject_task(request: Request) -> Response:
    """POST /api/tasks/{id}/reject — reject a review-gated task (review -> todo)."""
    return await _review_decision(request, approve=False)


# `{id:path}`, not `{id}`: workflow-gate tasks carry slash-bearing ids (e.g.
# `wf/run-bd64737f7416/approve/0`). uvicorn decodes `%2F` back to `/`, so a
# single-segment `{id}` never matches them and the request falls through to the
# GET-only SPA handler — POST there is a 405 the operator sees as "approval is
# broken". The trailing literal (`/approve`, `/cancel`, …) still anchors the
# match, so the greedy path converter binds the id and leaves the verb.
routes = [
    Route("/api/team/tasks", create_task, methods=["POST"]),
    Route("/api/tasks/{id:path}/cancel", cancel_task, methods=["POST"]),
    Route("/api/tasks/{id:path}/move", move_task, methods=["POST"]),
    Route("/api/tasks/{id:path}/approve", approve_task, methods=["POST"]),
    Route("/api/tasks/{id:path}/reject", reject_task, methods=["POST"]),
    Route("/api/tasks/{id:path}", patch_task, methods=["PATCH"]),
    Route("/api/tasks/{id:path}", delete_task, methods=["DELETE"]),
]

__all__ = [
    "approve_task",
    "cancel_task",
    "create_task",
    "delete_task",
    "patch_task",
    "reject_task",
    "routes",
]
