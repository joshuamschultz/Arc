"""The dashboard's view of arcteam's workflow control plane (SPEC-061 COMP-023).

``routes/workflows.py`` was written against two local Protocols while arcteam's
COMP-021 landed on a sibling branch, and the reconciliation the module's own
docstring asked for never happened: nothing ever set
``app.state.workflow_control_plane``, so every workflow screen answered 503 on
a deployment whose engine was running fine. This module is that reconciliation
— the one adapter between the route layer's shape and arcteam's real operations.

It holds no operational logic, by construction: every mutation is one call into
:class:`arcteam.workflow.control_plane.WorkflowControlPlane` (the same object
the CLI and the agent tools call), and every read is one call into the
definition store or the run store. What lives here is translation — a route
payload into a definition document, an arcteam result into the route layer's
``ControlPlaneResult`` — and nothing else. Deleting this file removes the
dashboard's workflow screens and removes no capability.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcui.routes.workflows import ControlPlaneResult, OperatorActor, WorkflowFieldError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from arcteam.workflow.control_plane import WorkflowControlPlane as TeamControlPlane

logger = logging.getLogger("arcui.workflow_plane")

_SLUG = re.compile(r"[^a-z0-9._-]+")

# What a dashboard-created draft starts as. The definition model requires at
# least one node, so "an empty draft" is not representable — the operator gets
# one placeholder node to edit rather than a validation error on the way in.
_STARTER_NODE = "start"

#: A task row's status as the dashboard's per-node vocabulary.
_NODE_STATUS: dict[str, str] = {
    "backlog": "pending",
    "todo": "pending",
    "in_progress": "running",
    "review": "waiting_gate",
    "done": "done",
    "failed": "failed",
}


def slugify(name: str) -> str:
    """A workflow id from a human name. Ids are names, never paths."""
    slug = _SLUG.sub("-", name.strip().lower()).strip("-.")
    return slug[:64] or "workflow"


class DashboardWorkflowPlane:
    """Implements both route-layer Protocols over arcteam's one operation set.

    Workflow operations and gate resolution land on the same object because
    they land on the same control plane — splitting them here would suggest two
    authorities where the requirement (REQ-246) insists on one.
    """

    def __init__(
        self,
        *,
        plane: TeamControlPlane,
        definitions: Any,
        runs: Any,
        tasks: Any,
        default_owner: str = "@operator",
    ) -> None:
        self._plane = plane
        self._definitions = definitions
        self._runs = runs
        self._tasks = tasks
        self._default_owner = default_owner

    # -- reads ---------------------------------------------------------------

    async def list_workflows(self, *, actor: OperatorActor) -> list[dict[str, Any]]:
        del actor
        summaries: list[dict[str, Any]] = []
        for workflow_id in self._definitions.list_ids(include_archived=True):
            bundle = self._load(workflow_id)
            if bundle is None:
                continue
            summaries.append(await self._summary(bundle))
        return summaries

    async def get_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> dict[str, Any] | None:
        del actor
        bundle = self._load(workflow_id)
        if bundle is None:
            return None
        detail = await self._summary(bundle)
        definition = bundle.definition
        detail["nodes"] = [
            node.model_dump(mode="json", exclude_none=True) for node in definition.nodes
        ]
        detail["edges"] = _edges(definition)
        detail["channel"] = definition.channel
        detail["versions"] = [{"version": v} for v in self._definitions.versions(workflow_id)]
        return detail

    async def list_runs(self, workflow_id: str, *, actor: OperatorActor) -> list[dict[str, Any]]:
        del actor
        rows = await self._runs.list_for_workflow(workflow_id)
        return [_run_summary(run) for run in rows]

    async def get_run(self, run_id: str, *, actor: OperatorActor) -> dict[str, Any] | None:
        del actor
        run = await self._runs.record(run_id)
        if run is None:
            return None
        detail = _run_summary(run)
        detail["workflow_id"] = run.workflow_id
        detail["version"] = run.workflow_version
        detail["path_taken"] = [entry.node_id for entry in run.path_taken]
        # Per-node state comes from the task rows — they carry the live status,
        # the row id a gate is resolved by, and the per-node run id that opens
        # the existing execution timeline. The Run's trace adds what has no row
        # at all: a branch that was considered and not taken.
        nodes: dict[str, dict[str, Any]] = {}
        for task in await self._tasks.query_by_flow_run(run_id):
            node_id = str(task.metadata.get("node_id", ""))
            if not node_id:
                continue
            nodes[node_id] = {
                "node_id": node_id,
                "status": _NODE_STATUS.get(task.status, "running"),
                "iteration": task.metadata.get("iteration"),
                "task_id": task.id,
                "task_run_id": task.metadata.get("run_id") or task.run_id,
                "kind": task.metadata.get("node_kind"),
            }
        for entry in run.path_taken:
            if entry.outcome == "skipped":
                nodes.setdefault(
                    entry.node_id,
                    {
                        "node_id": entry.node_id,
                        "status": "skipped",
                        "iteration": entry.loop_iteration,
                    },
                )
        detail["nodes"] = list(nodes.values())
        return detail

    # -- mutations -----------------------------------------------------------

    async def create_workflow(
        self, definition: dict[str, Any], *, actor: OperatorActor
    ) -> ControlPlaneResult:
        document = _document_from(definition, default_owner=self._default_owner)
        result = await self._plane.create(document, actor_did=actor.did)
        return await self._relay(result, workflow_id=str(document["workflow"]["id"]))

    async def patch_workflow(
        self,
        workflow_id: str,
        patch: dict[str, Any],
        *,
        expected_version: int,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        bundle = self._load(workflow_id)
        if bundle is None:
            return ControlPlaneResult(not_found=True)
        document = bundle.definition.to_document()
        _apply_patch(document, patch)
        result = await self._plane.edit(
            workflow_id,
            document,
            expected_version=expected_version,
            actor_did=actor.did,
            reason=str(patch.get("reason") or "edited from the dashboard"),
        )
        return await self._relay(result, workflow_id=workflow_id)

    async def archive_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        return await self._relay(
            await self._plane.archive(workflow_id, actor_did=actor.did), workflow_id=workflow_id
        )

    async def unarchive_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        return await self._relay(
            await self._plane.unarchive(workflow_id, actor_did=actor.did), workflow_id=workflow_id
        )

    async def run_workflow(
        self, workflow_id: str, run_input: dict[str, Any], *, actor: OperatorActor
    ) -> ControlPlaneResult:
        result = await self._plane.run(workflow_id, input=run_input, actor_did=actor.did)
        if not result.ok or result.run is None:
            return _errors(result)
        return ControlPlaneResult(value=_run_summary(result.run))

    async def cancel_run(self, run_id: str, *, actor: OperatorActor) -> ControlPlaneResult:
        result = await self._plane.cancel(run_id, actor_did=actor.did)
        if not result.ok or result.run is None:
            return _errors(result)
        return ControlPlaneResult(value=_run_summary(result.run))

    async def resolve_gate(
        self,
        task_id: str,
        *,
        decision: str,
        notes: str,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        """Relay the reviewer's choice. The plane decides what it means (REQ-246)."""
        result = await self._plane.resolve_gate(
            task_id, decision=decision, notes=notes, actor_did=actor.did
        )
        if not result.ok:
            return _errors(result)
        return ControlPlaneResult(value={} if result.run is None else _run_summary(result.run))

    # -- internals -----------------------------------------------------------

    def _load(self, workflow_id: str) -> Any | None:
        try:
            return self._definitions.load(workflow_id)
        except Exception:  # reason: an unloadable bundle is "not found" to a reader
            logger.warning("workflow %s could not be loaded", workflow_id, exc_info=True)
            return None

    async def _summary(self, bundle: Any) -> dict[str, Any]:
        definition = bundle.definition
        trigger = bundle.effective_trigger
        runs = await self._runs.list_for_workflow(definition.id)
        latest = max(runs, key=lambda r: r.created_at or "", default=None)
        return {
            "id": definition.id,
            "name": definition.description or definition.id,
            "version": definition.version,
            "status": bundle.status,
            "signer_did": bundle.signer_did,
            "trigger": (
                None if trigger is None else trigger.model_dump(mode="json", exclude_none=True)
            ),
            "last_run": None if latest is None else _run_summary(latest),
        }

    async def _relay(self, result: Any, *, workflow_id: str) -> ControlPlaneResult:
        if not result.ok:
            return _errors(result)
        value = await self.get_workflow(workflow_id, actor=OperatorActor(did="", session_id=""))
        return ControlPlaneResult(value=value or {"id": workflow_id})


