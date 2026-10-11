"""Workflow routes — thin delegating adapter to the arcteam control plane.

SPEC-061 ArcFlow, COMP-023. ``GET/POST /api/workflows``, ``GET/PATCH
/api/workflows/{id}``, ``POST /api/workflows/{id}/{archive,unarchive,run}``,
``GET /api/workflows/{id}/runs``, ``GET /api/workflow-runs/{id}``, ``POST
/api/workflow-runs/{id}/cancel``, ``POST /api/workflow-tasks/{id}/gate``,
``POST /api/workflows/{id}/events/{name}``.

**Naming note:** a COMP-006 workflow Run is deliberately namespaced
``/api/workflow-runs/...``, never bare ``/api/runs/...`` — that path already
belongs to the existing per-request agent execution/trace system
(``observe_run.py``: ``GET /api/runs``, ``GET /api/runs/{run_id}/timeline``,
keyed by ``request_id``). The two "run" concepts are unrelated: a workflow
Run is the whole multi-node execution (COMP-006, its own aggregate); each
*node* of it dispatches as a task whose own ``task.run_id`` is what joins the
existing timeline route. A run's per-node detail should carry that
per-node ``task.run_id`` precisely so the dashboard opens the EXISTING
``/api/runs/{run_id}/timeline`` (unchanged, out of this module's scope) for
node-level tool/LLM/cost drill-down — see the Run Detail page in web/.

**The governing rule (non-negotiable, per the operator):** arcui is a viewer
and an initiator. It holds NO operational work. Every handler in this module
does exactly three things — authenticate the operator, translate the HTTP
request into one call on :class:`WorkflowControlPlane` (or
:class:`GateControlPlane`), and relay the result. There is no validation, no
versioning, no sequencing, no signing, no node execution, and no
gate-resolution logic here — deleting this file removes zero capability,
because every one of these operations remains reachable from the CLI
(COMP-019) and from agent conversation (COMP-012). Mirrors the operator-gate
-> guard -> delegate -> audit shape of ``routes/tasks.py`` exactly.

**Merge-reconciliation note (COMP-021 / COMP-018 not yet landed):** the two
Protocols below (``WorkflowControlPlane``, ``GateControlPlane``) are the seam
this module was written against. They describe the SDD's COMP-021 and
COMP-018 contracts respectively but are defined here — not imported from
``arcteam`` — because that package's real implementation is a concurrent,
not-yet-merged workstream. When COMP-021/COMP-018 land in ``arcteam``, this
module should import their real classes and drop the local Protocols,
PROVIDED the shapes match; if they diverge, this file's Protocols are the
contract the dashboard needs and one side must yield to the other in review.
``request.app.state.workflow_control_plane`` / ``request.app.state.
gate_control_plane`` are the injection points — ``None`` degrades every
route to 503 ``workflow_control_plane_unavailable`` (fail-open dashboard,
matching the ``messaging_service is None`` degrade pattern in team_chat.py).

**Identity attribution (SPEC-087 COMP-008):** every control-plane call carries
one :class:`OperatorActor` built from :func:`arcui.acting.acting_identity` —
the signed-in account's DID, the session pseudo-DID of the static token, or
for an app the person it acts for (``on_behalf_of``) and the app itself
(``via_app``). The roles handed to the gate control plane are that identity's
roles, so an app adds no authority to the person it acts for.

**App-reachable handlers:** ``POST /api/workflows/{id}/run``, ``POST
/api/workflows/{id}/events/{name}``, ``GET /api/workflow-runs/{id}`` and
``POST /api/workflow-tasks/{id}/gate`` are rows
of ``arcui.principals.APP_ROUTES`` with no agent in the path, so each one
resolves the target's owning agent itself (workflow owner, run owner, gate
row owner) and binds the scope to it; an unresolvable target is 404 to an
app. A gate decision also passes :func:`arcui.acting.decider`, and the
control plane's approver list (a refusal there is a 403).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from arcui.acting import (
    ActingIdentity,
    Refusal,
    Scope,
    acting_identity,
    app_target_refusal,
    decider,
)
from arcui.audit import emit_mutation_audit, emit_read_audit
from arcui.principals import PrincipalRateLimiter, roster_did
from arcui.routes.trust import operator_signer_for_request
from arcui.schemas import ErrorResponse

GateDecision = Literal["approve", "fail_run", "return_for_revision"]
GATE_OPERATION = "gate.resolve"
EVENT_OPERATION = "workflow.event.fire"

#: The largest event or gate payload (SPEC-088 REQ-605/REQ-613). The control
#: plane's schema check applies the same 64 KiB bound to what it validates;
#: this one stops an oversized body before it is read or parsed at all.
PAYLOAD_CAP_BYTES = 64 * 1024
#: A gate body carries ``decision`` and ``notes`` around a payload of up to
#: :data:`PAYLOAD_CAP_BYTES`; the read is bounded so no body is buffered whole.
_GATE_BODY_CAP_BYTES = 2 * PAYLOAD_CAP_BYTES
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: Events one principal may fire at one workflow per minute (REQ-608).
EVENT_RATE_PER_MINUTE = 60


@dataclass(frozen=True)
class OperatorActor:
    """The acting identity threaded into every control-plane call.

    The one actor type of this adapter (SPEC-087 COMP-008). ``did`` is who is
    acting (see module docstring); ``session_id`` is the auth layer's
    per-session correlation id; ``on_behalf_of`` and ``via_app`` name the
    person and the app when an app acts for someone; ``roles`` are the acting
    identity's roles — empty by default, so an actor built without them is
    no approver of anything (fail closed).
    """

    did: str
    session_id: str
    on_behalf_of: str | None = None
    via_app: str | None = None
    roles: frozenset[str] = frozenset()

    @property
    def principal_did(self) -> str:
        """The authenticated caller: the app when an app acts, else ``did``.

        An app acting for a person is still the principal (REQ-606): event run
        ids and rate limits are keyed to it, so one app can neither replay nor
        block another app's events for the same person.
        """
        return self.via_app or self.did


class WorkflowFieldError(BaseModel):
    """One typed validation error against a specific node/field.

    The exact wire shape SDD COMP-002 specifies: ``{node_id, field, error,
    observed, admissible}``. All three authoring surfaces (conversational,
    file, dashboard) share this one error contract (REQ-253) — arcui relays
    it verbatim, it never constructs or reinterprets one.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    field: str
    error: str
    observed: Any = None
    admissible: list[Any] | None = None


