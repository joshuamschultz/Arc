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


_overheard: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "arcagent_overheard", default=False
)


def set_overheard(value: bool) -> None:
    """Mark the current turn as driven by a message this agent merely OVERHEARD.

    An un-addressed post to a shared channel wakes every member, so each one runs a
    turn on a message that named nobody. Answering is the design; retaining is not —
    otherwise one operator's message to the sales agent lands permanently in the
    trader's memory, the marketer's, and every other member's, and in all of their
    future prompts.

    Triage cannot carry this distinction: it decides "is replying my job?", never
    "is this mine to keep?", and it is fail-open, so an agent that stayed silent has
    usually still stored the message.
    """
    _overheard.set(value)


def overheard() -> bool:
    """Whether this turn came from a broadcast addressed to nobody in particular."""
    return _overheard.get()


def inbound_channel() -> str | None:
    """The current turn's inbound channel target, or None if not a channel turn."""
    return _inbound_channel.get()


def is_team_target(target: str) -> bool:
    """True when ``target`` is an arcteam address, not a gateway platform channel.

    The two delivery mediums are distinct. A gateway target is ``platform:chat_id``
    (telegram/slack/web) delivered through the embedded gateway's
    ``channel_deliver_fn``; an arcteam target (``channel://``, ``agent://``,
    ``role://``, ``user://``) is delivered back onto the team bus. Gateway targets
    never contain ``//`` — a reply to a dashboard group post must go back into the
    channel, not out to whatever platform the agent was last reached on.
    """
    return "://" in target


__all__ = [
    "inbound_channel",
    "is_team_target",
    "overheard",
    "set_inbound_channel",
    "set_overheard",
]
