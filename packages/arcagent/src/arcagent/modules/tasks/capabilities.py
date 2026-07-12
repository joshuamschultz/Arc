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

import json
import logging
import sqlite3
import uuid
from typing import Any

from arcteam.registry import resolve
from arcteam.types import Message, MsgType

from arcagent.modules.tasks import _runtime
from arcagent.modules.tasks.models import Priority, Task
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.sanitizer import sanitize_text

# A SQLite lock-timeout under shared-db contention surfaces as
# ``sqlite3.OperationalError`` from deep in the store; catch it alongside the
# validation errors so a tool degrades to a clean ``{"error"}`` (REL-F3b).
_TOOL_ERRORS = (ValueError, TypeError, sqlite3.OperationalError)

_logger = logging.getLogger("arcagent.modules.tasks.capabilities")


async def _state() -> _runtime._State:
    """Fetch runtime state, finishing the module's lazy async wiring first.

    ``_runtime.configure()`` is sync (mirrors every other module — the
    dispatcher calls it without ``await``), so the SQLite backend and, when
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
    ``did:...``) resolved via arcteam — unavailable if no registry could be
    injected or built live (SDD §3).
    """
    if owner is None:
        self_did: str = st.identity.did
        return self_did
    if owner == "":
        return None
    if st.registry is None:
        msg = "registry unavailable"
        raise ValueError(msg)
    return await resolve(st.registry, owner)


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
        task = Task(
            id=_new_task_id(),
            title=title,
            description=description,
            priority=priority,  # type: ignore[arg-type] # validated by Task's Literal on construction
            owner_did=owner_did,
            creator_did=st.identity.did,
            blocked_by=blocked_by or [],
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
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        if not await st.store.deps_met(current):
            return json.dumps(
                {"error": f"Task '{id}' is blocked by unfinished dependencies"}
            )
        patch: dict[str, Any] = {"status": "done", "resolution": resolution}
        if output is not None:
            patch["output"] = output
        updated = await st.store.update(id, patch, actor_did=st.identity.did)
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
        updated = await st.store.update(
            id, {"status": "failed", "resolution": resolution}, actor_did=st.identity.did
        )
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
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
        return json.dumps({"error": "registry unavailable"})
    try:
        to_did = await resolve(st.registry, to_handle)
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
    message = Message(
        sender=st.identity.did,
        to=[f"agent://{handle}"],
        msg_type=MsgType.TASK_ASSIGNED,
        body=f"@{handle} task_id={task.id} — {task.title}",
        classification=task.classification,
    )
    try:
        await st.messenger.send(message)
    except Exception:
        _logger.warning("failed to notify @%s of assignment for task '%s'", handle, task.id)


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
        created_subs = [await st.store.create(sub) for sub in subs]
        sub_ids = [s.id for s in created_subs]
        updated_parent = await st.store.update(
            id, {"blocked_by": [*parent.blocked_by, *sub_ids]}, actor_did=st.identity.did
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
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        updated = await st.store.update(id, {"output": output or {}}, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Dispatch loop (SPEC-056 Phase D) — assigned tasks actually run
# ---------------------------------------------------------------------------

# Poll cadence for the dispatch loop. A fixed decorator arg (interval cannot
# read per-agent config at decoration time); the real on/off switch is
# ``config.dispatch``, checked inside each tick. 15s is responsive enough for
# an operator assigning work from the board without hammering the shared DB.
_DISPATCH_TICK = 15.0

# Session key prefix for a dispatched task's run — one transcript per task so
# the board and the session log line up (``<workspace>/sessions/task:<id>.jsonl``).
_TASK_SESSION = "task"

# Highest-priority-first ordering (mirrors arcstore's claim order, SDD §2).
_PRIORITY_RANK: dict[Priority, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _format_task_prompt(task: Task) -> str:
    """Render the run prompt handed to the agent for an assigned task.

    Title/description are re-sanitized before prompt interpolation (LLM01/
    ASI06 defence-in-depth) even though the arcstore ``Task`` validator already
    scrubbed them at write time — the prompt is an instruction surface.
    """
    title = sanitize_text(task.title, max_length=500)
    lines = [
        f"You have been assigned task {task.id}.",
        f"Title: {title}",
    ]
    if task.description:
        lines.append(f"Details: {sanitize_text(task.description, max_length=4000)}")
    lines.append(f"Priority: {task.priority}")
    lines.append(
        "Do the work now. When finished, call complete_task with a short "
        "resolution — or fail_task if you cannot complete it. Work silently; "
        "only notify the user for a meaningful result, a question, or a blocker."
    )
    return "\n".join(lines)


async def _dispatch_tick() -> None:
    """Run one poll-and-dispatch tick. Factored out so it is directly testable.

    No-op unless dispatch is enabled AND a run callback is bound. Respects the
    one-``in_progress``-task cap (never stacks a second run) and skips tasks
    whose dependencies are unmet. Picks the highest-priority ready task, starts
    it (todo -> in_progress), and invokes the agent's run callback for it.
    """
    st = await _state()
    if not st.config.dispatch or st.agent_run_fn is None:
        return
    self_did = st.identity.did
    # Cap guard: if a task is already running for this agent, leave it be.
    if await st.store.list(status="in_progress", owner_did=self_did):
        return
    todos = await st.store.list(status="todo", owner_did=self_did)
    ready = [t for t in todos if await st.store.deps_met(t)]
    if not ready:
        return
    ready.sort(key=lambda t: (_PRIORITY_RANK.get(t.priority, 99), t.created_at or ""))
    picked = ready[0]
    started, _reason = await st.store.start_task(picked.id, self_did)
    if started is None or started.status != "in_progress":
        # Lost the atomic claim (a concurrent starter won) — try again next tick.
        return
    await st.agent_run_fn(
        _format_task_prompt(started), session_key=f"{_TASK_SESSION}:{started.id}"
    )


@hook(event="agent:ready", priority=100)
async def tasks_bind_run_fn(ctx: Any) -> None:
    """Bind the agent's run callback for the dispatch loop (mirrors messaging).

    ``run_fn`` (``ArcAgent.run_collected``) is delivered on the ``agent:ready``
    payload; the dispatch loop needs it to actually run an assigned task.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    run_fn = data.get("run_fn")
    if run_fn is not None:
        _runtime.state().agent_run_fn = run_fn


@background_task(name="tasks_dispatch_loop", interval=_DISPATCH_TICK)
async def tasks_dispatch_loop(_ctx: Any) -> None:
    """Background loop: pull and run the agent's ready assigned tasks.

    Gated by ``config.dispatch`` inside :func:`_dispatch_tick`. The awaited run
    inside each tick keeps dispatch serial — the loop does not tick again until
    the current task's run returns — so the in-progress cap holds without extra
    locking.
    """
    await _dispatch_tick()


__all__ = [
    "assign_task",
    "claim_task",
    "complete_task",
    "create_task",
    "decompose_task",
    "fail_task",
    "list_tasks",
    "set_task_output",
    "start_task",
    "tasks_bind_run_fn",
    "tasks_dispatch_loop",
    "update_task",
]
