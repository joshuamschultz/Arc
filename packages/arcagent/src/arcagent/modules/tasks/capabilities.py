"""Decorator-form tasks module — SPEC-056 Phase B.

Ten module-level ``@tool`` functions expose the mission-control task
surface over the arcstore-backed ``TaskStore`` (Phase A). There is no
``@capability`` class (unlike the scheduler template's ``SchedulerEngine``).

Phase D adds an opt-in dispatch loop (``@background_task``): when
``config.dispatch`` is on, each tick pulls the agent's highest-priority
ready ``todo`` task — whether a teammate's ``assign_task`` or an arcui board
assignment put it there — starts it, and wakes a real agent run via the
``agent_run_fn`` bound at ``agent:ready`` (the same seam the messaging
module uses). Reacting to the durable arcstore owner write is what makes the
two assignment surfaces uniform: arcui runs in a separate process and cannot
sign an inter-agent envelope, so a poll of the shared store — not a pushed
message — is the only mechanism that covers a board assignment.

Audit is emitted CENTRALLY by the tool registry, keyed on each tool's
declared ``classification`` (SDD §3, deepen correction) — tools declare
classification, they never call ``arctrust.audit.emit`` themselves. Free
text (title/description) is sanitized by the arcstore ``Task`` model's own
field validator at construction (LLM01/ASI06), so an injection payload
raises ``ValidationError`` (a ``ValueError`` subclass) the tool catches and
returns as a clean ``{"error"}``. Owner-only mutation is gated by
:func:`_require_owner`, checked against the runtime state's ``identity``
before every state transition (create is exempt — there is no prior owner
to protect; assign is exempt — reassignment is not the owner's call, SDD
§3). Runtime state is fetched via :func:`_state`, not
``_runtime.state()`` directly — the module's async wiring (opening the
arcstore backend, and the live registry) is deferred and finished there on
first use, since ``_runtime.configure()`` itself is sync (see
``_runtime``'s module docstring for why).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import arcrun

from arcagent.core.session_internal.capability_ledger import (
    CarriedLegs,
    bind_carried_legs,
    carried_legs,
    reset_carried_legs,
)
from arcagent.fleet import FleetMember, FleetNotice, FleetNoticeKind
from arcagent.modules.tasks import _runtime
from arcagent.modules.tasks._dispatch_helpers import (
    backoff_elapsed as _backoff_elapsed,
)
from arcagent.modules.tasks._dispatch_helpers import (
    format_task_prompt as _format_task_prompt,
)
from arcagent.modules.tasks._dispatch_helpers import (
    is_stale as _is_stale,
)
from arcagent.modules.tasks._dispatch_helpers import (
    pick_agent as _pick_agent,
)
from arcagent.modules.tasks._dispatch_helpers import (
    resolve_timeout as _resolve_timeout,
)
from arcagent.modules.tasks._dispatch_helpers import (
    session_key as _session_key,
)
from arcagent.modules.tasks.models import Priority, Task
from arcagent.modules.tasks.node_execution import (
    WorkflowNode,
    _confined,
    allowed_strategies,
    artifact_escape_failure,
    artifact_failure,
    bind_node,
    current_node,
    escaping_artifacts,
    missing_artifacts,
    node_from_task,
    render_node_section,
    reset_node,
    resolve_schema,
    run_workspace,
    validate_output,
)
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.json_args import as_optional_object
from arcagent.utils.moment import moment_cues
from arcagent.utils.sanitizer import sanitize_text

# Backend failures are caught at the tool seam so a tool degrades to a clean
# structured error instead of crashing the agent.
_TOOL_ERRORS = (ValueError, TypeError, OSError)

_logger = logging.getLogger("arcagent.modules.tasks.capabilities")


async def _state() -> _runtime._State:
    """Fetch runtime state, finishing the module's lazy async wiring first.

    ``_runtime.configure()`` is sync (mirrors every other module — the
    dispatcher calls it without ``await``), so the backend and, when
    live, the registry are opened lazily by ``ensure_store()`` on first tool
    use rather than at configure time.
    """
    await _runtime.ensure_store()
    return _runtime.state()


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


async def _resolve_owner(owner: str | None, st: _runtime._State) -> str | None:
    """Resolve the ``owner`` tool argument to a DID.

    ``None`` (argument omitted) defaults to self; ``""`` leaves the task
    unowned (backlog); anything else is an address ref (``@handle``,
    ``did:...``) resolved through the fleet seam — unavailable when no fleet
    injected or built live (SDD §3).
    """
    if owner is None:
        self_did: str = st.identity.did
        return self_did
    if owner == "":
        return None
    if st.registry is None:
        msg = (
            "this agent has no fleet directory, so it cannot resolve a teammate "
            "by handle — create the task unowned, or address it by DID"
        )
        raise ValueError(msg)
    resolved: str = await st.registry.resolve(owner)
    return resolved


def _require_owner(task: Task, st: _runtime._State) -> None:
    """Raise ``ValueError`` unless ``task`` is unowned or owned by this agent.

    Applied before every mutation except create (no prior owner to protect)
    and assign (reassignment is not the owner's call — SDD §3/§7).
    """
    if task.owner_did is not None and task.owner_did != st.identity.did:
        msg = f"Task '{task.id}' is owned by another agent"
        raise ValueError(msg)


@tool(
    name="create_task",
    description="Create a task, owned by self (default), a teammate (@handle), or unowned",
    classification="state_modifying",
)
async def create_task(
    title: str = "",
    description: str = "",
    priority: str = "medium",
    owner: str | None = None,
    blocked_by: list[str] | None = None,
) -> str:
    st = await _state()
    try:
        owner_did = await _resolve_owner(owner, st)
        task_id = _new_task_id()
        deps = blocked_by or []
        # Reject a dependency cycle up front (P2): a cyclic task could never
        # become ready, so it must never be written (ASI08 — no unsatisfiable
        # work in the DAG). A brand-new id can only cycle via a self-edge here,
        # but the guard is the single enforcement point for every blocked_by.
        if await st.store.deps_would_cycle(task_id, deps):
            return json.dumps({"error": "blocked_by would create a dependency cycle"})
        task = Task(
            id=task_id,
            title=title,
            description=description,
            priority=priority,  # type: ignore[arg-type] # validated by Task's Literal on construction
            owner_did=owner_did,
            creator_did=st.identity.did,
            blocked_by=deps,
            # Seed the retry ceiling from config so this agent's created tasks
            # honor the deployment default; the per-task field is authoritative
            # thereafter (the reliability engine reads task.max_attempts).
            max_attempts=st.config.default_max_attempts,
        )
        created = await st.store.create(task)
        return str(created.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="update_task",
    description="Update title/description/priority on an owned, at-rest task",
    classification="state_modifying",
)
async def update_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
) -> str:
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        candidates: dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
        }
        updates = {k: v for k, v in candidates.items() if v is not None}
        updated = await st.store.update(id, updates, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="start_task",
    description="Start an owned (or unowned) task; unowned tasks are claimed by self",
    classification="state_modifying",
)
async def start_task(id: str = "") -> str:  # noqa: A002 - matches JSON schema field name
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        task, reason = await st.store.start_task(id, st.identity.did)
        if task is None:
            return json.dumps({"error": f"unable to start task '{id}' ({reason})"})
        return json.dumps({"reason": reason, "task": task.model_dump(mode="json")})
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="complete_task",
    description="Mark an owned task done, with resolution and optional structured output",
    classification="state_modifying",
)
async def complete_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    resolution: str = "",
    output: dict[str, Any] | None = None,
) -> str:
    st = await _state()
    try:
        output = as_optional_object(output, "output")
    except ValueError as exc:
        return json.dumps({"error": str(exc), "retryable": True})
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        if not await st.store.deps_met(current):
            return json.dumps({"error": f"Task '{id}' is blocked by unfinished dependencies"})
        refusal = _node_completion_refusal(st, current, output)
        if refusal is not None:
            return await _fail_node_attempt(st, current, refusal)
        await _seal_run_legs(st, current)
        if current.requires_review:
            # Opt-in human gate (P3): land in ``review``, not ``done`` — an
            # operator approves/rejects before it is terminal (LLM06/ASI09).
            updates: dict[str, Any] = {"status": "review", "resolution": resolution}
            if output is not None:
                updates["output"] = output
            updated = await st.store.update(id, updates, actor_did=st.identity.did)
            await _notify_operator(st, f"needs review: {current.title}", current.classification)
        else:
            updated = await st.store.finish(
                id, status="done", resolution=resolution, output=output, actor_did=st.identity.did
            )
            await _notify_operator(st, f"done: {current.title}", current.classification)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="fail_task",
    description="Mark an owned task failed, with a short resolution",
    classification="state_modifying",
)
async def fail_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    resolution: str = "",
) -> str:
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        updated = await st.store.finish(
            id, status="failed", resolution=resolution, actor_did=st.identity.did
        )
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        await _notify_operator(st, f"failed: {current.title}", current.classification, alert=True)
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="assign_task",
    description="Reassign an at-rest task to a teammate (@handle); rejects in-progress tasks",
    classification="state_modifying",
)
async def assign_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    to_handle: str = "",
) -> str:
    st = await _state()
    if st.registry is None:
        return json.dumps(
            {
                "error": "this agent has no fleet directory, so it cannot resolve "
                "a teammate by handle"
            }
        )
    try:
        to_did = await st.registry.resolve(to_handle)
        updated = await st.store.assign(id, to_did, st.identity.did)
        if updated is None:
            return json.dumps({"error": f"unable to assign task '{id}'"})
        await _notify_assignee(st, to_handle, updated)
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


async def _notify_assignee(st: _runtime._State, to_handle: str, task: Task) -> None:
    """Send a ``TASK_ASSIGNED`` hand-off to the assignee's inbox (SDD §5).

    Best-effort: the arcstore owner write above is already durable truth, so
    a delivery failure here (unreachable bus, unregistered sender, etc.) is
    logged and swallowed rather than surfaced to the caller — assign_task
    must not roll back or mask a successful write just because notification
    could not go out.
    """
    if st.messenger is None:
        return
    handle = to_handle.removeprefix("@")
    # Carry the task's classification onto the envelope (SEC-F3) so the
    # messenger's no-write-down check engages — an UNCLASSIFIED default would
    # leave it inert regardless of how sensitive the task is (ASI07).
    notice = FleetNotice(
        sender=st.identity.did,
        to=(f"agent://{handle}",),
        kind=FleetNoticeKind.TASK_ASSIGNED,
        body=f"@{handle} task_id={task.id} — {task.title}",
        classification=task.classification,
    )
    try:
        await st.messenger.send_notice(notice)
    except Exception:
        _logger.warning("failed to notify @%s of assignment for task '%s'", handle, task.id)


async def _notify_operator(
    st: _runtime._State, body: str, classification: str, *, alert: bool = False
) -> None:
    """Best-effort operator notification on a key task transition (P4).

    Sends to ``user://operator`` (trifecta-allowed). Gated by ``config.notify``
    and the presence of a live messenger; a delivery failure is logged and
    swallowed so it can NEVER block or roll back the transition that triggered
    it (the store write is already durable truth, and AU has recorded it).
    ``alert`` picks ``ALERT`` (failures/escalations) over ``INFO``.
    """
    if not st.config.notify or st.messenger is None:
        return
    notice = FleetNotice(
        sender=st.identity.did,
        to=("user://operator",),
        kind=FleetNoticeKind.ALERT if alert else FleetNoticeKind.INFO,
        body=body,
        classification=classification,
    )
    try:
        await st.messenger.send_notice(notice)
    except Exception:
        _logger.warning("failed to notify operator: %s", body[:80])


@tool(
    name="claim_task",
    description="Pull the next available task for self, respecting the in-progress cap",
    classification="state_modifying",
)
async def claim_task() -> str:
    st = await _state()
    try:
        task, reason = await st.store.claim_next(st.identity.did)
        return json.dumps(
            {"reason": reason, "task": task.model_dump(mode="json") if task else None}
        )
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="list_tasks",
    description="List tasks scoped to self or the whole team, optionally filtered by status",
    classification="read_only",
)
async def list_tasks(scope: str = "team", status: str | None = None) -> str:
    st = await _state()
    owner_did = st.identity.did if scope == "self" else None
    try:
        tasks = await st.store.list(status=status, owner_did=owner_did)
        return json.dumps([t.model_dump(mode="json") for t in tasks])
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="decompose_task",
    description="Break an owned task into sub-tasks; the parent becomes blocked_by them (FR-15)",
    classification="state_modifying",
)
async def decompose_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    subtasks: list[dict[str, Any]] | None = None,
) -> str:
    st = await _state()
    try:
        parent = await st.store.get(id)
        if parent is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(parent, st)
        # Build ALL subtasks before persisting any, so a bad title in a later
        # subtask (rejected by the Task model's field validator on construction)
        # can't leave earlier ones orphaned (no partial write).
        subs: list[Task] = []
        for spec in subtasks or []:
            subs.append(
                Task(
                    id=_new_task_id(),
                    title=spec.get("title", ""),
                    description=spec.get("description", ""),
                    priority=spec.get("priority", "medium"),
                    owner_did=st.identity.did,
                    creator_did=st.identity.did,
                    parent_id=id,
                )
            )
        # Cycle guard BEFORE any write (P2), so a rejected decompose leaves no
        # orphan children. Fresh leaf subtasks can't close a loop, but this keeps
        # every blocked_by mutation flowing through the one acyclicity check.
        proposed_blocked_by = [*parent.blocked_by, *(s.id for s in subs)]
        if await st.store.deps_would_cycle(id, proposed_blocked_by):
            return json.dumps({"error": "decompose would create a dependency cycle"})
        created_subs = [await st.store.create(sub) for sub in subs]
        updated_parent = await st.store.update(
            id, {"blocked_by": proposed_blocked_by}, actor_did=st.identity.did
        )
        if updated_parent is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return json.dumps(
            {
                "parent": updated_parent.model_dump(mode="json"),
                "subtasks": [s.model_dump(mode="json") for s in created_subs],
            }
        )
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="set_task_output",
    description="Attach a structured result ({summary, artifacts}) to an owned task (FR-13)",
    classification="state_modifying",
)
async def set_task_output(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    output: dict[str, Any] | None = None,
) -> str:
    st = await _state()
    try:
        output = as_optional_object(output, "output")
    except ValueError as exc:
        return json.dumps({"error": str(exc), "retryable": True})
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        # A workflow node's output is only recorded once it satisfies the node's
        # declared schema (REQ-237). Writing an unvalidated value here would make
        # it visible downstream, which is precisely the pass-forward typed
        # handoff exists to prevent.
        refusal = _node_completion_refusal(st, current, output, check_artifacts=False)
        if refusal is not None:
            return await _fail_node_attempt(st, current, refusal)
        updated = await st.store.update(id, {"output": output or {}}, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


def _node_completion_refusal(
    st: _runtime._State,
    task: Task,
    output: dict[str, Any] | None,
    *,
    check_artifacts: bool = True,
) -> str | None:
    """Why this workflow node may NOT be considered complete, or None.

    Two gates, both from the node's own declaration: the output must satisfy the
    declared ``output_schema`` (REQ-237) and every declared artifact must exist
    on disk (REQ-238). An ordinary task has neither and passes straight through.
    """
    node = node_from_task(task)
    if node is None:
        return None
    schema = resolve_schema(node, _bundle_root(st, node))
    if isinstance(schema, str):
        # The schema was declared but could not be resolved. Fail the node —
        # skipping the gate because its schema is unreadable is the very
        # pass-forward the requirement exists to prevent.
        return schema
    violation = validate_output(node, output, schema)
    if violation is not None:
        return violation
    if node.kind == "router" and (output is None or output.get("route") not in node.routes):
        return f"router output.route must be one of: {', '.join(node.routes)}"
    if not check_artifacts:
        return None
    # Confinement first: an escaping path is an attack, not an incomplete node,
    # and must be refused before anything stats the filesystem.
    root = run_workspace(_team_root(st), st.workspace, node.run_id)
    escaping = escaping_artifacts(node, root)
    if escaping:
        return artifact_escape_failure(escaping)
    missing = missing_artifacts(node, root)
    return artifact_failure(missing) if missing else None


def _team_root(st: _runtime._State) -> Path | None:
    """The shared team root, or None for a solo agent."""
    return Path(st.team_root) if st.team_root else None


def _bundle_root(st: _runtime._State, node: WorkflowNode) -> Path | None:
    """The bundle directory this node's prompt and schema files live in.

    The runner stamps it, because the runner is the only component that knows
    which store it dispatched from. Looking under the agent's OWN workspace
    instead — the previous behaviour — made every declared schema unreadable on
    a fleet, since bundles live in the deployment directory the operator signs
    into, not in five separate agent workspaces.

    The stamped value is runner-authored, never model-authored, and the
    reference read out of it is still confined to the bundle.
    """
    del st
    if node.bundle_root:
        stamped = Path(node.bundle_root)
        return stamped if stamped.is_dir() else None
    from arctrust.paths import workflows_dir

    bundle = _confined(workflows_dir(), node.workflow_id)
    return bundle if bundle is not None and bundle.is_dir() else None


async def _fail_node_attempt(st: _runtime._State, task: Task, reason: str) -> str:
    """Record a node completion refusal as a RETRYABLE attempt failure.

    Never a pass-forward and never a terminal failure by itself: the refusal
    feeds the existing retry engine, so the node gets its declared attempts to
    produce a conforming result and only then dead-letters.
    """
    _logger.warning("Workflow node task %s refused completion: %s", task.id, reason)
    await _handle_attempt_failure(st, task.id, st.identity.did, reason)
    return json.dumps({"error": reason, "retryable": True})


# ---------------------------------------------------------------------------
# Script nodes (SPEC-061) — deterministic subprocess execution, no model turn
# ---------------------------------------------------------------------------

# stdout beyond this is truncated before it becomes a node output: an uncapped
# script stdout is a token blowout and an injection surface downstream, exactly
# as an uncapped upstream value is (node_execution.MAX_OUTPUT_CHARS).
_SCRIPT_STDOUT_CAP = 262_144


def _script_command(target: Path) -> list[str] | None:
    """The argv to run a bundle script by kind — never a shell string.

    The script path stays a single argv element, so a crafted filename can never
    become a second command. ``.py`` runs on the runtime interpreter so a script
    sees the environment the fleet does; ``.sh``/``.bash`` run on ``bash``;
    anything else must carry its own execute bit and shebang.
    """
    suffix = target.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(target)]
    if suffix in (".sh", ".bash"):
        return ["bash", str(target)]
    if os.access(target, os.X_OK):
        return [str(target)]
    return None


def _parse_script_output(raw: str) -> dict[str, Any] | None:
    """A script's stdout as the node's structured output.

    A JSON object IS the output, so a script node can declare an output_schema
    and satisfy it; a JSON scalar/array is wrapped under ``result``; anything
    else is carried verbatim under ``stdout``. Empty stdout is no output — a node
    with an output_schema then fails the schema gate, which is correct.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return {"stdout": text[:_SCRIPT_STDOUT_CAP]}
    if isinstance(value, dict):
        return value
    return {"result": value}


def _parse_tool_output(raw: str) -> dict[str, Any]:
    """Normalize the governed tool result into the task output boundary."""
    try:
        value = json.loads(raw)
    except ValueError:
        return {"result": raw}
    return value if isinstance(value, dict) else {"result": value}


async def _run_tool_node(
    st: _runtime._State, task: Task, node: WorkflowNode, self_did: str
) -> None:
    """Execute one declared tool through the agent's governed tool projection."""
    registry = st.tool_registry
    if registry is None or node.tool is None:
        await _fail_node_attempt(st, task, "tool node has no governed tool registry")
        return
    declared = next((item for item in registry.to_arcrun_tools() if item.name == node.tool), None)
    if declared is None:
        await _fail_node_attempt(st, task, f"declared tool {node.tool!r} is unavailable")
        return
    context = arcrun.ToolContext(
        run_id=node.run_id,
        tool_call_id=node.idempotency_key,
        turn_number=node.attempt,
        event_bus=None,
        cancelled=asyncio.Event(),
    )
    try:
        output = _parse_tool_output(await declared.execute(node.args, context))
    except Exception as exc:
        await _fail_node_attempt(st, task, f"tool {node.tool!r} failed: {exc}")
        return
    refusal = _node_completion_refusal(st, task, output)
    if refusal is not None:
        await _fail_node_attempt(st, task, refusal)
        return
    await _seal_run_legs(st, task)
    await st.store.finish(
        task.id, status="done", resolution="tool executed", output=output, actor_did=self_did
    )
    await _notify_operator(st, f"done: {task.title}", task.classification)


async def _run_script_node(
    st: _runtime._State, task: Task, node: WorkflowNode, self_did: str
) -> None:
    """Execute a ``script`` node deterministically and transition its task.

    The bundle's signed script runs as a subprocess in the run's shared
    workspace — no model, no tools. Its stdout becomes the node output, held to
    the SAME schema and artifact gates an agent node's output is
    (``_node_completion_refusal``): a script node is a first-class, verified step
    and not a trapdoor around the contract. A non-zero exit or a failed gate is a
    retryable attempt, exactly like an agent node's refusal.
    """
    bundle = _bundle_root(st, node)
    if bundle is None:
        await _fail_node_attempt(st, task, f"script node '{node.node_id}' has no reachable bundle")
        return
    target = _confined(bundle, node.script or "")
    if target is None or not target.is_file():
        await _fail_node_attempt(
            st, task, f"script {node.script!r} is missing or escapes the workflow bundle"
        )
        return
    command = _script_command(target)
    if command is None:
        await _fail_node_attempt(
            st, task, f"script {node.script!r} is not runnable (.py, .sh, or an executable file)"
        )
        return
    workdir = run_workspace(_team_root(st), st.workspace, node.run_id)
    workdir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "ARC_RUN_ID": node.run_id,
        "ARC_NODE_ID": node.node_id,
        "ARC_BUNDLE_ROOT": str(bundle),
        "ARC_WORKDIR": str(workdir),
        # Upstream outputs, typed as the runner validated them — handed to the
        # script as data in the environment, never spliced into the command.
        "ARC_UPSTREAM": json.dumps(node.upstream, default=str)[:_SCRIPT_STDOUT_CAP],
    }
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(workdir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    finally:
        # A cancel (operator stop or the reliability timeout) must not orphan the
        # child: kill it before the coroutine unwinds.
        if proc.returncode is None:
            proc.kill()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:500] or f"exit code {proc.returncode}"
        await _fail_node_attempt(st, task, f"script exited non-zero: {detail}")
        return
    output = _parse_script_output(stdout.decode(errors="replace"))
    refusal = _node_completion_refusal(st, task, output)
    if refusal is not None:
        await _fail_node_attempt(st, task, refusal)
        return
    await _seal_run_legs(st, task)
    await st.store.finish(
        task.id, status="done", resolution="script executed", output=output, actor_did=self_did
    )
    await _notify_operator(st, f"done: {task.title}", task.classification)


