"""Workflow routes — thin delegating adapter to the arcteam control plane.

SPEC-061 ArcFlow, COMP-023. ``GET/POST /api/workflows``, ``GET/PATCH
/api/workflows/{id}``, ``POST /api/workflows/{id}/{archive,unarchive,run}``,
``GET /api/workflows/{id}/runs``, ``GET /api/workflow-runs/{id}``, ``POST
/api/workflow-runs/{id}/cancel``, ``POST /api/workflow-tasks/{id}/gate``.

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

**Identity attribution:** arcui holds no per-human agent identity (ADR-019).
Every operator-gated mutation here is attributed to the fixed on-box
operator DID plus the auth layer's session id — the same attribution model
``tasks.py``'s ``_CREATOR`` and ``approvals.py``'s ``_OPERATOR_DID`` already
use. REQ-245's "authenticated identity of the deciding human" is satisfied
at the resolution this deployment's auth layer actually offers: role
(operator) + session id, not a distinct per-browser-user DID — the dashboard
has never distinguished individual humans behind the one operator token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

# arcui holds no agent identity; operator-originated writes are attributed to
# this fixed DID (mirrors `_CREATOR` in tasks.py / `_OPERATOR_DID` in
# approvals.py).
_OPERATOR_DID = "did:arc:ui:operator"

GateDecision = Literal["approve", "fail_run", "return_for_revision"]


@dataclass(frozen=True)
class OperatorActor:
    """The acting identity threaded into every control-plane call.

    ``did`` is the fixed on-box operator identity (see module docstring);
    ``session_id`` is the auth layer's per-session correlation id, which is
    what the audit trail actually keys distinct dashboard sessions on.
    """

    did: str
    session_id: str


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
    ``conflict`` and ``not_found`` are exclusive terminal flags. The route
    layer's entire job is translating this into an HTTP status — it never
    decides *whether* a request was valid, only how to report what the
    control plane already decided.
    """

    value: dict[str, Any] | None = None
    errors: list[WorkflowFieldError] | None = None
    conflict: bool = False
    not_found: bool = False


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

    async def cancel_run(self, run_id: str, *, actor: OperatorActor) -> ControlPlaneResult:
        """Order cancellation of an in-flight run."""
        ...

    async def list_runs(self, workflow_id: str, *, actor: OperatorActor) -> list[dict[str, Any]]:
        """Run history for a definition."""
        ...

    async def get_run(self, run_id: str, *, actor: OperatorActor) -> dict[str, Any] | None:
        """One run's detail: status, path taken, per-node status."""
        ...

    async def request_signature(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        """Queue this exact draft for the operator approval that signs it."""
        ...

    async def read_file(self, workflow_id: str, path: str) -> dict[str, Any] | None:
        """One companion file's text — the prompt a node actually runs."""
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
    ) -> ControlPlaneResult:
        """Resolve a pending gate task with the reviewer's decision.

        ``decision`` is one of :data:`GateDecision` — typed as plain ``str``
        here because membership validation is the control plane's job, not
        this route's; arcui relays whatever the reviewer chose verbatim.
        """
        ...


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _errors_response(errors: list[WorkflowFieldError]) -> JSONResponse:
    return JSONResponse({"errors": [e.model_dump(mode="json") for e in errors]}, status_code=400)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _actor(request: Request) -> OperatorActor:
    session_id = getattr(request.state, "session_id", None) or "unknown"
    return OperatorActor(did=_OPERATOR_DID, session_id=session_id)


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
    request: Request, result: ControlPlaneResult, *, target: str, operation: str, ok_status: int
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
            detail="validation_error",
        )
        return _errors_response(result.errors)
    emit_mutation_audit(request, target=target, operation=operation, outcome="applied")
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
    emit_mutation_audit(
        request, target="workflow:list", operation="workflow.list", outcome="applied"
    )
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


async def create_workflow(request: Request) -> JSONResponse:
    """POST /api/workflows — create a new draft (operator only)."""
    target = "workflow:new"
    denial = _require_operator(request, target=target, operation="workflow.create")
    if denial is not None:
        return denial

    body = await _json_body(request)
    if body is None:
        return _error("expected a JSON object body", 400)

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
    """POST /api/workflows/{id}/run — start a run of a signed definition (operator only)."""
    workflow_id = request.path_params["id"]
    target = f"workflow:{workflow_id}"
    denial = _require_operator(request, target=target, operation="workflow.run")
    if denial is not None:
        return denial

    body = await _json_body(request) or {}

    plane = _control_plane(request)
    if plane is None:
        return _error("workflow_control_plane_unavailable", 503)

    result = await plane.run_workflow(workflow_id, body, actor=_actor(request))
    return _relay(request, result, target=target, operation="workflow.run", ok_status=201)


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
    """GET /api/workflow-runs/{id} — one run's live/final detail, path taken (operator only)."""
    run_id = request.path_params["id"]
    target = f"workflow_run:{run_id}"
    denial = _require_operator(request, target=target, operation="run.read")
    if denial is not None:
        return denial

    plane = _control_plane(request)
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


async def resolve_gate(request: Request) -> Response:
    """POST /api/workflow-tasks/{id}/gate — resolve a gate node (operator only).

    Body: ``{"decision": "approve"|"fail_run"|"return_for_revision", "notes"?:
    str}``. No agent-callable tool reaches this operation (REQ-246) — only
    this operator-gated HTTP route and the equivalent CLI/channel-card
    caller of the same control-plane method.
    """
    task_id = request.path_params["id"]
    target = f"workflow_task:{task_id}"
    denial = _require_operator(request, target=target, operation="gate.resolve")
    if denial is not None:
        return denial

    body = await _json_body(request)
    decision = (body or {}).get("decision")
    if not isinstance(decision, str) or not decision:
        return _error('expected {"decision": "approve"|"fail_run"|"return_for_revision"}', 400)
    notes = (body or {}).get("notes", "")
    if not isinstance(notes, str):
        return _error("notes must be a string", 400)

    plane = _gate_plane(request)
    if plane is None:
        return _error("gate_control_plane_unavailable", 503)

    result = await plane.resolve_gate(
        task_id, decision=decision, notes=notes, actor=_actor(request)
    )
    return _relay(request, result, target=target, operation="gate.resolve", ok_status=200)


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
    Route("/api/workflows/{id}", get_workflow, methods=["GET"]),
    Route("/api/workflows/{id}", patch_workflow, methods=["PATCH"]),
    Route("/api/workflows/{id}/request-signature", request_signature, methods=["POST"]),
    Route("/api/workflows/{id}/file", get_workflow_file, methods=["GET"]),
    Route("/api/workflows/{id}/file", put_workflow_file, methods=["PUT"]),
    Route("/api/workflows/{id}/archive", archive_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/unarchive", unarchive_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/run", run_workflow, methods=["POST"]),
    Route("/api/workflows/{id}/runs", list_runs, methods=["GET"]),
    Route("/api/workflow-runs/{id}", get_run, methods=["GET"]),
    Route("/api/workflow-runs/{id}/cancel", cancel_run, methods=["POST"]),
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
    "GateDecision",
    "OperatorActor",
    "WorkflowControlPlane",
    "WorkflowFieldError",
    "archive_workflow",
    "cancel_run",
    "create_workflow",
    "get_run",
    "get_workflow",
    "list_runs",
    "list_workflows",
    "patch_workflow",
    "resolve_gate",
    "routes",
    "run_workflow",
    "unarchive_workflow",
]
