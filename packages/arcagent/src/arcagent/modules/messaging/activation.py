"""Whether one inbound message wakes this agent's run (SPEC-068 D1/D4).

A channel post reaches every member. Deciding who answers it is the product
question the operator settled: agents judge for themselves, because the person
posting often does not know who holds the answer, and an agent with useful
context should speak up even when the message was not aimed at it. So the
judgement stays — what changes is the direction it fails in.

The ladder is ordered cheapest-first and the model call is last, so a message
that can be dismissed for free never pays for a classifier:

===  ==========================  ================================
 #   check                       bypassed by
===  ==========================  ================================
 1   this agent sent it          nothing
 2   critical priority           — (decides: always wake)
 3   hop budget exhausted        nothing
 4   @mention scope              — (decides)
 5   not a channel post (DM)     — (decides: always wake)
 6   sender is an agent          mention, critical
 7   cooldown                    mention, critical
 8   answers already given       mention, critical
 9   circuit breaker             nothing
10   relevance gate (LLM)        mention, critical
===  ==========================  ================================

Checks 1, 3 and 6 are the loop guards. 6 is the strong one: only a *human's*
un-addressed post fans out, so an agent's reply — which is itself an
un-addressed channel post — wakes nobody, and A -> B -> A is impossible by
construction rather than merely bounded. 3 bounds the chain that remains, where
agents address each other explicitly. ``hop`` is covered by the message
signature, because a loop guard an adversary can clear is not a guard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.messaging.activation")

# A message may travel two agent turns from the human who started it. One lets a
# mentioned agent hand off once; two ends the chain before it can return to its
# author and start again.
MAX_HOP = 2

_CHANNEL_PREFIX = "channel://"


@dataclass(frozen=True)
class Decision:
    """Whether to wake, and the reason — carried into audit rather than a bare bool."""

    wake: bool
    reason: str


def channel_of(msg: Any) -> str | None:
    """The channel this message was posted to, or None for a DM/role message."""
    for target in msg.to or []:
        text = str(target)
        if text.startswith(_CHANNEL_PREFIX):
            return text[len(_CHANNEL_PREFIX) :]
    return None


def is_overheard(msg: Any) -> bool:
    """Whether answering this must not also mean remembering it (``7812264f``).

    An un-addressed channel post wakes every member, so each runs a turn on a
    message that named nobody. Answering is the design; retaining is not.
    """
    if str(msg.priority) == "critical" or msg.mentions:
        return False
    return channel_of(msg) is not None


def _is_own(msg: Any, identity: Any) -> bool:
    """Whether this agent is the author, reached via its own channel stream."""
    if identity is None:
        return False
    return identity.did in (msg.signer_did, msg.sender)


async def _sender_is_human(msg: Any, st: Any) -> bool:
    """Whether a registered ``user`` entity signed this message.

    Fail-closed: an unresolvable sender is not treated as a human, so a message
    of unknown provenance cannot trigger the fan-out. The operator's dashboard
    posts register as ``EntityType.USER``, which is what makes this separable.
    """
    try:
        from arcteam.types import EntityType

        entities = await st.registry.list_entities()
        did = msg.signer_did or msg.sender
        entity = next((e for e in entities if e.did == did), None)
        return entity is not None and entity.type == EntityType.USER
    except Exception:  # reason: fail-closed — unknown provenance does not fan out
        _logger.warning("could not classify message sender; not activating", exc_info=True)
        return False


def _in_cooldown(st: Any, channel: str) -> bool:
    """Whether this agent answered ``channel`` too recently to answer again."""
    window = st.config.channel_cooldown_seconds
    if window <= 0:
        return False
    last = st.channel_last_woken.get(channel)
    return last is not None and (time.monotonic() - last) < window


def record_activation(st: Any, channel: str) -> None:
    """Start this agent's cooldown for ``channel``."""
    st.channel_last_woken[channel] = time.monotonic()


async def _answer_cap_reached(msg: Any, st: Any, channel: str) -> bool:
    """Whether enough agents have already answered this message.

    Counts distinct senders that posted after it, other than its author. A soft
    cap by construction: every member decides independently with no coordinator,
    so agents deciding in the same tick can overshoot. It bounds the common case
    — a room noticing a question one after another — and the circuit breaker,
    not this, is what bounds a pathological one.
    """
    cap = st.config.channel_answer_cap
    if cap <= 0:
        return False
    try:
        later = await st.svc.list_channel_messages(
            channel_name=channel, after_seq=msg.seq, limit=cap * 4
        )
    except Exception:  # reason: advisory — a read failure must not silence the room
        _logger.warning("could not count answers in #%s; not capping", channel, exc_info=True)
        return False
    responders = {str(m.sender) for m in later if str(m.sender) != str(msg.sender)}
    return len(responders) >= int(cap)