@dataclass(frozen=True)
class ControlPlaneResult:
    """The uniform outcome shape every control-plane operation returns.

    Exactly one of ``value`` / ``errors`` is populated on success/rejection;
    ``conflict``, ``not_found`` and ``forbidden`` are exclusive terminal flags
    (``forbidden``: the control plane refused this actor, e.g. not a gate
    approver). ``unprocessable`` marks ``errors`` as a payload that failed its
    schema (422) rather than a repairable request (400); ``replayed`` marks a
    ``value`` as an earlier result returned again (200, not 201). The route
    layer's entire job is translating this into an HTTP status — it never
    decides *whether* a request was valid, only how to report what the
    control plane already decided.
    """

    value: dict[str, Any] | None = None
    errors: list[WorkflowFieldError] | None = None
    conflict: bool = False
    not_found: bool = False
    forbidden: bool = False
    unprocessable: bool = False
    replayed: bool = False


@runtime_checkable
class WorkflowControlPlane(Protocol):
    """COMP-021 contract (arcteam.WorkflowControlPlane, SDD §COMP-021).

    The single shared operation set for every workflow mutation and
    initiation. Every caller — agent builder tools (COMP-012), the operator
    CLI (COMP-019), and this dashboard route layer — invokes these same
    operations, so there is exactly one implementation of what a workflow
    edit or run means.
    """

    async def list_workflows(
        self, *, actor: OperatorActor, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Every workflow summary: id, name, version, status, trigger, last run.

        Archived definitions are excluded unless ``include_archived`` is set —
        the active list is the default, and an operator opts into the retired
        ones (COMP-022).
        """
        ...

    async def get_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> dict[str, Any] | None:
        """Full definition detail: graph, version history (signer, reason)."""
        ...

    async def create_workflow(
        self, definition: dict[str, Any], *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Validate and persist a new draft definition."""
        ...

    async def patch_workflow(
        self,
        workflow_id: str,
        patch: dict[str, Any],
        *,
        expected_version: int,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        """Edit node/edge/trigger/channel fields under optimistic concurrency."""
        ...

    async def archive_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Hide from the active list; refuse new runs; retain history (COMP-022)."""
        ...

    async def unarchive_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Restore an archived definition as a draft (COMP-022)."""
        ...

    async def run_workflow(
        self, workflow_id: str, run_input: dict[str, Any], *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Start a run of a signed definition."""
        ...

    async def fire_event(
        self,
        workflow_id: str,
        name: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        """Start (or replay) the run a declared event names (SPEC-088 COMP-007).

        ``replayed`` is set when the same principal sent this key and payload
        before; the same key with another payload is a ``conflict``.
        """
        ...

    async def cancel_run(self, run_id: str, *, actor: OperatorActor) -> ControlPlaneResult:
        """Order cancellation of an in-flight run."""
        ...

    async def retry_node(
        self,
        run_id: str,
        node_id: str,
        *,
        actor: OperatorActor,
        accept_side_effect_repeat: bool = False,
    ) -> ControlPlaneResult:
        """Re-run one failed node of a failed run; completed nodes are kept.

        ``accept_side_effect_repeat`` is the operator's explicit release of a
        non-idempotent tool for the new attempt.
        """
        ...

    async def list_runs(self, workflow_id: str, *, actor: OperatorActor) -> list[dict[str, Any]]:
        """Run history for a definition."""
        ...

    async def get_run(self, run_id: str, *, actor: OperatorActor) -> dict[str, Any] | None:
        """One run's detail: status, path taken, per-node status."""
        ...

    async def workflow_owner_handle(self, workflow_id: str) -> str | None:
        """The owner handle (``@name``) of a workflow's definition; None when unknown."""
        ...

    async def run_owner_handle(self, run_id: str) -> str | None:
        """The owner handle of the definition a run is pinned to; None when unknown."""
        ...

    async def request_signature(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Queue this exact draft for the operator approval that signs it."""
        ...

    async def migrate_workflow(
        self,
        workflow_id: str,
        *,
        apply: bool,
        signer: Any | None,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        """Preview (``apply=False``) or apply the one-time bundle migration."""
        ...

    async def read_file(self, workflow_id: str, path: str) -> dict[str, Any] | None:
        """One companion file's text — the prompt a node actually runs."""
        ...

    async def test_run_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Try a draft with state-modifying work stubbed; needs no signature."""
        ...

    async def list_templates(self) -> list[dict[str, Any]]:
        """The starter templates: ``id``, ``title``, ``description``."""
        ...

    async def create_from_template(
        self, template: str, workflow_id: str, *, owner: str, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Copy a starter template in as a new unsigned draft owned by ``owner``."""
        ...

    async def write_file(
        self,
        workflow_id: str,
        path: str,
        content: str,
        *,
        expected_version: int,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        """Rewrite one companion file as a versioned definition edit."""
        ...


@runtime_checkable
class GateControlPlane(Protocol):
    """COMP-018 contract: control-plane-only gate resolution (SDD §COMP-018).

    A gate is a review-status task row naming a workflow node. Resolution is
    NOT a generic task approve/reject (``review -> done|todo``) — a rejected
    gate carries a real decision the runner must act on: fail the whole run,
    or return it for revision with human-authored notes (REQ-247). That
    decision, and the run-state transition it causes, is real operational
    work belonging to the runner (COMP-008) via this seam — never to arcui.
    No agent-callable tool may reach this operation (REQ-246); only this
    route, gated to the operator role, may call it.
    """

    async def resolve_gate(
        self,
        task_id: str,
        *,
        decision: str,
        notes: str,
        actor: OperatorActor,
        payload: dict[str, Any] | None = None,
    ) -> ControlPlaneResult:
        """Resolve a pending gate task with the reviewer's decision.

        ``decision`` is one of :data:`GateDecision` (or ``submit`` for a
        human_input step) — typed as plain ``str`` here because membership
        validation is the control plane's job, not this route's; arcui relays
        whatever the reviewer chose verbatim. ``payload`` is the typed data an
        approve or submit carries, validated by the control plane.
        """
        ...

    async def gate_owner_did(self, task_id: str) -> str | None:
        """The owning agent DID of a gate row; None when not a gate or unowned."""
        ...


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _errors_response(errors: list[WorkflowFieldError], status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"errors": [e.model_dump(mode="json") for e in errors]}, status_code=status
    )


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _actor_of(identity: ActingIdentity) -> OperatorActor:
    return OperatorActor(
        did=identity.actor_did,
        session_id=identity.session_id,
        on_behalf_of=identity.on_behalf_of,
        via_app=identity.via_app,
        roles=identity.roles,
    )


def _actor(request: Request) -> OperatorActor:
    return _actor_of(acting_identity(request))


async def _owner_did_of(request: Request, handle: str | None) -> str | None:
    """A definition's ``@owner`` handle as the roster agent's DID; None when unknown."""
    if handle is None:
        return None
    return await roster_did(request, handle.removeprefix("@"))


def _control_plane(request: Request) -> WorkflowControlPlane | None:
    return getattr(request.app.state, "workflow_control_plane", None)


def _gate_plane(request: Request) -> GateControlPlane | None:
    return getattr(request.app.state, "gate_control_plane", None)


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return None
    return body if isinstance(body, dict) else None


def _require_operator(request: Request, *, target: str, operation: str) -> JSONResponse | None:
    """Shared operator gate: 403 + denial audit, or ``None`` to proceed."""
    if _is_operator(request):
        return None
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="denied", detail="viewer role"
    )
    return _error("operator_role_required", 403)


def _relay(
    request: Request,
    result: ControlPlaneResult,
    *,
    target: str,
    operation: str,
    ok_status: int,
    applied_detail: str = "",
) -> JSONResponse:
    """Translate a `ControlPlaneResult` into the HTTP response + audit it.

    This is the one place every mutation route converges: the control plane
    already decided validity, conflict, and existence — this function only
    picks the matching status code and emits one audit record.
    """
    if result.not_found:
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="denied", detail="not_found"
        )
        return _error("not found", 404)
    if result.forbidden:
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="denied", detail="not_authorized"
        )
        return _error("not_authorized", 403)
    if result.conflict:
        emit_mutation_audit(
            request,
            target=target,
            operation=operation,
            outcome="denied",
            detail="version_conflict",
        )
        return _error("version_conflict", 409)
    if result.errors is not None:
        emit_mutation_audit(
            request,
            target=target,
            operation=operation,
            outcome="denied",
            detail="invalid_payload" if result.unprocessable else "validation_error",
        )
        return _errors_response(result.errors, 422 if result.unprocessable else 400)
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="applied", detail=applied_detail
    )
    return JSONResponse(result.value, status_code=ok_status)


