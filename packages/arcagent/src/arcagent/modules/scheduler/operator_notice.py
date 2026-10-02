"""Tell the operator when a schedule stops doing its job.

A schedule that fails, goes past due, or is switched off by its breaker is only
useful to catch if a person hears about it. These events used to be emitted with
nobody listening, so a nightly job could stay dead for weeks unnoticed.
"""

from __future__ import annotations

import logging
from typing import Any

from arctrust import sanitize_error_text

from arcagent.modules.scheduler._runtime import _State

_logger = logging.getLogger("arcagent.modules.scheduler.operator_notice")


def format_schedule_notice(event: str, data: dict[str, Any]) -> str:
    """One plain sentence naming the schedule, what happened, and why."""
    name = data.get("schedule_name") or data.get("schedule_id") or "a schedule"
    if event == "schedule:missed":
        return (
            f"Schedule {name} is {data.get('late_seconds', 0)}s past due and has not started "
            f"(due {data.get('due_at', 'unknown')})."
        )
    if event == "schedule:rearmed":
        return f"Schedule {name} was switched off by its breaker and has switched itself back on."
    reason = sanitize_error_text(str(data.get("error", "unknown error")), limit=300)
    if data.get("breaker_tripped"):
        return (
            f"Schedule {name} was switched off after {data.get('consecutive_failures')} "
            f"failures in a row. Last error: {reason}. It will try again on its own."
        )
    return f"Schedule {name} failed: {reason}"


async def notify_operator(st: _State, text: str, *, fallback_target: str | None = None) -> bool:
    """Deliver ``text`` to the operator's channel. Returns whether it was handed off.

    The target is the operator-configured one, else the schedule's own delivery
    target. With neither, or with no gateway, the notice is logged at error level:
    an undeliverable alarm must still be findable.
    """
    target = st.config.operator_notify_target or fallback_target
    deliver = st.channel_deliver_fn
    if not target or deliver is None:
        _logger.error("operator notice has no delivery path: %s", text)
        return False
    try:
        await deliver(target, text)
    except Exception:  # reason: a failed alert must never break the scheduler
        _logger.exception("operator notice delivery failed: %s", text)
        return False
    return True
