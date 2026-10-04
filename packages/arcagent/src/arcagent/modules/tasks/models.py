"""Task model re-exports for the ``tasks`` module.

``Task``, ``TaskStatus``, ``Priority`` are the arcstore-owned durable model
(SPEC-056 Phase A) — re-exported here so tool code only ever imports from
this module, mirroring the scheduler template's ``models.py``. Free-text
sanitization (NFKC + injection regex) lives on the arcstore ``Task`` model's
own field validator and on ``TaskStore.update``'s write path, so every
construction and mutation is sanitized at the store boundary — no per-caller
validator to keep in sync (SEC-F2/ARCH-4).
"""

from __future__ import annotations

from typing import Literal

from arcstore.tasks import (
    RUN_ENDED_UNFINISHED,
    SERVICE_RESTART_INTERRUPTED,
    Priority,
    Task,
    TaskStatus,
    reclaim_allowance_s,
)

ClaimReason = Literal["assigned", "continue_current", "no_tasks_available"]

__all__ = [
    "RUN_ENDED_UNFINISHED",
    "SERVICE_RESTART_INTERRUPTED",
    "ClaimReason",
    "Priority",
    "Task",
    "TaskStatus",
    "reclaim_allowance_s",
]