def _errors(result: Any) -> ControlPlaneResult:
    """arcteam's typed issues, verbatim — the route layer reinterprets nothing."""
    issues = [
        WorkflowFieldError(
            node_id=str(getattr(issue, "node_id", "") or ""),
            field=str(getattr(issue, "field", "") or ""),
            error=str(getattr(issue, "error", issue)),
            observed=getattr(issue, "observed", None),
            admissible=list(getattr(issue, "admissible", ()) or ()) or None,
        )
        for issue in result.errors
    ]
    conflict = any(issue.field == "version" for issue in issues)
    return ControlPlaneResult(errors=issues, conflict=conflict)


def _run_summary(run: Any) -> dict[str, Any]:
    """One run, from either shape it arrives in.

    Reads come back as the canonical ``arcstore.runs.Run``; a just-started or
    just-cancelled run comes back as the runner's own ``FlowRun``. They name
    the same facts differently, and the dashboard sees one shape.
    """
    return {
        "run_id": getattr(run, "id", None) or run.run_id,
        "status": run.status,
        "started_at": getattr(run, "created_at", None) or getattr(run, "started_at", None),
        "ended_at": getattr(run, "completed_at", None),
    }


def _edges(definition: Any) -> list[dict[str, str]]:
    """The graph the dashboard draws, derived from each node's ``needs``."""
    edges = [{"from": need, "to": node.id} for node in definition.nodes for need in node.needs]
    edges.extend(
        {"from": node.id, "to": node.loop_back_to}
        for node in definition.nodes
        if node.loop_back_to is not None
    )
    return edges


