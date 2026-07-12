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
:func:`_require_owner`, checked against ``_runtime.state().identity`` before
every state transition (create is exempt — there is no prior owner to
protect; assign is exempt — reassignment is not the owner's call, SDD §3).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from arcteam.registry import resolve

from arcagent.modules.tasks import _runtime
from arcagent.modules.tasks.models import Task, validate_task_text
from arcagent.tools._decorator import tool

_logger = logging.getLogger("arcagent.modules.tasks.capabilities")


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


async def _resolve_owner(owner: str | None, st: Any) -> str | None:
    """Resolve the ``owner`` tool argument to a DID.

    ``None`` (argument omitted) defaults to self; ``""`` leaves the task
    unowned (backlog); anything else is an address ref (``@handle``,
    ``did:...``) resolved via arcteam.
    """
    if owner is None:
        self_did: str = st.identity.did
        return self_did
    if owner == "":
        return None
    return await resolve(st.registry, owner)


def _require_owner(task: Task, st: Any) -> None:
    """Raise ``ValueError`` unless ``task`` is unowned or owned by this agent.

    Applied before every mutation except create (no prior owner to protect)
    and assign (reassignment is not the owner's call — SDD §3/§7).
    """
    if task.owner_did is not None and task.owner_did != st.identity.did:
        msg = f"Task '{task.id}' is owned by another agent"
        raise ValueError(msg)


# Claim ordering, mirroring arcstore.tasks._PRIORITY_ORDER (SDD §2).
_PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


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
    st = _runtime.state()
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
        return created.model_dump_json()
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
    st = _runtime.state()
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
        return updated.model_dump_json()
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="start_task",
    description="Start an owned (or unowned) task; unowned tasks are claimed by self",
    classification="state_modifying",
)
async def start_task(id: str = "") -> str:  # noqa: A002 - matches JSON schema field name
    st = _runtime.state()
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
    st = _runtime.state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        if resolution:
            validate_task_text(resolution)
        patch: dict[str, Any] = {"status": "done", "resolution": resolution}
        if output is not None:
            patch["output"] = output
        updated = await st.store.update(id, patch, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return updated.model_dump_json()
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
    st = _runtime.state()
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
        return updated.model_dump_json()
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
    st = _runtime.state()
    try:
        to_did = await resolve(st.registry, to_handle)
        updated = await st.store.assign(id, to_did, st.identity.did)
        if updated is None:
            return json.dumps({"error": f"unable to assign task '{id}'"})
        return updated.model_dump_json()
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="claim_task",
    description="Pull the next available task for self, respecting the in-progress cap",
    classification="state_modifying",
)
async def claim_task() -> str:
    st = _runtime.state()
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
    st = _runtime.state()
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
    st = _runtime.state()
    try:
        parent = await st.store.get(id)
        if parent is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(parent, st)
        created_subs: list[Task] = []
        for spec in subtasks or []:
            sub_title = spec.get("title", "")
            validate_task_text(sub_title)
            sub_description = spec.get("description", "")
            if sub_description:
                validate_task_text(sub_description)
            sub = Task(
                id=_new_task_id(),
                title=sub_title,
                description=sub_description,
                priority=spec.get("priority", "medium"),
                owner_did=st.identity.did,
                creator_did=st.identity.did,
                parent_id=id,
            )
            created_subs.append(await st.store.create(sub))
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
    st = _runtime.state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        updated = await st.store.update(id, {"output": output or {}}, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return updated.model_dump_json()
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
