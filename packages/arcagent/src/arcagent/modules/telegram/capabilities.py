"""Decorator-form telegram module — SPEC-021 task 3.5.

Five capabilities expose Telegram functionality to the unified
capability loader:

  * ``@background_task(interval=...)`` — drives the bot polling
    lifecycle. Drain-then-replace on reload (R-062) cleanly stops
    the bot before a new task starts.
  * ``@tool notify_user``               — agent-callable proactive
    message to the human.
  * ``@hook agent:shutdown``            — graceful bot shutdown.
  * ``@hook schedule:completed``        — observability passthrough
    (no auto-forward; the agent decides via ``notify_user``).
  * ``@hook schedule:failed``           — auto-notify on failure.

State is shared via :mod:`arcagent.modules.telegram._runtime`. The
agent configures it once at startup; capabilities read state lazily.

The legacy :class:`TelegramModule` class still exists alongside this
module to keep its existing test surface working; both forms route
through the same :class:`TelegramBot` instance internally.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from arcagent.modules.telegram import _runtime
from arcagent.tools._decorator import background_task, hook, tool

_logger = logging.getLogger("arcagent.modules.telegram.capabilities")


@background_task(interval=1.0)
async def telegram_poll(ctx: Any) -> None:
    """Run the Telegram bot's polling lifecycle.

    Starts the bot once, then idles until cancellation. The bot
    itself manages its long-poll loop via ``python-telegram-bot``'s
    Updater; this task exists so the capability loader can
    drain-then-replace it on reload (R-062). On cancellation, the
    bot is stopped cleanly so the queue and updater drain.
    """
    st = _runtime.state()
    await st.bot.start()
    try:
        while True:
            # Sleep at the configured poll interval; the bot is
            # already polling in its own task. This loop only exists
            # to keep the supervised task alive for cancellation.
            await asyncio.sleep(st.config.poll_interval)
    except asyncio.CancelledError:
        await st.bot.stop()
        raise


@tool(
    description=(
        "Send a message to the user via Telegram. Use this ONLY when "
        "you have a meaningful update, result, question, or need "
        "direction. Do NOT use for routine status like 'no new messages' "
        "or 'task completed with no findings'."
    ),
    classification="state_modifying",
)
async def notify_user(message: str = "") -> str:
    """Send a proactive Telegram notification to the user."""
    if not message:
        return json.dumps({"error": "message is required"})
    st = _runtime.state()
    await st.bot.send_notification(message)
    _logger.info("Agent sent user notification (%d chars)", len(message))
    return json.dumps({"status": "sent", "length": len(message)})


@hook(event="agent:shutdown")
async def on_agent_shutdown(ctx: Any) -> None:
    """Stop the bot on agent shutdown.

    Idempotent: ``TelegramBot.stop`` is safe to call when the bot was
    never started (no-ops on its internal None checks).
    """
    st = _runtime.state()
    await st.bot.stop()


@hook(event="schedule:completed")
async def on_schedule_completed(ctx: Any) -> None:
    """Observe schedule:completed without auto-forwarding.

    Per design, completed schedules do not auto-notify — the agent
    decides what's worth sending via ``notify_user``. This hook
    exists so observability/audit pipelines see telegram subscribing
    to the event.
    """
    st = _runtime.state()
    if st.telemetry is not None and hasattr(st.telemetry, "record_event"):
        data = ctx.data if hasattr(ctx, "data") else {}
        st.telemetry.record_event(
            "telegram:schedule_completed_observed",
            {"schedule_name": data.get("schedule_name", "")},
        )


@hook(event="schedule:failed")
async def on_schedule_failed(ctx: Any) -> None:
    """Auto-notify the user when a scheduled task fails.

    Failures are always worth knowing about; unlike ``schedule:completed``
    we don't gate on agent judgment.
    """
    st = _runtime.state()
    data = ctx.data if hasattr(ctx, "data") else {}
    error = data.get("error", "unknown error")
    await st.bot.send_notification(f"Scheduled task failed: {error}")