def _document_from(body: dict[str, Any], *, default_owner: str) -> dict[str, Any]:
    """A create payload as a definition document.

    The dashboard's create sheet posts a name; a full document may also be
    posted. Either way the document is what reaches the control plane, so the
    validation, versioning, and draft-status rules are the same ones every
    other surface gets.
    """
    if "workflow" in body:
        return dict(body)
    name = str(body.get("name") or "").strip()
    workflow_id = str(body.get("id") or slugify(name))
    return {
        "workflow": {
            "id": workflow_id,
            "description": name or workflow_id,
            "owner": str(body.get("owner") or default_owner),
        },
        "node": [{"id": _STARTER_NODE, "kind": "agent"}],
    }


def _apply_patch(document: dict[str, Any], patch: dict[str, Any]) -> None:
    """Apply the route layer's patch keys onto a definition document.

    ``nodes`` replaces the whole node array (one contract for every node
    mutation); ``trigger`` and ``channel`` replace that one field. ``edges``
    is derived from ``needs`` and is therefore not writable.
    """
    if "nodes" in patch:
        document["node"] = list(patch["nodes"] or ())
    if "trigger" in patch:
        if patch["trigger"] is None:
            document.pop("trigger", None)
        else:
            document["trigger"] = dict(patch["trigger"])
    if "channel" in patch:
        document["workflow"]["channel"] = patch["channel"]
    if "name" in patch:
        document["workflow"]["description"] = patch["name"]


def build_dashboard_plane(
    *,
    runner: Any,
    workflows_root: Path | None = None,
    default_owner: str = "@operator",
) -> DashboardWorkflowPlane:
    """Wire the dashboard plane from the runner the fleet service already hosts.

    Everything the control plane needs — the definition store, the run store,
    and the tier — is already resolved on the runner, so this takes no second
    store, no second backend, and no second tier value that could disagree
    with the one the engine enforces.
    """
    from arcteam.workflow import parse_definition, validate_definition
    from arcteam.workflow.control_plane import WorkflowControlPlane

    definitions = runner.definitions
    root = workflows_root or definitions.root

    def _validate(definition: Any, *, pending_files: frozenset[str] = frozenset()) -> Any:
        return validate_definition(definition, bundle_root=root, pending_files=pending_files)

    plane = WorkflowControlPlane(
        definitions=definitions,
        parse=lambda document: parse_definition(dict(document)),
        validate=_validate,
        runner=runner,
        runs=runner.runs,
        tier=runner.tier,
    )
    return DashboardWorkflowPlane(
        plane=plane,
        definitions=definitions,
        runs=runner.runs,
        tasks=runner.tasks,
        default_owner=default_owner,
    )


__all__ = ["DashboardWorkflowPlane", "build_dashboard_plane", "slugify"]