async def list_workflows(request: Request) -> JSONResponse:
    """GET /api/workflows — every workflow summary (operator only)."""
    denial = _require_operator(request, target="workflow:list", operation="workflow.list")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    include_archived = request.query_params.get("include_archived") in ("1", "true", "True")
    workflows = await plane.list_workflows(
        actor=_actor(request), include_archived=include_archived
    )
    emit_read_audit(request, target="workflow:list", operation="workflow.list", outcome="ok")
    return JSONResponse({"workflows": workflows})


async def get_workflow(request: Request) -> JSONResponse:
    """GET /api/workflows/{id} — full definition detail (operator only)."""
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.read")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    definition = await plane.get_workflow(workflow_id, actor=_actor(request))
    if definition is None:
        emit_mutation_audit(
            request, target=target, operation="workflow.read", outcome="denied", detail="not_found"
        )
        return _error("not found", 404)
    emit_mutation_audit(request, target=target, operation="workflow.read", outcome="applied")
    return JSONResponse(definition)


def _owner_refusal(owner: object) -> JSONResponse | None:
    """A 400 unless ``owner`` is a non-empty string; the plane refuses the placeholder."""
    if isinstance(owner, str) and owner.strip():
        return None
    return _errors_response(
        [
            WorkflowFieldError(
                node_id="",
                field="owner",
                error="choose the agent that owns this workflow",
                observed=owner if isinstance(owner, str) else None,
            )
        ]
    )


