"""Decorator-form slack module — SPEC-021 task 3.6.

Replaces :class:`SlackModule`'s lifecycle and three subscriptions with
the unified capability surface:

  * ``@capability(name="slack")`` class — owns the
    :class:`SlackBot` Socket Mode WebSocket; ``setup`` connects,
    ``teardown`` closes.
  * ``@tool slack_notify_user`` — agent-callable tool that sends a
    proactive DM to the stored user.
  * ``@hook agent:ready``       — binds ``agent.chat()`` callback into
    the bot for deferred wiring.
  * ``@hook agent:shutdown``    — tears down the bot (parallel safety
    net to lifecycle teardown for module-only deployments).
  * ``@hook schedule:failed``   — forwards schedule failure errors as a
    user notification.

State is shared via :mod:`arcagent.modules.slack._runtime`. The agent
configures it once at startup; the capability + hooks read state
lazily.

The legacy :class:`SlackModule` class still exists alongside this
module to keep the existing Module-Bus test surface working; both
forms route through the same :class:`SlackBot` semantics.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arcagent.modules.slack import _runtime
from arcagent.modules.slack.bot import SlackBot
from arcagent.tools._decorator import capability, hook, tool

_logger = logging.getLogger("arcagent.modules.slack.capabilities")


@capability(name="slack")
class SlackCapability:
    """WebSocket lifecycle owner for the slack module.

    ``setup`` instantiates a :class:`SlackBot` from runtime state and
    establishes the Socket Mode connection. ``teardown`` cleanly closes
    the connection. Both are idempotent — called more than once they
    no-op safely.
    """

    async def setup(self, ctx: object) -> None:
        """Create + start the Slack bot. ``ctx`` is unused (R-061)."""
        st = _runtime.state()
        if st.bot is not None:
            return
        bot = SlackBot(
            config=st.config,
            telemetry=st.telemetry,
            workspace=st.workspace,
        )
        await bot.start()
        st.bot = bot
        _logger.info("Slack capability set up")

    async def teardown(self) -> None:
        """Stop the Slack bot. Safe to call when bot is absent."""
        st = _runtime.state()
        if st.bot is None:
            return
        await st.bot.stop()
        st.bot = None
        _logger.info("Slack capability torn down")


@tool(
    name="slack_notify_user",
    description=(
        "Send a message to the user via Slack DM. Use this ONLY when "
        "you have a meaningful update, result, question, or need "
        "direction. Do NOT use for routine status like 'no new messages' "
        "or 'task completed with no findings'."
    ),
    classification="state_modifying",
    capability_tags=["slack_notify"],
    when_to_use=(
        "When you need to proactively message the user with a finding, "
        "question, or action item via Slack."
    ),
)
async def slack_notify_user(message: str) -> str:
    """Send a notification to the user via Slack."""
    if not message:
        return json.dumps({"error": "message is required"})
    st = _runtime.state()
    if st.bot is None:
        return json.dumps({"error": "Slack bot not running"})
    await st.bot.send_notification(message)
    _logger.info("Agent sent user notification (%d chars)", len(message))
    return json.dumps({"status": "sent", "length": len(message)})


@hook(event="agent:ready")
async def bind_agent_chat_fn(ctx: Any) -> None:
    """Bind ``agent.chat()`` callback into the bot for deferred wiring."""
    st = _runtime.state()
    if st.bot is None:
        return
    data = ctx.data if hasattr(ctx, "data") else {}
    chat_fn = data.get("chat_fn")
    if chat_fn is not None:
        st.bot.set_agent_chat_fn(chat_fn)
        _logger.info("Bound agent_chat_fn via agent:ready event")


@hook(event="agent:shutdown")
async def stop_slack_bot(ctx: Any) -> None:
    """Stop the Slack bot. Mirrors :class:`SlackCapability.teardown`."""
    st = _runtime.state()
    if st.bot is None:
        return
    await st.bot.stop()
    st.bot = None


@hook(event="schedule:failed")
async def notify_schedule_failed(ctx: Any) -> None:
    """Forward schedule failure errors as a user notification."""
    st = _runtime.state()
    if st.bot is None:
        return
    data = ctx.data if hasattr(ctx, "data") else {}
    error = data.get("error", "unknown error")
    await st.bot.send_notification(f"Scheduled task failed: {error}")


__all__ = [
    "SlackCapability",
    "bind_agent_chat_fn",
    "notify_schedule_failed",
    "slack_notify_user",
    "stop_slack_bot",
]
