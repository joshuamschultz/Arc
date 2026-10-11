"""Intake-row handling on the task dispatch path (SPEC-088 COMP-003, D-840).

An intake row is an ordinary task written by ``POST /api/runs``. Its control
data lives in the top-level ``metadata.intake`` block, which only the intake
route writes; caller metadata nested anywhere else (``metadata.app``) is never
read here, so a board task cannot pin a run id or borrow an identity by
looking like intake data. Nothing in this module reaches the prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arcstore.tasks import TaskStore

from arcagent.modules.tasks.models import Task
from arcagent.utils.sanitizer import sanitize_text

#: Largest ``output.text`` an intake row stores, in UTF-8 bytes.
MAX_OUTPUT_BYTES = 256 * 1024
_RUN_PURPOSES = frozenset({"manual", "message", "schedule", "pulse", "workflow"})


def intake_block(task: Task) -> Mapping[str, Any] | None:
    """The row's top-level ``metadata.intake`` block, or ``None`` for any other row."""
    block = task.metadata.get("intake")
    return block if isinstance(block, Mapping) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def intake_run_id(task: Task) -> str | None:
    """The run id the intake route minted for this row, if it is an intake row."""
    block = intake_block(task)
    return None if block is None else _text(block.get("run_id"))


def intake_run_kwargs(task: Task) -> dict[str, Any]:
    """Extra ``run_collected`` keywords for an intake row; empty for any other row.

    ``caller_did`` is the person the app acts for, else the app that created
    the row. An unknown ``run_purpose`` is dropped (the run keeps its default)
    rather than forwarded.
    """
    block = intake_block(task)
    if block is None:
        return {}
    kwargs: dict[str, Any] = {}
    caller = _text(block.get("on_behalf_of")) or _text(task.creator_did)
    if caller is not None:
        kwargs["caller_did"] = caller
    purpose = block.get("run_purpose")
    if purpose in _RUN_PURPOSES:
        kwargs["run_purpose"] = purpose
    return kwargs


def bounded_output_text(text: str) -> str:
    """Cut ``text`` to ``MAX_OUTPUT_BYTES`` of UTF-8 without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")


def _loop_error(result: Any) -> str | None:
    """The loop's own failure reason from a ``RunResult`` payload, if it failed."""
    payload = getattr(result, "completion_payload", None)
    if not isinstance(payload, dict) or payload.get("status") != "failed":
        return None
    return str(payload.get("error") or payload.get("summary") or "run failed")


async def settle_intake_run(store: TaskStore, task: Task, result: Any, actor_did: str) -> None:
    """Make a still-running intake row terminal from the run's returned result.

    An agent that already finished its own row keeps its result: both writes
    are conditional on the row still being this attempt's ``in_progress``.
    A ``None`` result means the failure path already transitioned the row.
    """
    run_id = intake_run_id(task)
    if run_id is None or result is None:
        return
    current = await store.get(task.id)
    if current is None or current.status != "in_progress":
        return
    error = _loop_error(result)
    if error is not None:
        await store.dead_letter(
            task.id,
            actor_did=actor_did,
            resolution="run failed",
            last_error=sanitize_text(error, max_length=500),
            expected_attempts=current.attempts,
        )
        return
    content = getattr(result, "content", None)
    await store.complete_attempt(
        task.id,
        attempt_key=run_id,
        attempts=current.attempts,
        resolution="run finished",
        output={"text": bounded_output_text(content if isinstance(content, str) else "")},
        actor_did=actor_did,
    )


__all__ = [
    "MAX_OUTPUT_BYTES",
    "bounded_output_text",
    "intake_block",
    "intake_run_id",
    "intake_run_kwargs",
    "settle_intake_run",
]
