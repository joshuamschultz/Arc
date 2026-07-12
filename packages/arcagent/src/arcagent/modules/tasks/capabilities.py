"""Decorator-form tasks module — SPEC-056 Phase B.

Ten module-level ``@tool`` functions expose the mission-control task
surface over the arcstore-backed ``TaskStore`` (Phase A). There is no
lifecycle engine here (unlike the scheduler template's ``SchedulerEngine``)
— tasks have no background loop, so there is no ``@capability`` class.

Audit is emitted CENTRALLY by the tool registry, keyed on each tool's
declared ``classification`` (SDD §3, deepen correction) — tools declare
classification, they never call ``arctrust.audit.emit`` themselves. Free
text (title/description/resolution) is sanitized via
:func:`arcagent.modules.tasks.models.validate_task_text` before it ever
reaches the store (LLM01/ASI06). Owner-only mutation is gated by
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
import uuid
from typing import Any

from arcteam.registry import resolve
from arcteam.types import Message, MsgType

from arcagent.modules.tasks import _runtime
from arcagent.modules.tasks.models import Task, validate_task_text
from arcagent.tools._decorator import tool

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
        validate_task_text(title)
        if description:
            validate_task_text(description)
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
    except (ValueError, TypeError) as exc:
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
        if "title" in updates:
            validate_task_text(updates["title"])
        if "description" in updates:
            validate_task_text(updates["description"])
        updated = await st.store.update(id, updates, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except (ValueError, TypeError) as exc:
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
    except (ValueError, TypeError) as exc:
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
        if resolution:
            validate_task_text(resolution)
        patch: dict[str, Any] = {"status": "done", "resolution": resolution}
        if output is not None:
            patch["output"] = output
        updated = await st.store.update(id, patch, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except (ValueError, TypeError) as exc:
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
        if resolution:
            validate_task_text(resolution)
        updated = await st.store.update(
            id, {"status": "failed", "resolution": resolution}, actor_did=st.identity.did
        )
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except (ValueError, TypeError) as exc:
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
    except (ValueError, TypeError) as exc:
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
    message = Message(
        sender=st.identity.did,
        to=[f"agent://{handle}"],
        msg_type=MsgType.TASK_ASSIGNED,
        body=f"@{handle} task_id={task.id} — {task.title}",
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
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="list_tasks",
    description="List tasks scoped to self or the whole team, optionally filtered by status",
    classification="read_only",
)
async def list_tasks(scope: str = "team", status: str | None = None) -> str:
    st = await _state()
    owner_did = st.identity.did if scope == "self" else None
    tasks = await st.store.list(status=status, owner_did=owner_did)
    return json.dumps([t.model_dump(mode="json") for t in tasks])


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
        # Validate + build ALL subtasks before persisting any, so a bad title in
        # a later subtask can't leave earlier ones orphaned (no partial write).
        subs: list[Task] = []
        for spec in subtasks or []:
            sub_title = spec.get("title", "")
            validate_task_text(sub_title)
            sub_description = spec.get("description", "")
            if sub_description:
                validate_task_text(sub_description)
            subs.append(
                Task(
                    id=_new_task_id(),
                    title=sub_title,
                    description=sub_description,
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
    except (ValueError, TypeError) as exc:
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
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


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
    "update_task",
]