async def create_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows — create a new draft (operator only)."""
    target = "workflow:new"
    denial = _require_operator(request, target=target, operation="workflow.create")
    if denial is not None:
        return denial

    body = await _json_body(request)
    if body is None:
        return _error("expected a JSON object body", 400)

    if "workflow" not in body:  # a full document carries its own owner
        refusal = _owner_refusal(body.get("owner"))
        if refusal is not None:
            return refusal

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.create_workflow(body, actor=_actor(request))
    if result.value is not None:
        target = f"workflow:{result.value.get('id', 'new')}"
    return _relay(request, result, target=target, operation="workflow.create", ok_status=201)


async def patch_workflow(request: Request) -> JSONResponse:
    """PATCH /api/workflows/{id} — edit node/edge/trigger/channel (operator only).

    Carries ``expected_version`` for optimistic concurrency (REQ-253) — the
    control plane, not this route, decides whether the version is stale.
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.patch")
    if denial is not None:
        return denial

    body = await _json_body(request)
    if body is None or "expected_version" not in body:
        return _error("expected a JSON body with expected_version", 400)
    expected_version = body["expected_version"]
    if not isinstance(expected_version, int):
        return _error("expected_version must be an integer", 400)
    patch = {k: v for k, v in body.items() if k != "expected_version"}
    if "owner" in patch:
        refusal = _owner_refusal(patch["owner"])
        if refusal is not None:
            return refusal

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.patch_workflow(
        workflow_id, patch, expected_version=expected_version, actor=_actor(request)
    )
    return _relay(request, result, target=target, operation="workflow.patch", ok_status=200)


async def archive_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/archive — hide + refuse new runs (operator only)."""
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.archive")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.archive_workflow(workflow_id, actor=_actor(request))
    return _relay(request, result, target=target, operation="workflow.archive", ok_status=200)


async def unarchive_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/unarchive — restore as a draft (operator only)."""
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.unarchive")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.unarchive_workflow(workflow_id, actor=_actor(request))
    return _relay(request, result, target=target, operation="workflow.unarchive", ok_status=200)


