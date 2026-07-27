"""Per-turn ambient context — the inbound channel of the current turn.

Set once at turn-dispatch entry (``agent_dispatch``) from the ``reply_target`` a
surface passes to :meth:`ArcAgent.run`, so any capability running inside the
turn — e.g. the scheduler creating a schedule mid-conversation — can default
delivery back to the channel the request arrived on. ``None`` outside an
interactive channel turn (scheduler-driven runs, the messaging inbox loop).

It is a plain :class:`contextvars.ContextVar` set at the same dispatch entry
point that replays module ``_runtime`` bindings, so the value is present in the
same context the arcrun loop's tool dispatches inherit — the reason the setter
lives in dispatch, not in the gateway executor (a contextvar set across the
executor→agent task boundary does not reliably reach tool calls; see the
``bind_session_id`` pattern in ``agent_dispatch``).
"""

from __future__ import annotations

import contextvars

_inbound_channel: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arcagent_inbound_channel", default=None
)


def set_inbound_channel(target: str | None) -> None:
    """Record the current turn's inbound channel ("platform:chat_id")."""
    _inbound_channel.set(target)


def inbound_channel() -> str | None:
    """The current turn's inbound channel target, or None if not a channel turn."""
    return _inbound_channel.get()


__all__ = ["inbound_channel", "set_inbound_channel"]
