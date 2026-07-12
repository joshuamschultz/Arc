"""Assignee-side handler for a delivered ``TASK_ASSIGNED`` envelope (SDD §5).

``assign_task`` (``capabilities.py``) sends the notification; this module is
the other end — the assignee's messaging dispatch calls
:func:`handle_task_assigned` on delivery, which adopts the named task via the
existing ``start_task`` tool. Kept separate from ``capabilities.py`` because
it is a message handler, not an LLM-facing tool (no ``@tool`` stamp).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from arcagent.modules.tasks.capabilities import start_task

if TYPE_CHECKING:
    from arcteam.types import Message

_TASK_ID_RE = re.compile(r"task_id=(\S+)")


def _extract_task_id(body: str) -> str | None:
    """Pull the ``task_id=`` marker out of a notification body, or ``None``.

    Deliberately not "the token nearest an @mention" — a body can carry
    several mentions (e.g. a cc'd sender plus the assignee), and only the
    explicit marker unambiguously names the task.
    """
    match = _TASK_ID_RE.search(body)
    return match.group(1) if match else None


async def handle_task_assigned(message: Message) -> str:
    """Adopt the task named in a ``TASK_ASSIGNED`` envelope via ``start_task``.

    Idempotent on redelivery: ``start_task`` treats a task already owned and
    in_progress under this agent as ``continue_current`` rather than an
    error (arcstore ``TaskStore.start_task``), so a durable-consumer replay
    of the same envelope is a no-op, not a failure.
    """
    task_id = _extract_task_id(message.body)
    if task_id is None:
        return json.dumps({"error": "no task_id marker in message body"})
    return str(await start_task(id=task_id))


__all__ = ["handle_task_assigned"]