async def run_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/run — start a run of a signed definition.

    Operator, or an app holding ``workflows:run`` on the workflow's owner agent.
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    plane = _control_plane(request)

    async def _owner() -> str | None:
        if plane is None:
            return None
        return await _owner_did_of(request, await plane.workflow_owner_handle(workflow_id))

    denial = await app_target_refusal(
        request, Scope.WORKFLOWS_RUN, _owner, target=target, operation="workflow.run"
    )
    if denial is not None:
        return denial

    body = await _json_body(request) or {}

    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.run_workflow(workflow_id, body, actor=_actor(request))
    return _relay(request, result, target=target, operation="workflow.run", ok_status=201)


async def fire_event(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/events/{name} — start a run from a declared event.

    Operator, or an app holding ``workflows:event`` on the workflow's owner
    agent (SPEC-088 COMP-011). In order, each refusal audited: scope (403),
    the per-(principal, workflow) limit (429), a body over 64 KiB (413, before
    any JSON parse), a missing ``Idempotency-Key`` or a body that is not a
    JSON object (400). The control plane decides the rest: 201 for a new run,
    200 for a replay of the same key and payload, 404 for an unknown workflow
    or event, 409 for the same key with another payload, 422 naming every
    failing payload field. The run id derives from the principal (the app,
    when an app acts for a person), never from the person (REQ-606).
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}/event:{request.path_params['name']}"
    plane = _control_plane(request)

    async def _owner() -> str | None:
        if plane is None:
            return None
        return await _owner_did_of(request, await plane.workflow_owner_handle(workflow_id))

    denial = await app_target_refusal(
        request, Scope.WORKFLOWS_EVENT, _owner, target=target, operation=EVENT_OPERATION
    )
    if denial is not None:
        return denial
    limiter: PrincipalRateLimiter | None = getattr(
        request.app.state, "workflow_event_limiter", None
    )
    if plane is None or limiter is None:
        return _error("workflow_control_plane_unavailable", 503)

    actor = _actor(request)
    if not limiter.allow(json.dumps([actor.principal_did, workflow_id])):
        return _refused(request, target, EVENT_OPERATION, "rate_limited", 429)
    raw = await _capped_body(request, PAYLOAD_CAP_BYTES)
    if raw is None:
        return _refused(request, target, EVENT_OPERATION, "payload_too_large", 413)
    key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not key:
        return _refused(request, target, EVENT_OPERATION, "idempotency_key_required", 400)
    payload = _json_object(raw)
    if payload is None:
        return _refused(request, target, EVENT_OPERATION, "expected a JSON object body", 400)

    result = await plane.fire_event(
        workflow_id, request.path_params["name"], payload, idempotency_key=key, actor=actor
    )
    return _relay(
        request,
        result,
        target=target,
        operation=EVENT_OPERATION,
        ok_status=200 if result.replayed else 201,
    )


def _refused(
    request: Request, target: str, operation: str, reason: str, status: int
) -> JSONResponse:
    """One audited refusal of a request the caller was allowed to make but sent badly."""
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="denied", detail=reason
    )
    return _error(reason, status)


async def _capped_body(request: Request, cap: int) -> bytes | None:
    """The request body, or ``None`` once it is over ``cap`` bytes.

    The declared ``Content-Length`` is checked first, then the stream itself is
    counted as it arrives, so a lying or absent ``Content-Length`` cannot slip
    a larger body past the cap. Nothing past the cap is buffered or parsed.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return None
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > cap:
            return None
    return bytes(received)


def _json_object(raw: bytes) -> dict[str, Any] | None:
    """``raw`` parsed as a JSON object; ``None`` when it is not one."""
    try:
        parsed = json.loads(raw)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both subclass it
        return None
    return parsed if isinstance(parsed, dict) else None


def _payload_bytes(payload: dict[str, Any]) -> int:
    """A payload's size as the control plane's schema check measures it."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())


async def list_runs(request: Request) -> JSONResponse:
    """GET /api/workflows/{id}/runs — run history for a definition (operator only)."""
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.list_runs")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    runs = await plane.list_runs(workflow_id, actor=_actor(request))
    emit_mutation_audit(request, target=target, operation="workflow.list_runs", outcome="applied")
    return JSONResponse({"runs": runs})


