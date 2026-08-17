"""The backstop: a scheduled pass over questions nobody answered (ADR-032).

The fast path decides who answers in the moment. It can still miss — the winner
was in cooldown, the room had already hit its answer cap, the breaker was open,
or the agent was simply down when the message arrived. Every one of those is a
bounded, audited, deliberate silence, and every one of them still leaves a human
staring at a question nobody replied to.

So the sweep exists, and its role is exactly one thing: **it is the backstop,
not the fast path.** Claude Tag ships mention-gating with a scheduled sweep
behind it, which is a good answer that costs minutes of latency; we take the
sweep and keep the immediate route in front of it.

It does not re-run the router. If ranking was going to find an owner it already
did, so re-ranking would just repeat the decision that produced the silence.
The channel's named responder picks the message up instead — deterministically,
so exactly one agent acts with no coordinator.

Bounded on every axis that could run away: only a human's un-addressed message,
only one older than the grace window, only one nothing has replied to, only if
this agent is the responder, only a few per pass, and only ever once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger("arcagent.modules.messaging.sweep")

# How far back a pass looks. A message older than this was missed by something
# other than timing, and re-reading the whole channel every pass is not free.
_LOOKBACK = 200


def _age_seconds(ts: str) -> float:
    """Seconds since *ts*, or 0.0 when it cannot be read.

    An unparseable timestamp reads as "brand new", so the message waits for a
    later pass rather than being swept immediately on bad data.
    """
    try:
        stamped = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamped).total_seconds()


def unanswered(messages: list[Any], *, humans: set[str], older_than: float) -> list[Any]:
    """The human questions in a channel that nobody replied to, and that had time to be.

    Takes the **whole** channel, not a pre-filtered slice. Being a question and
    being answered are two different populations: only a human asks, but anyone
    may answer, so dropping the agents before the reply check would make every
    question the room actually answered look unanswered and sweep it a second
    time.

    "Replied to" is any later message from someone other than the asker, which
    is the same evidence the answer cap reads. Addressed messages are left
    alone: a mention has a deterministic owner whose own durable consumer will
    deliver it when that agent comes back, so sweeping it would produce a second
    answer rather than a first one.
    """
    return [
        message
        for index, message in enumerate(messages)
        if str(message.signer_did or message.sender) in humans
        and not message.mentions
        and not {str(m.sender) for m in messages[index + 1 :]} - {str(message.sender)}
        and _age_seconds(str(message.ts)) >= older_than
    ]


async def _human_dids(st: Any) -> set[str]:
    from arcteam.types import EntityType

    entities = await st.registry.list_entities()
    return {e.did for e in entities if e.type == EntityType.USER}


async def _agent_dids(st: Any) -> list[str]:
    from arcteam.types import EntityType

    entities = await st.registry.list_entities()
    return [e.did for e in entities if e.type == EntityType.AGENT]


async def find_missed(st: Any) -> list[tuple[Any, str]]:
    """Every (message, channel) this agent should pick up as the responder.

    Reads only; the caller decides what to wake. Keeping the search separate
    from the waking is what makes the policy testable without a live agent.
    """
    from arcteam import routing

    me = st.identity.did if st.identity is not None else ""
    if not me:
        return []
    humans = await _human_dids(st)
    agents = await _agent_dids(st)
    missed: list[tuple[Any, str]] = []
    for channel in await st.svc.list_channels():
        if me not in channel.members:
            continue
        if routing.default_responder(channel, agents) != me:
            continue
        messages = await st.svc.list_channel_messages(channel_name=channel.name, limit=_LOOKBACK)
        for message in unanswered(
            messages, humans=humans, older_than=st.config.sweep_after_seconds
        ):
            if str(message.id) not in st.swept:
                missed.append((message, channel.name))
    return missed


async def run_once(st: Any, deliver: Any) -> int:
    """One pass. Returns how many messages were picked up.

    Every message is marked swept **before** delivery is attempted, not after.
    A delivery that raises must not leave the message eligible again on the next
    pass: a backstop that retries a failing wake every five minutes is a
    runaway, and the human can always ask again.
    """
    if not st.config.sweep_enabled:
        return 0
    try:
        missed = await find_missed(st)
    except Exception:  # reason: a backstop must never take down the inbox loop
        _logger.warning("deferred sweep could not read the channels", exc_info=True)
        return 0

    picked = 0
    for message, channel in missed[: st.config.sweep_max_per_tick]:
        st.swept.add(str(message.id))
        try:
            await deliver(message, channel)
        except Exception:  # reason: one bad wake must not stop the rest of the pass
            _logger.warning("deferred sweep could not wake on #%s", channel, exc_info=True)
            continue
        picked += 1
    return picked


__all__ = ["find_missed", "run_once", "unanswered"]
