"""Pure task-dispatch policy helpers extracted from the capability surface."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from arcteam.types import Entity

from arcagent.modules.tasks.models import Task
from arcagent.utils.sanitizer import sanitize_text

_TASK_SESSION = "task"
_SAFE_SESSION_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")


def format_task_prompt(task: Task) -> str:
    title = sanitize_text(task.title, max_length=500)
    lines = [f"You have been assigned task {task.id}.", f"Title: {title}"]
    if task.description:
        lines.append(f"Details: {sanitize_text(task.description, max_length=4000)}")
    lines.append(f"Priority: {task.priority}")
    lines.append(
        "Do the work now. When finished, call complete_task with a short "
        "resolution — or fail_task if you cannot complete it. Work silently; "
        "only notify the user for a meaningful result, a question, or a blocker."
    )
    return "\n".join(lines)


def resolve_timeout(task: Task, config: Any) -> float | None:
    timeout = task.timeout_seconds if task.timeout_seconds else config.task_timeout_seconds
    return timeout if timeout and timeout > 0 else None


def backoff_elapsed(task: Task, now: str) -> bool:
    return task.next_attempt_at is None or task.next_attempt_at <= now


def session_key(task_id: str) -> str:
    if _SAFE_SESSION_KEY.match(task_id):
        return f"{_TASK_SESSION}:{task_id}"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    flattened = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-")
    return f"{_TASK_SESSION}:{flattened}-{digest}"


def is_stale(task: Task, now: datetime, threshold: float) -> bool:
    if not task.started_at:
        return True
    try:
        started = datetime.fromisoformat(task.started_at)
    except ValueError:
        return True
    return (now - started).total_seconds() >= threshold


def pick_agent(task: Task, agents: list[Entity], load: dict[str, int]) -> Entity:
    def rank(agent: Entity) -> tuple[int, int, str]:
        matches = bool(set(task.tags) & set(agent.capabilities))
        return (0 if matches else 1, load.get(agent.did, 0), agent.name)

    return min(agents, key=rank)


__all__ = [
    "backoff_elapsed",
    "format_task_prompt",
    "is_stale",
    "pick_agent",
    "resolve_timeout",
    "session_key",
]