async def get_run(request: Request) -> JSONResponse:
    """GET /api/workflow-runs/{id} — one run's live/final detail, path taken.

    Operator, or an app holding ``runs:read`` on the run's owner agent.
    """
    run_id = request.path_params["id"]
    target = f"workflow_run:{run_id}"
    plane = _control_plane(request)

    async def _owner() -> str | None:
        if plane is None:
            return None
        return await _owner_did_of(request, await plane.run_owner_handle(run_id))

    denial = await app_target_refusal(
        request, Scope.RUNS_READ, _owner, target=target, operation="run.read"
    )
    if denial is not None:
        return denial

    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    run = await plane.get_run(run_id, actor=_actor(request))
    if run is None:
        emit_mutation_audit(
            request, target=target, operation="run.read", outcome="denied", detail="not_found"
        )
        return _error("not found", 404)
    emit_mutation_audit(request, target=target, operation="run.read", outcome="applied")
    return JSONResponse(run)


async def cancel_run(request: Request) -> JSONResponse:
    """POST /api/workflow-runs/{id}/cancel — order cancellation of a run (operator only)."""
    run_id = request.path_params["id"]
    target = f"workflow_run:{run_id}"
    denial = _require_operator(request, target=target, operation="run.cancel")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.cancel_run(run_id, actor=_actor(request))
    return _relay(request, result, target=target, operation="run.cancel", ok_status=200)


async def retry_node(request: Request) -> JSONResponse:
    """POST /api/workflow-runs/{id}/nodes/{node}/retry — retry one failed node (operator only)."""
    run_id = request.path_params["id"]
    node_id = request.path_params["node"]
    target = f"workflow_run:{run_id}/{node_id}"
    denial = _require_operator(request, target=target, operation="run.retry_node")
    if denial is not None:
        return denial

    accept = await _accept_side_effect_repeat(request)
    if accept is None:
        return _error("accept_side_effect_repeat must be true or false", 400)

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.retry_node(
        run_id, node_id, actor=_actor(request), accept_side_effect_repeat=accept
    )
    return _relay(
        request,
        result,
        target=target,
        operation="run.retry_node",
        ok_status=200,
        applied_detail="accepted_repeat" if accept else "",
    )


async def _accept_side_effect_repeat(request: Request) -> bool | None:
    """The operator's explicit accept of a repeated side effect; ``None`` if malformed.

    Only a JSON ``true`` accepts. An absent body is "no accept"; anything that is
    not a boolean is refused rather than coerced, because the answer releases a
    non-idempotent tool to run twice.
    """
    if not await request.body():
        return False
    body = await _json_body(request)
    if body is None:
        return None
    value = body.get("accept_side_effect_repeat", False)
    return value if isinstance(value, bool) else None


async def resolve_gate(request: Request) -> Response:
    """POST /api/workflow-tasks/{id}/gate — resolve a gate or human_input node.

    Body: ``{"decision": "approve"|"fail_run"|"return_for_revision"|"submit",
    "notes"?: str, "payload"?: object}``. A payload over 64 KiB is 413; a
    payload that is not an object, or fails the step's schema, is 422 with one
    issue per field and the row keeps waiting. No agent-callable tool reaches
    this operation (REQ-246) — only this HTTP route and the equivalent
    CLI/channel-card caller of the same control-plane method. Who may decide:
    :func:`admit_gate_decider`; the decision records the acting person as
    ``decided_by`` and, when an app relayed it, that person as ``on_behalf_of``.
    """
    task_id = request.path_params["id"]
    target = f"workflow_task:{task_id}"
    admitted = await admit_gate_decider(request, task_id, target=target)
    if isinstance(admitted, Response):
        return admitted

    raw = await _capped_body(request, _GATE_BODY_CAP_BYTES)
    if raw is None:
        return _refused(request, target, GATE_OPERATION, "payload_too_large", 413)
    body = _json_object(raw) or {}
    decision = body.get("decision")
    if not isinstance(decision, str) or not decision:
        return _error('expected {"decision": "approve"|"fail_run"|"return_for_revision"}', 400)
    notes = body.get("notes", "")
    if not isinstance(notes, str):
        return _error("notes must be a string", 400)
    payload = body.get("payload")
    if payload is not None and not isinstance(payload, dict):
        # The wire shape, not the schema: the control plane takes an object.
        issue = WorkflowFieldError(node_id="", field="payload", error="must be a JSON object")
        result = ControlPlaneResult(errors=[issue], unprocessable=True)
        return _relay(request, result, target=target, operation=GATE_OPERATION, ok_status=200)
    if payload is not None and _payload_bytes(payload) > PAYLOAD_CAP_BYTES:
        return _refused(request, target, GATE_OPERATION, "payload_too_large", 413)

    result = await admitted.plane.resolve_gate(
        task_id, decision=decision, notes=notes, actor=admitted.actor, payload=payload
    )
    return _relay(request, result, target=target, operation=GATE_OPERATION, ok_status=200)


