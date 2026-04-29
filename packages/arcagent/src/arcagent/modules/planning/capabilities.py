"""Decorator-form planning module — SPEC-021.

Two ``@hook`` functions mirror :class:`PlanningModule`'s ``startup``
registrations:

  * ``agent:assemble_prompt`` (priority 60) — inject pending tasks.
  * ``agent:shutdown``        (priority 100) — log module stop.

Four ``@tool`` functions expose the task notebook to the LLM:

  * ``task_create``   — create a new task.
  * ``task_list``     — list tasks, optionally filtered by status.
  * ``task_update``   — update a task's status or description.
  * ``task_complete`` — mark a task done and record the result.

State is shared via :mod:`arcagent.modules.planning._runtime`. The
agent configures it once at startup; hooks and tools read it lazily.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from arcagent.modules.planning import _runtime
from arcagent.tools._decorator import hook, tool

_logger = logging.getLogger("arcagent.modules.planning.capabilities")


# --- Helpers ---------------------------------------------------------------


def _load_tasks() -> list[dict[str, Any]]:
    """Read tasks from workspace file."""
    tasks_path = _runtime.state().tasks_path
    if not tasks_path.exists():
        return []
    try:
        return json.loads(tasks_path.read_text(encoding="utf-8"))  # type: ignore[return-value]
    except (json.JSONDecodeError, OSError):
        return []


def _save_tasks(tasks: list[dict[str, Any]]) -> None:
    """Persist tasks to workspace file."""
    tasks_path = _runtime.state().tasks_path
    tasks_path.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _find_task(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    """Find a task by ID. Returns None if not found."""
    return next((t for t in tasks if t["id"] == task_id), None)


# --- Hooks -----------------------------------------------------------------


@hook(event="agent:assemble_prompt", priority=60)
async def inject_planning_section(ctx: Any) -> None:
    """Inject pending tasks into prompt sections."""
    sections = ctx.data.get("sections")
    if sections is None or not isinstance(sections, dict):
        return

    pending = [t for t in _load_tasks() if t.get("status") != "done"]
    if not pending:
        return

    lines = [f"## Pending Tasks ({len(pending)} incomplete)", ""]
    for task in pending:
        status = task.get("status", "pending")
        lines.append(
            f"- **[{status}]** `{task.get('id', '?')}`: "
            f"{task.get('description', '(no description)')}"
        )

    sections["planning"] = "\n".join(lines)


@hook(event="agent:shutdown", priority=100)
async def planning_shutdown(ctx: Any) -> None:
    """Log planning module stop on agent shutdown."""
    del ctx  # event payload unused
    _logger.info("Planning module stopped")


# --- Tools -----------------------------------------------------------------


@tool(
    name="task_create",
    description=(
        "Create a new task in your notebook. Use this to plan "
        "multi-step work that persists across turns."
    ),
    classification="state_modifying",
    capability_tags=("planning",),
    when_to_use="When starting multi-step work that spans multiple turns.",
)
async def task_create(description: str) -> str:
    """Create a task and return the new task record as JSON."""
    if not description:
        return json.dumps({"error": "description is required"})

    tasks = _load_tasks()
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    task: dict[str, Any] = {
        "id": task_id,
        "description": description,
        "status": "pending",
    }
    tasks.append(task)
    _save_tasks(tasks)

    _logger.info("Created task %s: %s", task_id, description)
    return json.dumps(task)


@tool(
    name="task_list",
    description=(
        "List your current tasks. Optionally filter by status "
        "(pending, in_progress, waiting, done)."
    ),
    classification="read_only",
    capability_tags=("planning",),
    when_to_use="When you want to review what's pending or in progress.",
)
async def task_list(status: str | None = None) -> str:
    """Return all tasks (or tasks matching ``status``) as JSON."""
    tasks = _load_tasks()
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    return json.dumps({"tasks": tasks, "total": len(tasks)})


@tool(
    name="task_update",
    description=(
        "Update a task's status or description. Use status values: "
        "pending, in_progress, waiting, done."
    ),
    classification="state_modifying",
    capability_tags=("planning",),
    when_to_use="When a task's state or description needs to change.",
)
async def task_update(
    id: str,  # noqa: A002 - matches the established JSON schema field name
    status: str | None = None,
    description: str | None = None,
) -> str:
    """Update fields on a task by ID and return the updated record."""
    tasks = _load_tasks()
    target = _find_task(tasks, id)
    if target is None:
        return json.dumps({"error": f"Task not found: {id}"})

    if status:
        target["status"] = status
    if description:
        target["description"] = description

    _save_tasks(tasks)
    _logger.info("Updated task %s: status=%s", id, target["status"])
    return json.dumps(target)


@tool(
    name="task_complete",
    description="Mark a task as done and record the result summary.",
    classification="state_modifying",
    capability_tags=("planning",),
    when_to_use="When a task has been finished and you want to record the outcome.",
)
async def task_complete(id: str, result: str) -> str:  # noqa: A002
    """Set task status to ``done``, store ``result``, return confirmation."""
    tasks = _load_tasks()
    target = _find_task(tasks, id)
    if target is None:
        return json.dumps({"error": f"Task not found: {id}"})

    target["status"] = "done"
    target["result"] = result
    _save_tasks(tasks)

    _logger.info("Task %s completed", id)
    return json.dumps({"id": id, "status": "done", "result": result})


__all__ = [
    "inject_planning_section",
    "planning_shutdown",
    "task_complete",
    "task_create",
    "task_list",
    "task_update",
]
