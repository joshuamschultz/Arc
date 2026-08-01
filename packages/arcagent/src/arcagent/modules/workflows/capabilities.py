"""Decorator-form workflows module — SPEC-061 COMP-012.

Twelve module-level ``@tool`` functions expose the ArcFlow builder surface.
There is no ``@capability`` class: the engine's lifecycle belongs to the runner
host in arcgateway (COMP-009), not to a per-agent capability.

The surface is deliberately THIN. Every tool does exactly four agent-side jobs
and then delegates:

1. **Allowlist** the caller's fields (``models.project``) so an invented
   ``model``, ``temperature``, ``status``, or ``signature`` is dropped rather
   than forwarded (ASI02).
2. **Check quotas before validation work** — a node-count check is free, a
   whole-graph validation over 200 nodes is not, so the free check runs first
   and neither reaches the control plane on refusal (LLM10).
3. **Normalize inline free text** (NFKC + zero-width strip) *before* the
   injection scan, so a homoglyph payload folds to ASCII first (LLM01).
4. **Delegate** to arcteam's control plane (COMP-021) — the same operation set
   the command line and the dashboard call — and refuse to report a mutation as
   anything but a draft (REQ-223).

Graph validation, versioning, hashing, and signing are NOT here and must never
be: one implementation of what a workflow edit means is the only way three
authoring surfaces stay in agreement.

Audit is emitted CENTRALLY by the tool registry, keyed on each tool's declared
``classification`` — tools never call ``arctrust.audit.emit`` themselves.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arcagent.modules.workflows import _runtime
from arcagent.modules.workflows.models import (
    NODE_FIELDS,
    TRIGGER_FIELDS,
    issue,
    normalize_inline_text,
    project,
)
from arcagent.tools._decorator import hook, tool

_logger = logging.getLogger("arcagent.modules.workflows.capabilities")

# The only status a builder tool may ever report for a mutation. Signing is an
# out-of-band operator command whose key never enters an agent process
# (REQ-223/REQ-224), so a mutation that came back as anything else means a lower
# layer regressed — and this surface fails closed rather than reporting it.
_DRAFT = "draft"


async def _plane() -> Any:
    """Return the control plane, finishing lazy async wiring first, or None."""
    await _runtime.ensure_control_plane()
    return _runtime.state().control_plane


def _errors(*issues: dict[str, Any]) -> str:
    """Render a typed error list plus the agent's remaining repair budget."""
    return json.dumps(
        {
            "errors": list(issues),
            "max_repair_attempts": _runtime.state().config.max_repair_attempts,
        }
    )


def _unavailable() -> str:
    return _errors(
        issue(
            field="workflows",
            error=(
                "workflow control plane unavailable — the arcteam workflow engine "
                "is not installed or could not be opened"
            ),
        )
    )


def _from_exception(exc: Exception) -> str:
    """Convert a control-plane refusal into the one typed error shape.

    arcteam returns graph issues as ``ValidationIssue`` and raises parse and
    concurrency failures; both carry ``.issues`` in the same five-key shape, so
    an agent repairing a rejection never branches on where it came from.
    """
    raw = getattr(exc, "issues", None)
    if raw:
        return json.dumps(
            {
                "errors": [item.model_dump() for item in raw],
                "max_repair_attempts": _runtime.state().config.max_repair_attempts,
            }
        )
    # A stale edit is refused, never merged (REQ-248). Recognised by the
    # committed arcteam exception name rather than by message text.
    names = {klass.__name__ for klass in type(exc).__mro__}
    if "StaleEditError" in names:
        return _errors(
            issue(
                field="expected_version",
                error=f"stale edit refused: {exc}",
                admissible=("re-read the workflow with workflow_inspect, then retry",),
            )
        )
    return _errors(issue(field="workflow", error=str(exc)))