@dataclass(frozen=True)
class GateDecider:
    """A caller admitted to decide one gate: the plane to relay to, and as whom."""

    plane: GateControlPlane
    actor: OperatorActor


async def admit_gate_decider(
    request: Request, task_id: str, *, target: str
) -> GateDecider | Response:
    """Who may decide a gate (this route and ``POST /api/tasks/{id}/approve|reject``).

    In order, each refusal audited once: ``gates:resolve`` bound to the gate
    row's owner agent (an app on an unknown gate gets 404); the plane is
    present (503); :func:`arcui.acting.decider` (named person, federal
    delegation). The control plane then checks its approver list against the
    returned actor's DID and roles — a refusal there is a 403 from
    :func:`_relay`.
    """
    plane = _gate_plane(request)

    async def _owner() -> str | None:
        return None if plane is None else await plane.gate_owner_did(task_id)

    denial = await app_target_refusal(
        request, Scope.GATES_RESOLVE, _owner, target=target, operation=GATE_OPERATION
    )
    if denial is not None:
        return denial
    if plane is None:
        return _error("gate_control_plane_unavailable", 503)
    identity = await asyncio.to_thread(decider, request, Scope.GATES_RESOLVE)
    if isinstance(identity, Refusal):
        return JSONResponse({"error": identity.code}, status_code=identity.status)
    return GateDecider(plane=plane, actor=_actor_of(identity))


