"""LLM-callable tools for the planning module.

Personal task notebook — create, list, update, and complete tasks.
Tasks persist in workspace/tasks.json across agent turns.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, cast

from arcagent.core.tool_registry import RegisteredTool, native_tool

_logger = logging.getLogger("arcagent.planning.tools")


def _load_tasks(tasks_path: Path) -> list[dict[str, Any]]:
    """Read tasks from workspace file."""
    if not tasks_path.exists():
        return []
    try:
        return cast(list[dict[str, Any]], json.loads(tasks_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return []


def _save_tasks(tasks_path: Path, tasks: list[dict[str, Any]]) -> None:
    """Persist tasks to workspace file."""
    tasks_path.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _find_task(
    tasks: list[dict[str, Any]],
    task_id: str,
) -> dict[str, Any] | None:
    """Find a task by ID in the task list."""
    return next((t for t in tasks if t["id"] == task_id), None)


def create_planning_tools(workspace: Path) -> list[RegisteredTool]:
    """Create task management tools for the LLM to call."""

    tasks_path = workspace / "tasks.json"

    @native_tool(
        description=(
            "Create a new task in your notebook. Use this to plan "
            "multi-step work that persists across turns."
        ),
        source="planning",
        timeout_seconds=10,
        params={
            "description": "What needs to be done",
        },
        required=["description"],
    )
    async def task_create(
        description: str = "",
        **kwargs: Any,
    ) -> str:
        if not description:
            return json.dumps({"error": "description is required"})

        tasks = _load_tasks(tasks_path)
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task: dict[str, Any] = {
            "id": task_id,
            "description": description,
            "status": "pending",
        }
        tasks.append(task)
        _save_tasks(tasks_path, tasks)

        _logger.info("Created task %s: %s", task_id, description)
        return json.dumps(task)

    @native_tool(
        description=(
            "List your current tasks. Optionally filter by status "
            "(pending, in_progress, waiting, done)."
        ),
        source="planning",
        timeout_seconds=10,
        params={
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "waiting", "done"],
                "description": "Filter by status",
            },
        },
    )
    async def task_list(
        status: str = "",
        **kwargs: Any,
    ) -> str:
        tasks = _load_tasks(tasks_path)
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        return json.dumps({"tasks": tasks, "total": len(tasks)})

    @native_tool(
        description=(
            "Update a task's status or description. Use status values: "
            "pending, in_progress, waiting, done."
        ),
        source="planning",
        timeout_seconds=10,
        params={
            "id": "Task ID to update",
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "waiting", "done"],
                "description": "New status",
            },
            "description": "Updated description",
        },
        required=["id"],
    )
    async def task_update(
        id: str = "",  # noqa: A002 - matches JSON schema field name
        status: str = "",
        description: str = "",
        **kwargs: Any,
    ) -> str:
        if not id:
            return json.dumps({"error": "id is required"})

        tasks = _load_tasks(tasks_path)
        target = _find_task(tasks, id)
        if target is None:
            return json.dumps({"error": f"Task not found: {id}"})

        if status:
            target["status"] = status
        if description:
            target["description"] = description

        _save_tasks(tasks_path, tasks)
        _logger.info("Updated task %s: status=%s", id, target["status"])
        return json.dumps(target)

    @native_tool(
        description=("Mark a task as done and record the result summary."),
        source="planning",
        timeout_seconds=10,
        params={
            "id": "Task ID to complete",
            "result": "Summary of what was accomplished",
        },
        required=["id", "result"],
    )
    async def task_complete(
        id: str = "",  # noqa: A002 - matches JSON schema field name
        result: str = "",
        **kwargs: Any,
    ) -> str:
        if not id:
            return json.dumps({"error": "id is required"})

        tasks = _load_tasks(tasks_path)
        target = _find_task(tasks, id)
        if target is None:
            return json.dumps({"error": f"Task not found: {id}"})

        target["status"] = "done"
        target["result"] = result
        _save_tasks(tasks_path, tasks)

        _logger.info("Task %s completed", id)
        return json.dumps(
            {
                "id": id,
                "status": "done",
                "result": result,
            }
        )

    return [
        task_create.tool,
        task_list.tool,
        task_update.tool,
        task_complete.tool,
    ]