def _mutation(result: Any) -> str:
    """Render a mutation result, refusing anything that is not a draft.

    This is enforcement, not decoration: there must be no path by which a
    successful edit reports signed status, so the check lives on the surface
    rather than only in a test.
    """
    status = getattr(result, "status", None)
    if status != _DRAFT:
        _logger.error("Workflow mutation returned status %r; refusing to report it", status)
        return _errors(
            issue(
                field="status",
                error=(
                    f"mutation returned status {status!r}; a builder tool only ever "
                    f"produces a {_DRAFT} — signing is an out-of-band operator command"
                ),
                observed=status,
                admissible=(_DRAFT,),
            )
        )
    return json.dumps({"status": _DRAFT, **_dump(result)})


def _dump(result: Any) -> dict[str, Any]:
    """JSON-friendly form of a control-plane result (pydantic model or mapping)."""
    if isinstance(result, dict):
        return result
    dump = getattr(result, "model_dump", None)
    if dump is None:
        return {"result": str(result)}
    payload = dump(mode="json")
    return payload if isinstance(payload, dict) else {"result": payload}


def _clean_nodes(nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Project every node through the field allowlist."""
    return [project(node, NODE_FIELDS) for node in nodes or []]


def _text(value: str, field: str) -> str:
    """Normalize one inline free-text field; raises ``ValueError`` on refusal."""
    if not value:
        return ""
    limit = _runtime.state().config.max_inline_text_length
    try:
        return normalize_inline_text(value, max_length=limit)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _version_required(expected_version: int | None) -> dict[str, Any] | None:
    """The typed refusal for a missing ``expected_version``, or None if present."""
    if expected_version is not None:
        return None
    return issue(
        field="expected_version",
        error="an edit must declare the version it was based on so a stale edit is refused",
        admissible=("the `version` returned by workflow_inspect",),
    )


# --- Mutating tools --------------------------------------------------------


@tool(
    name="workflow_create",
    description="Create a new workflow as an unsigned draft from a list of nodes",
    classification="state_modifying",
    capability_tags=("workflows",),
    when_to_use="When a repeatable, multi-step process should become a named artifact.",
    requires_skill="workflow-builder",
)
async def workflow_create(
    workflow_id: str = "",
    description: str = "",
    owner: str = "",
    channel: str = "",
    nodes: list[dict[str, Any]] | None = None,
) -> str:
    """Create version 1 of a workflow as an unsigned draft."""
    st = _runtime.state()
    if not workflow_id:
        return _errors(issue(field="workflow_id", error="a workflow needs a stable id"))
    node_list = nodes or []
    if len(node_list) > st.config.max_nodes:
        return _errors(
            issue(
                field="nodes",
                error=f"node quota exceeded (max {st.config.max_nodes})",
                observed=len(node_list),
            )
        )
    plane = await _plane()
    if plane is None:
        return _unavailable()
    if len(await plane.list()) >= st.config.max_workflows:
        return _errors(
            issue(
                field="workflow_id",
                error=f"workflow quota exceeded (max {st.config.max_workflows})",
            )
        )
    try:
        clean_description = _text(description, "description")
    except ValueError as exc:
        return _errors(issue(field="description", error=str(exc), observed=description))
    try:
        result = await plane.create(
            workflow_id=workflow_id,
            description=clean_description,
            owner=owner,
            channel=channel,
            nodes=_clean_nodes(node_list),
            actor_did=st.identity.did,
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _mutation(result)


@tool(
    name="workflow_add_node",
    description="Add one node to a workflow; returns a new draft version",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_add_node(
    workflow_id: str = "",
    node: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> str:
    """Add a node and re-validate the whole graph."""
    st = _runtime.state()
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    plane = await _plane()
    if plane is None:
        return _unavailable()
    quota = await _node_quota_issue(plane, workflow_id, adding=1)
    if quota is not None:
        return _errors(quota)
    try:
        result = await plane.add_node(
            workflow_id=workflow_id,
            node=project(node or {}, NODE_FIELDS),
            expected_version=expected_version,
            actor_did=st.identity.did,
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _mutation(result)


async def _node_quota_issue(plane: Any, workflow_id: str, *, adding: int) -> dict[str, Any] | None:
    """Refuse a node addition that would breach the node quota, before validation."""
    max_nodes = _runtime.state().config.max_nodes
    current = await plane.inspect(workflow_id=workflow_id)
    definition = getattr(current, "definition", None)
    existing = len(getattr(definition, "nodes", ()) or ())
    if existing + adding > max_nodes:
        return issue(
            field="nodes",
            error=f"node quota exceeded (max {max_nodes})",
            observed=existing + adding,
        )
    return None


@tool(
    name="workflow_edit_node",
    description="Change allowlisted fields on one node; returns a new draft version",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_edit_node(
    workflow_id: str = "",
    node_id: str = "",
    updates: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> str:
    """Edit a node in place. A node id is immutable — a rename is remove + add."""
    st = _runtime.state()
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    # ``id`` is deliberately excluded: node ids are immutable once signed, so an
    # in-flight run pinned by hash and every historical audit row stay valid.
    allowed = NODE_FIELDS - {"id"}
    clean = project(updates or {}, allowed)
    if not clean:
        return _errors(
            issue(
                node_id=node_id,
                field="updates",
                error="no editable field supplied",
                observed=sorted(updates or {}),
                admissible=tuple(sorted(allowed)),
            )
        )
    plane = await _plane()
    if plane is None:
        return _unavailable()
    try:
        result = await plane.edit_node(
            workflow_id=workflow_id,
            node_id=node_id,
            updates=clean,
            expected_version=expected_version,
            actor_did=st.identity.did,
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _mutation(result)


@tool(
    name="workflow_remove_node",
    description="Remove one node from a workflow; returns a new draft version",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_remove_node(
    workflow_id: str = "",
    node_id: str = "",
    expected_version: int | None = None,
) -> str:
    """Remove a node and re-validate the whole graph (dangling needs surface here)."""
    st = _runtime.state()
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    plane = await _plane()
    if plane is None:
        return _unavailable()
    try:
        result = await plane.remove_node(
            workflow_id=workflow_id,
            node_id=node_id,
            expected_version=expected_version,
            actor_did=st.identity.did,
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _mutation(result)


@tool(
    name="workflow_set_trigger",
    description="Set or clear a workflow's trigger; returns a new draft version",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_set_trigger(
    workflow_id: str = "",
    trigger: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> str:
    """Declare when a workflow fires. Dispatch is typed — never a free-text prompt."""
    st = _runtime.state()
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    plane = await _plane()
    if plane is None:
        return _unavailable()
    try:
        result = await plane.set_trigger(
            workflow_id=workflow_id,
            trigger=project(trigger, TRIGGER_FIELDS) if trigger else None,
            expected_version=expected_version,
            actor_did=st.identity.did,
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _mutation(result)


@tool(
    name="workflow_set_channel",
    description="Bind a workflow to the group channel its runs narrate into",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_set_channel(
    workflow_id: str = "",
    channel: str = "",
    expected_version: int | None = None,
) -> str:
    """Bind the channel where node transitions, handoffs, and gates are narrated."""
    st = _runtime.state()
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    plane = await _plane()
    if plane is None:
        return _unavailable()
    try:
        result = await plane.set_channel(
            workflow_id=workflow_id,
            channel=channel,
            expected_version=expected_version,
            actor_did=st.identity.did,
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _mutation(result)


@tool(
    name="workflow_run",
    description="Start a run of a workflow with typed input",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_run(workflow_id: str = "", input: dict[str, Any] | None = None) -> str:  # noqa: A002 - matches JSON schema field name
    """Start a run. Refuses when the definition spans an unapproved composition."""
    st = _runtime.state()
    plane = await _plane()
    if plane is None:
        return _unavailable()
    bundle = await plane.inspect(workflow_id=workflow_id)
    if bundle is None:
        return _errors(issue(field="workflow_id", error=f"workflow '{workflow_id}' not found"))
    refusal = await _activation_refusal(bundle, workflow_id)
    if refusal is not None:
        return _errors(issue(field="workflow_id", error=refusal))
    try:
        result = await plane.run(
            workflow_id=workflow_id, input=input or {}, actor_did=st.identity.did
        )
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return json.dumps(_dump(result))


async def _activation_refusal(bundle: Any, workflow_id: str) -> str | None:
    """COMP-016 — operator approval for a definition spanning a forbidden set."""
    from arcagent.modules.workflows.activation import require_activation_grant, union_of_legs

    st = _runtime.state()
    definition = getattr(bundle, "definition", None)
    nodes = [_dump(node) for node in getattr(definition, "nodes", ()) or ()]
    union = union_of_legs(nodes, _tool_tags())
    return await require_activation_grant(
        human_gate=st.human_gate,
        root=st.workspace / st.config.workflows_dir,
        workflow_id=workflow_id,
        content_hash=str(getattr(bundle, "content_hash", "")),
        agent_did=st.identity.did,
        union=union,
    )


@tool(
    name="workflow_cancel_run",
    description="Cancel an in-flight workflow run",
    classification="state_modifying",
    capability_tags=("workflows",),
)
async def workflow_cancel_run(run_id: str = "") -> str:
    """Cancel a run. The Run record is marked before node cancellation fans out."""
    st = _runtime.state()
    plane = await _plane()
    if plane is None:
        return _unavailable()
    try:
        result = await plane.cancel_run(run_id=run_id, actor_did=st.identity.did)
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return json.dumps(_dump(result))


# --- Read-only tools -------------------------------------------------------


@tool(
    name="workflow_list",
    description="List this agent's workflows with status and current version",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_list() -> str:
    """List workflows so the agent can talk about what it owns."""
    plane = await _plane()
    if plane is None:
        return _unavailable()
    return json.dumps([_dump(bundle) for bundle in await plane.list()])


@tool(
    name="workflow_inspect",
    description="Show one workflow's graph, version, status, and content hash",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_inspect(workflow_id: str = "", version: int | None = None) -> str:
    """Render one workflow, optionally at a retained prior version."""
    plane = await _plane()
    if plane is None:
        return _unavailable()
    bundle = await plane.inspect(workflow_id=workflow_id, version=version)
    if bundle is None:
        return _errors(issue(field="workflow_id", error=f"workflow '{workflow_id}' not found"))
    return json.dumps(_dump(bundle))


@tool(
    name="workflow_runs",
    description="List a workflow's run history",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_runs(workflow_id: str = "") -> str:
    """List runs of one workflow, newest first."""
    plane = await _plane()
    if plane is None:
        return _unavailable()
    return json.dumps([_dump(run) for run in await plane.runs(workflow_id=workflow_id)])


@tool(
    name="workflow_run_status",
    description="Report one run's status, path taken, and per-node state",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_run_status(run_id: str = "") -> str:
    """Report a single run's live state, including the path actually taken."""
    plane = await _plane()
    if plane is None:
        return _unavailable()
    return json.dumps(_dump(await plane.run_status(run_id=run_id)))


# --- Hooks -----------------------------------------------------------------

# Tool name -> declared capability tags, captured from the capability registry
# at agent:ready. The activation check (COMP-016) resolves a tool node's legs
# through this map; without it a tool node contributes nothing statically and
# the runtime leg threader (COMP-015) is the only gate — still fail-closed, but
# the operator loses the pre-flight warning.
_tool_tags_map: dict[str, tuple[str, ...]] = {}


def _tool_tags() -> dict[str, tuple[str, ...]]:
    """The captured tool-name → capability-tags map."""
    return dict(_tool_tags_map)


@hook(event="agent:ready", priority=100)
async def workflows_capture_tool_tags(ctx: Any) -> None:
    """Capture the registry's tool→capability-tags map for the activation check."""
    data = ctx.data if hasattr(ctx, "data") else {}
    registry = data.get("skill_registry")
    tools = getattr(registry, "_tools", None)
    if not tools:
        return
    _tool_tags_map.clear()
    for name, entry in tools.items():
        _tool_tags_map[name] = tuple(getattr(entry, "capability_tags", ()) or ())


__all__ = [
    "workflow_add_node",
    "workflow_cancel_run",
    "workflow_create",
    "workflow_edit_node",
    "workflow_inspect",
    "workflow_list",
    "workflow_remove_node",
    "workflow_run",
    "workflow_run_status",
    "workflow_runs",
    "workflow_set_channel",
    "workflow_set_trigger",
    "workflows_capture_tool_tags",
]