async def request_signature(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/request-signature — queue it for approval.

    The dashboard cannot sign either: it raises the same operator approval an
    agent raises, bound to this exact content hash, and approving THAT is what
    signs (REQ-224). One rail, whoever asked.
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.sign.request")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.request_signature(workflow_id, actor=_actor(request))
    return _relay(request, result, target=target, operation="workflow.sign.request", ok_status=201)


async def migrate_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/migrate — repair an unreadable bundle (operator only).

    Body ``{"apply": bool, "resign": bool}``; the default is a preview that
    writes nothing. ``resign`` signs through the operator signing handle, the
    same path ``arc workflow migrate --resign`` takes, so only the pinned
    operator key can re-sign. The body never carries a key.
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.migrate")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)
    body = await _json_body(request)
    if body is None:
        return _error("invalid_body", 400)
    apply = body.get("apply") is True
    signer = None
    if apply and body.get("resign") is True:
        try:
            signer = await asyncio.to_thread(operator_signer_for_request, request)
        except Exception:  # reason: an unresolvable operator key is a refusal, never a 500
            emit_mutation_audit(
                request,
                target=target,
                operation="workflow.migrate",
                outcome="denied",
                detail="operator_signer_unavailable",
            )
            return _error("operator_signer_unavailable", 503)
    result = await plane.migrate_workflow(
        workflow_id, apply=apply, signer=signer, actor=_actor(request)
    )
    operation = "workflow.migrate" if apply else "workflow.migrate.preview"
    detail = "" if result.value is None else str(result.value.get("action", ""))
    return _relay(
        request,
        result,
        target=target,
        operation=operation,
        ok_status=200,
        applied_detail=f"resign={signer is not None} action={detail}",
    )


async def test_run_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows/{id}/test-run — try a draft safely (operator only).

    Allowed on an unsigned draft at every tier: state-modifying work is stubbed
    and spend is capped by the control plane, which also audits the run.
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.test_run")
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.test_run_workflow(workflow_id, actor=_actor(request))
    return _relay(request, result, target=target, operation="workflow.test_run", ok_status=201)


async def list_workflow_templates(request: Request) -> JSONResponse:
    """GET /api/workflow-templates — the starter templates (operator only)."""
    denial = _require_operator(
        request, target="workflow:templates", operation="workflow.templates"
    )
    if denial is not None:
        return denial

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    return JSONResponse({"templates": await plane.list_templates()})


async def create_from_template(request: Request) -> JSONResponse:
    """POST /api/workflows/from-template — start a draft from a template (operator only).

    Body: ``{"template": str, "workflow_id": str, "owner": str}``. The copy lands unsigned,
    through the same create path every author uses.
    """
    denial = _require_operator(
        request, target="workflow:from-template", operation="workflow.create_from_template"
    )
    if denial is not None:
        return denial

    body = await _json_body(request)
    template = (body or {}).get("template")
    workflow_id = (body or {}).get("workflow_id")
    if not isinstance(template, str) or not isinstance(workflow_id, str):
        return _error('expected {"template": str, "workflow_id": str, "owner": str}', 400)
    refusal = _owner_refusal((body or {}).get("owner"))
    if refusal is not None:
        return refusal
    owner = str((body or {})["owner"])

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.create_from_template(
        template, workflow_id, owner=owner, actor=_actor(request)
    )
    return _relay(
        request,
        result,
        target=f"workflow:{workflow_id}",
        operation="workflow.create_from_template",
        ok_status=201,
    )


async def get_workflow_file(request: Request) -> JSONResponse:
    """GET /api/workflows/{id}/file?path=… — one prompt or schema body."""
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.file.read")
    if denial is not None:
        return denial

    path = request.query_params.get("path", "")
    if not path:
        return _error("expected ?path=<bundle-relative path>", 400)

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    body = await plane.read_file(workflow_id, path)
    if body is None:
        return _error("not found", 404)
    return JSONResponse(body)


async def put_workflow_file(request: Request) -> JSONResponse:
    """PUT /api/workflows/{id}/file — rewrite one prompt or schema body.

    A versioned edit, not a disk write: the bundle is the signed unit, so
    changing an instruction bumps the version and drops the signature exactly
    as changing a node does.
    """
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.file.write")
    if denial is not None:
        return denial

    body = await _json_body(request)
    path = (body or {}).get("path")
    content = (body or {}).get("content")
    expected_version = (body or {}).get("expected_version")
    if not isinstance(path, str) or not isinstance(content, str):
        return _error('expected {"path": str, "content": str, "expected_version": int}', 400)
    if not isinstance(expected_version, int):
        return _error("expected_version must be an integer", 400)

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.write_file(
        workflow_id, path, content, expected_version=expected_version, actor=_actor(request)
    )
    return _relay(request, result, target=target, operation="workflow.file.write", ok_status=200)


routes = [
    Route("/api/workflows", list_workflows, methods=["GET"]),
    Route("/api/workflows", create_workflow, methods=["POST"]),
    Route("/api/workflows/from-template", create_from_template, methods=["POST"]),
    Route("/api/workflow-templates", list_workflow_templates, methods=["GET"]),
    Route("/api/workflows/{id}", get_workflow, methods=["GET"]),
    Route("/api/workflows/{id}", patch_workflow, methods=["PATCH"]),
    Route("/api/workflows/{id}/request-signature", request_signature, methods=["POST"]),
    Route("/api/workflows/{id}/migrate", migrate_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/file", get_workflow_file, methods=["GET"]),
    Route("/api/workflows/{id}/file", put_workflow_file, methods=["PUT"]),
    Route("/api/workflows/{id}/archive", archive_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/unarchive", unarchive_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/run", run_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/events/{name}", fire_event, methods=["POST"]),
    Route("/api/workflows/{id}/test-run", test_run_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/runs", list_runs, methods=["GET"]),
    Route("/api/workflow-runs/{id}", get_run, methods=["GET"]),
    Route("/api/workflow-runs/{id}/cancel", cancel_run, methods=["POST"]),
    Route("/api/workflow-runs/{id}/nodes/{node}/retry", retry_node, methods=["POST"]),
    # ``{id:path}``, not ``{id}``: a workflow task id is
    # ``wf/{run_id}/{node_id}/{iteration}`` (``workflow.runner.node_task_id``),
    # so it CONTAINS slashes and the default converter — which matches a single
    # segment — could never match a real one. Every attempt to resolve a gate
    # over this route answered 404, percent-encoded or not, because uvicorn
    # decodes ``%2F`` back to ``/`` before Starlette routes. That made
    # ``resolve_gate`` — the single implementation of the one operation only a
    # human may perform (REQ-246) — unreachable over HTTP, which is the whole
    # of the dashboard's gate surface and the only cross-process entry an app
    # has to it.
    #
    # The greedy match is unambiguous here: ``node_task_id`` asserts that
    # neither the run id nor the node id contains a slash, and the trailing
    # ``/gate`` literal forces the split at the last segment.
    Route("/api/workflow-tasks/{id:path}/gate", resolve_gate, methods=["POST"]),
]

__all__ = [
    "ControlPlaneResult",
    "GateControlPlane",
    "GateDecider",
    "GateDecision",
    "OperatorActor",
    "WorkflowControlPlane",
    "WorkflowFieldError",
    "admit_gate_decider",
    "archive_workflow",
    "cancel_run",
    "create_from_template",
    "create_workflow",
    "fire_event",
    "get_run",
    "get_workflow",
    "list_runs",
    "list_workflow_templates",
    "list_workflows",
    "patch_workflow",
    "resolve_gate",
    "retry_node",
    "routes",
    "run_workflow",
    "test_run_workflow",
    "unarchive_workflow",
]