def _breaker(st: Any, channel: str) -> Any:
    """The per-(agent, channel) breaker guarding the relevance gate."""
    from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

    breaker = st.channel_breakers.get(channel)
    if breaker is None:
        breaker = CircuitBreaker(
            failure_threshold=st.config.triage_failure_threshold,
            base_wait_seconds=st.config.triage_base_wait_seconds,
        )
        st.channel_breakers[channel] = breaker
    return breaker


def _gate_prompt(st: Any, channel: str) -> str:
    """The relevance gate's system prompt — both judgements, silence by default.

    Naming the agent's role is what makes the first judgement answerable at all,
    and the explicit non-value clause is what stops six agents replying "thanks".
    """
    name = sanitize_text(st.config.entity_name or st.agent_name, max_length=200)
    role = sanitize_text(st.config.entity_role or "not stated", max_length=500)
    return (
        f"You are {name}, one member of a team, in the shared channel #{channel}.\n"
        f"Your role: {role}.\n"
        "A message was posted to the channel. It was not addressed to anyone "
        "specific.\n"
        "Answer YES only if either is true:\n"
        "  1. The message is about your role or domain.\n"
        "  2. You hold specific information that would genuinely help, that "
        "another member is unlikely to have.\n"
        "Adding agreement, encouragement, or a restatement is not value.\n"
        "If you are unsure, answer NO.\n"
        "Reply with exactly one word: YES or NO."
    )


async def _relevance(msg: Any, st: Any, channel: str, breaker: Any) -> Decision:
    """One bounded yes/no call deciding whether answering is this agent's job.

    **Fails closed.** A missing classifier, a timeout, an exception, or a verdict
    that is not recognisably YES all mean silence. The old gate failed open, so
    a broken or slow model degraded to a full run in every member of the channel
    on every message — a cost control whose failure mode is to spend the maximum
    is the wrong shape. The recoverable direction is silence, because an
    @mention bypasses this gate entirely and always wakes its target.
    """
    if not st.config.channel_triage:
        return Decision(True, "gate_disabled")
    if st.classify_fn is None:
        return Decision(False, "gate_unavailable")
    try:
        verdict: str = await asyncio.wait_for(
            st.classify_fn(
                system=_gate_prompt(st, channel),
                user=sanitize_text(str(msg.body), max_length=2000),
            ),
            timeout=st.config.triage_timeout_seconds,
        )
    except Exception:  # reason: fail-closed — an undecided gate does not wake a run
        breaker.record_failure()
        _logger.warning("relevance gate failed for #%s; staying silent", channel, exc_info=True)
        return Decision(False, "gate_failed")
    breaker.record_success()
    if verdict.strip().upper().startswith("Y"):
        return Decision(True, "relevant")
    return Decision(False, "not_relevant")


async def decide(msg: Any, st: Any) -> Decision:
    """Run the ladder for one inbound message."""
    identity = st.identity
    if _is_own(msg, identity):
        return Decision(False, "self")
    if str(msg.priority) == "critical":
        return Decision(True, "critical")
    if int(getattr(msg, "hop", 0) or 0) >= MAX_HOP:
        return Decision(False, "hop_exhausted")
    if msg.mentions:
        if identity is not None and identity.did in list(msg.mentions):
            return Decision(True, "mentioned")
        return Decision(False, "addressed_to_others")
    channel = channel_of(msg)
    if channel is None:
        return Decision(True, "direct")
    if not await _sender_is_human(msg, st):
        return Decision(False, "agent_broadcast")
    if _in_cooldown(st, channel):
        return Decision(False, "cooldown")
    if await _answer_cap_reached(msg, st, channel):
        return Decision(False, "answer_cap")
    breaker = _breaker(st, channel)
    if not breaker.allow_request():
        return Decision(False, "breaker_open")
    return await _relevance(msg, st, channel, breaker)


__all__ = ["MAX_HOP", "Decision", "channel_of", "decide", "is_overheard", "record_activation"]
