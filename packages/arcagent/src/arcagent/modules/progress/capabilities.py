"""Progress-narration wiring — the hooks that turn run events into channel messages.

Three hooks:

* ``agent:ready`` (priority 100) — take the embedded gateway's channel delivery
  callback, the same seam ``notify_user`` and scheduled deliveries ride.
* ``agent:run_progress`` (priority 200) — the narration itself. Logging priority,
  because nothing downstream depends on this and nothing here may hold a run up.
* ``agent:shutdown`` (priority 60) — cancel any coalesce timer still waiting.

Everything is best effort. A delivery that fails is logged and dropped; it must
never slow or break the run it describes. The loop is never made to wait: the
bridge that emits ``agent:run_progress`` already schedules each emission as a
detached task, so the ``await`` on delivery happens well off the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from arcagent.core import turn_context
from arcagent.modules.progress import _runtime, narrator
from arcagent.modules.progress._runtime import Tally
from arcagent.tools._decorator import hook
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.progress.capabilities")

_READY_PRIORITY = 100
_NARRATE_PRIORITY = 200
_SHUTDOWN_PRIORITY = 60


# -- Origin resolution ---------------------------------------------------


def _deliverable_target(raw: Any) -> str | None:
    """The channel this turn arrived on, when we can actually reach it.

    The ONLY source of a destination in this module. There is no fallback to a
    remembered channel or a most-recent channel, because a fallback is how a
    question asked in one place gets answered in another. Two cases return None,
    and both mean total silence:

    * no origin at all — a scheduled run, a dispatched task, a headless CLI run;
    * an arcteam address (``channel://``, ``agent://``) — that rides the team bus,
      which this module has no client for. Silence beats handing a group's
      progress to whatever gateway the agent was last reached on.
    """
    if not isinstance(raw, str) or not raw:
        return None
    return None if turn_context.is_team_target(raw) else raw


# -- Hooks ---------------------------------------------------------------


@hook(event="agent:ready", priority=_READY_PRIORITY)
async def progress_bind_delivery(ctx: Any) -> None:
    """Capture the gateway's channel delivery callback for narration."""
    data = ctx.data if hasattr(ctx, "data") else {}
    _runtime.state().channel_deliver_fn = data.get("channel_deliver_fn")


@hook(event="agent:run_progress", priority=_NARRATE_PRIORITY)
async def narrate_run_progress(ctx: Any) -> None:
    """Turn one forwarded run event into at most one message on the origin channel."""
    data = ctx.data if hasattr(ctx, "data") else {}
    target = _deliverable_target(data.get("reply_target"))
    if target is None:
        return
    st = _runtime.state()
    event = str(data.get("event", ""))
    raw_payload = data.get("data")
    payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}

    if event == "dynamic.agent.start":
        _record_child_start(st, target)
        return
    if event == "dynamic.agent.end":
        await _record_child_end(st, target, payload)
        return
    line = narrator.line_for(event, payload, max_step_chars=st.config.max_step_chars)
    if line is not None:
        await _send(st, target, line)


@hook(event="agent:shutdown", priority=_SHUTDOWN_PRIORITY)
async def drain_on_shutdown(_ctx: Any) -> None:
    """Cancel every waiting coalesce timer and forget the tallies."""
    st = _runtime.state()
    pending = list(st.background_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    st.tallies.clear()


# -- Child-agent accounting ----------------------------------------------


def _record_child_start(st: _runtime._State, target: str) -> None:
    """Note one more child agent and make sure a coalesce timer is running.

    Children arrive in bursts, so nothing is announced yet: the timer below waits
    out the burst and reports the whole batch as one line.
    """
    tally = st.tallies.setdefault(target, Tally())
    tally.pending += 1
    if tally.flush_task is not None:
        return
    task = asyncio.create_task(_announce_batch(st, target))
    tally.flush_task = task
    st.background_tasks.add(task)
    task.add_done_callback(st.background_tasks.discard)


async def _announce_batch(st: _runtime._State, target: str) -> None:
    """Wait out the burst, then announce every child seen during it as one line."""
    try:
        await asyncio.sleep(st.config.coalesce_seconds)
        tally = st.tallies.get(target)
        if tally is None:
            return
        count, tally.pending = tally.pending, 0
        tally.flush_task = None
        if count <= 0:
            return
        tally.started += count
        await _send(st, target, narrator.fanout_line(count))
        # A child can finish before its start was ever announced (a fast one, or a
        # depth refusal). Re-check here so that batch is not left hanging.
        await _maybe_announce_results(st, target)
    except asyncio.CancelledError:
        raise
    except Exception:  # reason: fail-open — narration must never surface as a task error
        _logger.warning("progress: announcing a batch failed", exc_info=True)


async def _record_child_end(st: _runtime._State, target: str, payload: Mapping[str, Any]) -> None:
    """Note one child agent coming back, and report the batch once they all have."""
    tally = st.tallies.setdefault(target, Tally())
    tally.ended += 1
    if not payload.get("success", False):
        tally.failed += 1
    await _maybe_announce_results(st, target)


async def _maybe_announce_results(st: _runtime._State, target: str) -> None:
    """Report the batch result once every announced child has come back."""
    tally = st.tallies.get(target)
    if tally is None or tally.started <= 0 or tally.ended < tally.started:
        return
    total, finished = tally.started, tally.started - tally.failed
    tally.started = tally.ended = tally.failed = 0
    await _send(st, target, narrator.fanin_line(finished, total))


# -- Sending -------------------------------------------------------------


async def _send(st: _runtime._State, target: str, line: narrator.Line) -> None:
    """Send one line if pacing allows it, then apply its end-of-run reset."""
    tally = st.tallies.setdefault(target, Tally())
    if line.text and _allowed(st, tally, line):
        tally.lines_sent += 1
        tally.last_sent = time.monotonic()
        tally.last_kind = line.kind
        await _deliver(st, target, line.text)
    if line.terminal:
        if tally.flush_task is not None:
            tally.flush_task.cancel()
        st.tallies.pop(target, None)


def _allowed(st: _runtime._State, tally: Tally, line: narrator.Line) -> bool:
    """Whether this line clears the run's ceiling and the gap between lines.

    The gap is there to stop one source repeating itself, so it only bites when
    the previous line came from the same source — a stage name straight after
    the plan line is news, a stage name straight after another one can wait. A
    line that fails the gap is dropped, not queued: by the time a queued progress
    line would go out, the thing it describes is over.
    """
    if tally.lines_sent >= st.config.max_lines_per_run:
        return False
    if line.urgent or tally.lines_sent == 0 or line.kind != tally.last_kind:
        return True
    return time.monotonic() - tally.last_sent >= st.config.min_gap_seconds


async def _deliver(st: _runtime._State, target: str, text: str) -> None:
    """Put one line on the origin channel. Never raises."""
    if st.channel_deliver_fn is None:
        return
    try:
        await st.channel_deliver_fn(target, text)
    except Exception:  # reason: fail-open — a failed notice must not disturb the run
        _logger.warning("progress: delivery to %s failed", target, exc_info=True)
        return
    await safe_audit(
        st.telemetry,
        "progress.narrated",
        {"target": target, "chars": len(text)},
        logger=_logger,
    )


__all__ = [
    "drain_on_shutdown",
    "narrate_run_progress",
    "progress_bind_delivery",
]