# ---------------------------------------------------------------------------
# Dispatch loop (SPEC-056 Phase D) — assigned tasks actually run
# ---------------------------------------------------------------------------

# Poll cadence for the dispatch loop. A fixed decorator arg (interval cannot
# read per-agent config at decoration time); the real on/off switch is
# ``config.dispatch``, checked inside each tick. 15s is responsive enough for
# an operator assigning work from the board without hammering the shared DB.
_DISPATCH_TICK = 15.0

# Faster cadence for the reliability watcher (cancel + stuck-reclaim). It only
# reads the agent's own in_progress tasks and cancels in-memory handles, so it
# is cheap; a short interval makes operator "stop" feel responsive.
_RELIABILITY_TICK = 5.0

# Highest-priority-first ordering (mirrors arcstore's claim order, SDD §2).
_PRIORITY_RANK: dict[Priority, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Reasons the arcrun loop stamps on ``completion_payload.error`` when the loop
# ITSELF halts a run — the SPEC-043 breakers (max_turns / max_cost / max_tokens /
# runaway_loop / error_cascade, mirrored from
# ``arcrun.builtins.task_complete.BudgetBreachReason``) plus the operator-cancel
# code. On any of these the agent never reached its own terminator, so the run
# returns NORMALLY carrying a "failed" payload while the arcstore row is still
# ``in_progress``. That is a terminal failure, not a completed step — a cap is
# deterministic, so a retry would only re-burn the same ceiling.
_LOOP_HALT_REASONS = frozenset(
    {"max_turns", "max_cost", "max_tokens", "runaway_loop", "error_cascade", "cancelled"}
)


async def _is_dispatchable(st: _runtime._State, task: Task, now: str) -> bool:
    """Whether a todo task should be started now (P1 gates + P2 DAG gates).

    Skips a task whose retry backoff has not elapsed, whose dependencies aren't
    all done, or that has subtasks — a coordinator parent auto-completes from
    its children (reconcile), it never runs.
    """
    if not _backoff_elapsed(task, now):
        return False
    if not await st.store.deps_met(task):
        return False
    return not await st.store.children(task.id)


async def _dispatch_tick() -> None:
    """Run one poll-and-dispatch tick. Factored out so it is directly testable.

    No-op unless dispatch is enabled AND a run callback is bound. Respects the
    one-``in_progress``-task cap (never stacks a second run), skips tasks whose
    dependencies are unmet or whose retry backoff has not elapsed, picks the
    highest-priority ready task, starts it (todo -> in_progress), and runs it
    under the reliability wrapper (timeout + retry/dead-letter).
    """
    st = await _state()
    if not st.config.dispatch or st.agent_run_fn is None:
        return
    self_did = st.identity.did
    # Cap guard: if a task is already running for this agent, leave it be.
    if await st.store.list(status="in_progress", owner_did=self_did):
        return
    todos = await st.store.list(status="todo", owner_did=self_did)
    now = datetime.now(UTC).isoformat()
    ready = [t for t in todos if await _is_dispatchable(st, t, now)]
    if not ready:
        return
    ready.sort(key=lambda t: (_PRIORITY_RANK.get(t.priority, 99), t.created_at or ""))
    picked = ready[0]
    # Pin the run id up front and stamp it in the same atomic write that claims
    # the task, then hand the SAME id to the run so the loop's spooled events
    # share it — the arcui activity timeline joins task.run_id to those events.
    run_id = str(uuid.uuid4())
    started, _reason = await st.store.start_task(picked.id, self_did, run_id=run_id)
    if started is None or started.status != "in_progress":
        # Lost the atomic claim (a concurrent starter won) — try again next tick.
        return
    await _run_task(st, started, run_id, self_did)


async def _announce_task_moment(st: _runtime._State, task: Task) -> None:
    """Announce a ``task_start`` moment on the bus before the task run begins.

    Best-effort (SPEC-071): a Brain (arcmemory) may fire a proactive recall off
    this, but a moment emit must never break task dispatch — any failure is
    logged and swallowed. No-op when the agent has no bus (bare/test paths).
    """
    if st.bus is None:
        return
    try:
        prompt = _format_task_prompt(task)
        await st.bus.emit(
            "agent:moment",
            {
                "kind": "task_start",
                "cues": moment_cues(prompt),
                "text": prompt,
                "session_id": _session_key(task.id),
            },
        )
    except Exception:  # reason: fail-open — a moment emit must not break dispatch
        _logger.exception("task_start moment emit failed for task %s", task.id)


async def _run_task(st: _runtime._State, task: Task, run_id: str, self_did: str) -> None:
    """Drive one dispatched run under the reliability wrapper (P1).

    The run is a tracked ``asyncio.Task`` (so the watcher can cancel it) wrapped
    in an optional wall-clock timeout. A normal return leaves the task's status
    as the agent set it (done/failed via its own tools, or in_progress if it
    never completed — the stuck-reclaim path handles that later). A timeout or
    unhandled error is a failed attempt fed to the retry engine; an operator
    cancel (the watcher cancelled the run, recorded in ``st.cancelling``) is a
    terminal dead-letter — process shutdown re-raises instead.
    """
    await _announce_task_moment(st, task)
    timeout = _resolve_timeout(task, st.config)
    node = node_from_task(task)
    run_kwargs: dict[str, Any] = {}
    if node is not None:
        run_kwargs["allowed_strategies"] = allowed_strategies(node)
        # A node may pin where its human-facing notification goes. Threaded as
        # the turn's reply target, it becomes what ``notify_user`` delivers to —
        # so a cron run's summary lands on the pinned channel instead of falling
        # back to whatever chat the operator last used (SPEC-061 follow-up).
        if node.deliver_to:
            run_kwargs["reply_target"] = node.deliver_to
    # A workflow node runs in a FRESH session, which would reset the run's
    # trifecta accumulation and let a composition no single session could
    # complete be reached by splitting it across two nodes (COMP-015). Binding
    # the run's legs into this dispatch closes that: the ledger unions them into
    # every policy evaluation inside, and records back what this node lit.
    with _node_dispatch(st, node) as carrier:
        if node is not None and node.kind == "script" and node.script:
            # A script node is deterministic code, not a model turn: run its
            # bundle script directly instead of the loop (SPEC-061). It still
            # rides the same reliability wrapper, cancel path, and leg carrier.
            is_script = True
            run = asyncio.ensure_future(_run_script_node(st, task, node, self_did))
        elif node is not None and node.kind == "tool":
            is_script = True
            run = asyncio.ensure_future(_run_tool_node(st, task, node, self_did))
        else:
            is_script = False
            run = asyncio.ensure_future(
                st.agent_run_fn(
                    _format_task_prompt(task),
                    session_key=_session_key(task.id),
                    run_id=run_id,
                    **run_kwargs,
                )
            )
        st.running[task.id] = run
        try:
            result = await _await_run(st, task, run, timeout, self_did)
            # The model path returns a RunResult; a loop the breaker halted
            # returns NORMALLY with a "failed" payload, so the clean-return
            # branch above did nothing and the row is still in_progress. Convert
            # that into a terminal failure now instead of waiting for
            # stuck-reclaim to guess. A script node transitions its own row.
            if not is_script:
                await _settle_capped_run(st, task.id, self_did, result)
        finally:
            st.running.pop(task.id, None)
    if node is not None and carrier is not None:
        await _persist_run_legs(st, task, node, carrier.snapshot(), self_did)


@contextmanager
def _node_dispatch(st: _runtime._State, node: WorkflowNode | None) -> Iterator[CarriedLegs | None]:
    """Bind a workflow node's identity and carried legs for the duration of a run.

    Both are ContextVars, so the loop's tool dispatches inherit them: the node
    supplies the assemble-prompt section and the per-attempt idempotency key
    (REQ-242), the carrier supplies the run's accumulated trifecta legs.
    """
    if node is None:
        yield None
        return
    carrier = CarriedLegs(
        legs=set(node.accumulated_legs), max_legs=st.config.max_run_capability_legs
    )
    node_token = bind_node(node)
    legs_token = bind_carried_legs(carrier)
    try:
        yield carrier
    finally:
        reset_carried_legs(legs_token)
        reset_node(node_token)


async def _persist_run_legs(
    st: _runtime._State,
    task: Task,
    node: WorkflowNode,
    legs: frozenset[str],
    self_did: str,
) -> None:
    """Write this node's accumulated legs back onto the row for the runner.

    The task row is the handoff (D-538), so the accumulation travels the same
    durable path the work does: the runner reads it off the completed node and
    stamps it onto the next node it materialises. A message would be lossy; a
    row is not.

    The key is FLAT, alongside the runner's own metadata keys, because that is
    where both producers read it: ``node_from_task`` seeds this node from it and
    the runner unions it into the next node's stamp.
    """
    if set(legs) == set(node.accumulated_legs):
        return
    metadata = dict(task.metadata or {})
    metadata["accumulated_legs"] = sorted(legs)
    try:
        await st.store.update(task.id, {"metadata": metadata}, actor_did=self_did)
    except _TOOL_ERRORS:
        _logger.warning("Could not persist accumulated legs for node %s", node.node_id)


async def _seal_run_legs(st: _runtime._State, task: Task) -> None:
    """Land the run's carried legs BEFORE this node's row goes terminal.

    The runner stamps the next node from COMPLETED rows, so legs written only
    once the whole dispatch unwinds can be missed by a tick that lands in
    between: the next node would start under-charged, which is exactly the
    cross-session composition COMP-015 exists to make unreachable.
    """
    node = current_node()
    carrier = carried_legs()
    if node is None or carrier is None:
        return
    await _persist_run_legs(st, task, node, carrier.snapshot(), st.identity.did)


async def _await_run(
    st: _runtime._State, task: Task, run: asyncio.Task[Any], timeout: float | None, self_did: str
) -> Any:
    """Await one dispatched run, routing every outcome to the retry engine.

    Returns the run's result on a clean return (the model path's ``RunResult``,
    inspected by :func:`_settle_capped_run` for a breaker-halted loop) and
    ``None`` on the failure/cancel branches, which have already transitioned the
    task themselves.
    """
    try:
        return await asyncio.wait_for(run, timeout)
    except TimeoutError:
        await _handle_attempt_failure(st, task.id, self_did, f"timeout after {timeout:g}s")
    except asyncio.CancelledError:
        if task.id in st.cancelling:
            st.cancelling.discard(task.id)
            await st.store.dead_letter(
                task.id, actor_did=self_did, resolution="cancelled", last_error="cancelled"
            )
        else:
            raise  # genuine shutdown — never swallow the loop's own cancellation
    except Exception as exc:  # reason: any run failure feeds the retry engine (LLM10/ASI08)
        await _handle_attempt_failure(st, task.id, self_did, f"{type(exc).__name__}: {exc}")
    return None


async def _settle_capped_run(
    st: _runtime._State, task_id: str, self_did: str, result: Any
) -> None:
    """Terminally fail a task whose loop was halted by a budget/turn cap.

    A capped loop (max_cost / max_turns / max_tokens / runaway_loop /
    error_cascade / operator cancel) does NOT raise: the breaker synthesizes a
    ``completion_payload`` with ``status="failed"`` and ``error=<reason>``,
    emits ``loop.complete``, and returns — so the run reads as a clean finish
    even though the agent never called ``complete_task``/``fail_task``. Left
    alone the row sits ``in_progress`` (the dashboard shows "running") until
    stuck-reclaim guesses it dead minutes later, and a workflow that
    materialised it as a node never finalises because the node is still
    in-flight. The terminal signal is already in hand, so act on it now.

    Guarded on the row still being ``in_progress``: an agent that finished
    through its own tools wrote the terminal row first, and that decision wins
    (``dead_letter`` is itself status-conditional, so the guard is belt-and-
    braces against a race).
    """
    payload = getattr(result, "completion_payload", None)
    if not isinstance(payload, dict):
        return
    reason = payload.get("error")
    if not isinstance(reason, str) or reason not in _LOOP_HALT_REASONS:
        return
    current = await st.store.get(task_id)
    if current is None or current.status != "in_progress":
        return
    raw_summary = str(payload.get("summary") or f"loop halted: {reason}")
    summary = sanitize_text(raw_summary, max_length=500)
    failed = await st.store.dead_letter(
        task_id, actor_did=self_did, resolution=summary, last_error=f"loop halted: {reason}"
    )
    if failed is not None:
        await _notify_operator(
            st, f"failed: {current.title} ({reason})", current.classification, alert=True
        )


async def _handle_attempt_failure(
    st: _runtime._State, task_id: str, self_did: str, error: str
) -> None:
    """Retry (with exponential backoff) or dead-letter a failed attempt (P1).

    ``attempts`` was incremented at start, so it is the count of tries so far.
    Below the ceiling -> requeue to ``todo`` gated by an exponential backoff;
    at/above it -> terminal ``failed`` (dead letter). Both writes are status-
    conditional in the store, so a concurrent stuck-reclaim can't double-apply.
    """
    current = await st.store.get(task_id)
    if current is None:
        return
    error = sanitize_text(error, max_length=500)
    if current.attempts >= current.max_attempts:
        await st.store.dead_letter(
            task_id,
            actor_did=self_did,
            resolution=f"failed after {current.attempts} attempt(s) — retries exhausted",
            last_error=error,
        )
        await _notify_operator(
            st, f"dead-lettered: {current.title} ({error})", current.classification, alert=True
        )
        return
    backoff = st.config.retry_backoff_seconds * (2 ** (current.attempts - 1))
    next_at = (datetime.now(UTC) + timedelta(seconds=backoff)).isoformat()
    await st.store.requeue(task_id, actor_did=self_did, last_error=error, next_attempt_at=next_at)


async def _reliability_tick() -> None:
    """One cancel + stuck-reclaim pass over the agent's in_progress tasks (P1).

    For each in_progress task this agent owns: an operator cancel request stops
    the live run (or dead-letters it directly if no run is live); otherwise, a
    task with no live run is reclaimed as a failed attempt — immediately on the
    first pass (pre-restart orphans) or once past ``stuck_reclaim_seconds``
    thereafter (a run that ended without completing). A live, healthy run is
    left untouched.
    """
    st = await _state()
    if not st.config.dispatch:
        return
    self_did = st.identity.did
    in_progress = await st.store.list(status="in_progress", owner_did=self_did)
    now = datetime.now(UTC)
    first_pass = not st.reclaim_done
    for task in in_progress:
        if task.cancel_requested:
            await _cancel_running(st, task, self_did)
        elif _should_reclaim(st, task, now, first_pass):
            await _notify_operator(
                st,
                f"escalation — stuck task reclaimed: {task.title}",
                task.classification,
                alert=True,
            )
            await _handle_attempt_failure(
                st, task.id, self_did, "stuck: no active run — reclaimed"
            )
    st.reclaim_done = True
    await _reconcile_parents(st, self_did)
    await _route_unassigned(st, self_did)


async def _reconcile_parents(st: _runtime._State, self_did: str) -> None:
    """Roll a decomposition parent up from its children's terminal states (P2).

    A parent this agent owns that is still open and has subtasks auto-completes
    when every child is ``done``, and auto-fails the moment any child fails
    terminally (a failed subtask makes the parent unachievable — fail-fast,
    ASI08). Runs each reliability tick so multi-level DAGs settle bottom-up over
    successive passes. The transition is deterministic and audited (the store
    write's resolution records why).
    """
    # One list + in-memory grouping (no children() query per task). Children of
    # a decomposition are created owned by the same agent as the parent, so
    # grouping this agent's own tasks by parent_id sees the whole family;
    # cross-owner children (post-reassignment, a later phase) are out of scope.
    mine = await st.store.list(owner_did=self_did)
    children_by_parent: dict[str, list[Task]] = {}
    for task in mine:
        if task.parent_id:
            children_by_parent.setdefault(task.parent_id, []).append(task)
    for parent in mine:
        if parent.status in ("done", "failed"):
            continue
        children = children_by_parent.get(parent.id)
        if children:
            await _reconcile_one_parent(st, parent, children, self_did)


async def _reconcile_one_parent(
    st: _runtime._State, parent: Task, children: list[Task], self_did: str
) -> None:
    """Apply the roll-up rule to one open parent (fail-fast, else all-done)."""
    if any(c.status == "failed" for c in children):
        await st.store.finish(
            parent.id,
            status="failed",
            resolution="a subtask failed — parent cannot complete",
            actor_did=self_did,
        )
    elif all(c.status == "done" for c in children) and await st.store.deps_met(parent):
        # Require the parent's FULL dependency set, not just its children — a
        # parent that also carries non-child blocked_by deps waits for those too.
        await st.store.finish(
            parent.id,
            status="done",
            resolution="all subtasks complete",
            actor_did=self_did,
        )


# ---------------------------------------------------------------------------
# Auto-routing (SPEC-056 Phase 3) — ownerless tasks find the best agent
# ---------------------------------------------------------------------------


async def _route_unassigned(st: _runtime._State, self_did: str) -> None:
    """Route every ownerless task to the best eligible agent (P3).

    No-op without routing enabled or a live registry (the roster source).
    Selection is deterministic (see :func:`_pick_agent`) so concurrent routers
    on different agents converge; the store's ``route`` is owner-null-conditional
    so exactly one write lands. Load is tracked incrementally across the pass so
    a burst of tasks spreads rather than piling on the momentary least-loaded.
    """
    if not st.config.routing or st.registry is None:
        return
    unassigned = await st.store.unassigned()
    if not unassigned:
        return
    agents = await _eligible_agents(st)
    if not agents:
        return
    load = await _load_by_owner(st)
    for task in unassigned:
        chosen = _pick_agent(task, agents, load)
        routed = await st.store.route(task.id, chosen.did, self_did)
        if routed is not None:
            load[chosen.did] = load.get(chosen.did, 0) + 1
            await _notify_assignee(st, chosen.handle, routed)


async def _eligible_agents(st: _runtime._State) -> list[FleetMember]:
    """The routing candidate set. Who is eligible is the fleet layer's call."""
    return list(await st.registry.list_agents())


async def _load_by_owner(st: _runtime._State) -> dict[str, int]:
    """Current in-flight load per agent: count of todo + in_progress tasks."""
    load: dict[str, int] = {}
    for status in ("todo", "in_progress"):
        for task in await st.store.list(status=status):
            if task.owner_did:
                load[task.owner_did] = load.get(task.owner_did, 0) + 1
    return load


def _should_reclaim(st: _runtime._State, task: Task, now: datetime, first_pass: bool) -> bool:
    """Whether an in_progress task with no live run should be reclaimed now.

    Never reclaim a task with a live run. On the first pass reclaim any orphan
    (a pre-restart in_progress task); thereafter only once it is past the
    staleness threshold (a run that ended without ever completing).
    """
    if task.id in st.running:
        return False
    return first_pass or _is_stale(task, now, st.config.stuck_reclaim_seconds)


async def _cancel_running(st: _runtime._State, task: Task, self_did: str) -> None:
    """Honor a cancel request: stop the live run, or dead-letter if none.

    A live run is cancelled via its tracked handle; ``st.cancelling`` marks the
    stop as deliberate so :func:`_run_task` finalizes it as cancelled (not a
    shutdown re-raise). With no live run (e.g. the process restarted after the
    request), finalize the task directly.
    """
    run = st.running.get(task.id)
    if run is not None:
        st.cancelling.add(task.id)
        run.cancel()
        return
    await st.store.dead_letter(
        task.id, actor_did=self_did, resolution="cancelled", last_error="cancelled"
    )


@hook(event="agent:ready", priority=100)
async def tasks_bind_run_fn(ctx: Any) -> None:
    """Bind the agent's run callback for the dispatch loop (mirrors messaging).

    ``run_fn`` (``ArcAgent.run_collected``) is delivered on the ``agent:ready``
    payload; the dispatch loop needs it to actually run an assigned task.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    st = _runtime.state()
    run_fn = data.get("run_fn")
    if run_fn is not None:
        st.agent_run_fn = run_fn
    # Both are needed by the workflow node adapter (COMP-014/015) and arrive on
    # the same payload: the ledger carries a run's accumulated trifecta legs into
    # each node's fresh session, the registry activates a node's declared skill.
    st.capability_ledger = data.get("capability_ledger") or st.capability_ledger
    st.skill_registry = data.get("skill_registry") or st.skill_registry


@hook(event="agent:assemble_prompt", priority=60)
async def inject_team_handoff_section(ctx: Any) -> None:
    """Teach the agent to hand work to the teammate who owns it.

    Present ONLY because the tasks module is loaded — the loader subscribes this
    hook when it registers ``assign_task``/``create_task``, so a headless agent
    without the module is never told to call tools it does not have. The messaging
    module contributes the companion channel/DM/mention guidance for its own
    tools; the two seams never name each other's tools.
    """
    sections = ctx.data.get("sections") if hasattr(ctx, "data") else None
    if not isinstance(sections, dict):
        return
    sections["handoffs"] = "\n".join(
        [
            "## Team Handoffs",
            "",
            "Hand work to the teammate who owns it. Do not do everything yourself.",
            "",
            "- Do it yourself when it is quick and clearly your job.",
            "- Hand off when the job belongs to someone else or needs their skill.",
            "- Give an at-rest task to a teammate: "
            '`assign_task(id, to_handle="@handle")`. They pick it up and run it.',
            '- Make new work owned by a teammate: `create_task(title=..., owner="@handle")`.',
            "- Use the same `@handle` you would tag in a channel; it resolves to that agent.",
            "- After you hand off, let them run it. Ask in the channel if you need a status.",
        ]
    )


@hook(event="agent:assemble_prompt", priority=60)
async def inject_workflow_node_section(ctx: Any) -> None:
    """Inject the running node's instructions and upstream outputs (REQ-233/239).

    The assemble-prompt sections seam is the ONLY way node content reaches the
    model: context mutation is a single discrete path (compaction owns it) and a
    second writer breaks it. Upstream values are bound as typed JSON under a
    labelled section, never interpolated into a command or spliced into prose.
    """
    sections = ctx.data.get("sections") if hasattr(ctx, "data") else None
    if not isinstance(sections, dict):
        return
    node = current_node()
    if node is None:
        return
    st = _runtime.state()
    # The SAME resolution the completion gate uses (``_node_completion_refusal``
    # -> ``resolve_schema``). One source, so the shape the model is shown and
    # the shape it is judged against cannot drift — they were drifting, and a
    # node that did its work correctly failed a gate it was never told about.
    schema = resolve_schema(node, _bundle_root(st, node))
    sections["workflow_node"] = render_node_section(
        node,
        _skill_body(node),
        _node_instructions(st, node),
        schema=schema if isinstance(schema, dict) else None,
    )


def _node_instructions(st: _runtime._State, node: WorkflowNode) -> str:
    """The node's prompt file content, read out of the verified bundle.

    The runner stamps a bundle-relative REFERENCE, not the bytes, so the bytes
    are only trustworthy read from the bundle whose manifest was verified — and
    only through ``_confined``, because a reference is a caller-controlled name
    becoming a path. Unreadable degrades to no instructions: the node still runs
    under its schema and artifact gates, which is where correctness is enforced.
    """
    root = _bundle_root(st, node)
    if node.prompt_ref is None or root is None:
        return ""
    target = _confined(root, node.prompt_ref)
    if target is None:
        _logger.warning("Node %s prompt %r escapes its bundle", node.node_id, node.prompt_ref)
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        _logger.warning("Node %s could not read prompt %r", node.node_id, node.prompt_ref)
        return ""


def _skill_body(node: WorkflowNode) -> str | None:
    """Load a node's declared skill body from the capability registry.

    Deterministic activation (REQ-233): the body is read from the registry entry
    and placed in the prompt as part of dispatching the node — the model is never
    asked to call ``use_skill`` first, because a node that only *sometimes*
    activates its skill does not run the same way twice.
    """
    if node.skill is None:
        return None
    try:
        registry = _runtime.state().skill_registry
    except RuntimeError:
        return None
    entry = getattr(registry, "_skills", {}).get(node.skill) if registry is not None else None
    location = getattr(entry, "location", None)
    if location is None:
        _logger.warning("Node %s declares unknown skill %r", node.node_id, node.skill)
        return None
    try:
        return str(Path(location).read_text(encoding="utf-8"))
    except OSError:
        _logger.warning("Node %s could not read skill %r", node.node_id, node.skill)
        return None


@background_task(name="tasks_dispatch_loop", interval=_DISPATCH_TICK)
async def tasks_dispatch_loop(_ctx: Any) -> None:
    """Background loop: pull and run the agent's ready assigned tasks.

    The loader spawns this once and it owns its own cadence (mirrors the memory
    consolidate + skills curator loops — ``register_task`` calls ``fn(None)`` a
    single time, so the ``while True`` MUST live here or the loop runs one tick
    and dies). Gated by ``config.dispatch`` inside :func:`_dispatch_tick`. The
    awaited run inside each tick keeps dispatch serial — a tick does not return
    until the current task's run does — so the in-progress cap holds without
    extra locking.
    """
    while True:
        try:
            await _dispatch_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a tick error must never crash the agent
            _logger.warning("tasks dispatch tick failed", exc_info=True)
        await asyncio.sleep(_DISPATCH_TICK)


@background_task(name="tasks_reliability_watcher", interval=_RELIABILITY_TICK)
async def tasks_reliability_watcher(_ctx: Any) -> None:
    """Background loop: honor operator cancels and reclaim stuck runs.

    Owns its own cadence (see :func:`tasks_dispatch_loop`). Runs concurrently
    with (and faster than) the dispatch loop so an operator "stop" reaches a run
    while the dispatch tick is still awaiting it, and so a task orphaned
    in_progress by a crash/restart is recovered rather than stranded. Gated by
    ``config.dispatch`` inside :func:`_reliability_tick`.
    """
    while True:
        try:
            await _reliability_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a tick error must never crash the agent
            _logger.warning("tasks reliability tick failed", exc_info=True)
        await asyncio.sleep(_RELIABILITY_TICK)


@hook(event="agent:shutdown", priority=100)
async def tasks_shutdown(_ctx: Any) -> None:
    """Close the module-owned ArcStore backend after task work drains."""
    await _runtime.close_store()


__all__ = [
    "assign_task",
    "claim_task",
    "complete_task",
    "create_task",
    "decompose_task",
    "fail_task",
    "inject_workflow_node_section",
    "list_tasks",
    "set_task_output",
    "start_task",
    "tasks_bind_run_fn",
    "tasks_dispatch_loop",
    "tasks_reliability_watcher",
    "tasks_shutdown",
    "update_task",
]
