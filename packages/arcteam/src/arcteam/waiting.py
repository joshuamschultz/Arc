"""Direction (b): an agent asked, the human is silent — the operator's queue.

This is the outbound mirror of :func:`arcteam.messaging.sweep.unanswered`.

That predicate finds a **human** question no **agent** answered — the agent's
backlog, something for the fleet to pick up. This one finds the opposite
population: an **agent** question no **human** answered. That is the operator's
action queue — an agent ended a turn by asking a person something over a
channel and is now blocked on a reply nobody has given.

Three properties make it safe to surface on Home's "NEEDS YOU" panel:

* **Attribution is the signed asker, never a heuristic.** The agent credited as
  waiting is the one whose Ed25519 signature is on the message
  (``signer_did``) — deterministic across reads, and unforgeable by a message
  that merely claims a ``sender`` (ASI03/ASI09). It is emphatically *not*
  ``routing.default_responder``: that answers "who *should* reply", a
  non-deterministic BM25/dense ranking that can flip the attributed agent
  between two reads and belongs to the inbound direction, not this one.

* **No double-counting, by structural provenance.** A run paused on an approval
  or a workflow gate already surfaces through its own queue (pending approvals,
  review-tasks). Such a run also *narrates* itself onto the channel, and that
  narration is stamped ``meta["class"] == "narration"`` by
  :class:`arcteam.workflow.narrator.RunNarrator`. It is excluded here on that
  structural mark — never by matching text — so the paused run is counted once.
  Task/result/ack envelopes carry their work on the task queue and are excluded
  the same structural way.

* **Only a deliberate ask, and orphans age out.** A message counts only when
  the agent flagged it ``action_required=True`` — the signed, unforgeable line
  between "should I proceed?" and an agent's auto-posted closing reply or a
  plain FYI, which both leave the flag False and are excluded. "Silent" then
  means no human message appears later in the channel than the ask (documented
  v1 limitation below). A question older than :data:`MAX_WAIT_AGE_SECONDS` has
  outlived its run and leaves the queue, because a permanent orphan on NEEDS YOU
  trains the operator to ignore the panel.

Read-only over messenger storage: it advances no cursor and writes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from arcteam.types import Channel, Entity, EntityType, Message, MsgType

_logger = logging.getLogger("arcteam.waiting")

# How far back each channel is read per pass. A question older than this window
# has been answered or has aged out, and re-reading a whole stream is not free.
# Matches the deferred sweep's lookback for the same reason.
_LOOKBACK = 200

# A question whose age exceeds this leaves the queue. An unanswered ask that has
# sat for a week is an orphan whose originating run is long dead; a permanent
# orphan in NEEDS YOU is worse than absent, because it teaches the operator that
# the panel lies. Named, not a magic literal, and part of the reader's contract.
#
# The time cap is a proxy for run-liveness, not the ideal signal: ideally an ask
# would leave the moment its originating run ends. That eviction is deferred for
# one concrete reason — an arcteam Message carries no run_id/request_id, so
# liveness has nothing to join on. (arcteam legally reads the arcstore runs
# substrate, so this is a missing field, not a forbidden reach-through.) It
# becomes a cheap follow-up if/when messages gain a run_id.
MAX_WAIT_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days

# A structured work envelope is not a free-form question to a person: a task, its
# assignment, its result, and an ack all have their own operator surface (the
# task queue). Only conversational kinds can be an un-answered question here.
_STRUCTURED_KINDS = frozenset(
    {MsgType.TASK, MsgType.TASK_ASSIGNED, MsgType.RESULT, MsgType.ACK}
)

# The ``meta["class"]`` value RunNarrator stamps on every run/gate/handoff story
# it posts. Such a message mirrors a run already surfaced through approvals or
# review-tasks, so counting it here would double-count the same paused run.
_NARRATION_CLASS = "narration"

# How much of the question body rides along in the preview. Home links out to the
# channel for the rest; this is enough to recognise which question is waiting.
_PREVIEW_CHARS = 200


def signed_author(message: Message) -> str:
    """The DID whose signature is on *message*, or ``""`` when unsigned.

    Attribution reads the cryptographic ``signer_did`` only — never the derived
    "who should answer" ranking and never the free-form ``sender`` claim — so
    the same agent is credited on every read and a forged ``sender`` cannot
    misattribute a question. ``MessagingService.send`` stamps this field and
    refuses to deliver a message whose ``sender`` does not resolve to it, so a
    delivered channel message always carries it.
    """
    return message.signer_did


def _age_seconds(ts: str, now: datetime) -> float:
    """Seconds between *ts* and *now*, or 0.0 when *ts* cannot be read.

    An unparseable timestamp reads as "brand new" so a question is never aged
    out on bad data — it stays visible until a later read can date it.
    """
    try:
        stamped = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return (now - stamped).total_seconds()


def _is_waiting_question(message: Message) -> bool:
    """Whether *message* is an ask the agent flagged as needing the operator.

    Three structural gates, no body text:

    * ``action_required`` must be True — the agent deliberately asked the
      operator to act (``messaging_send(action_required=True)``). This is the
      line between a question and noise: an agent's auto-posted final reply
      (``deliver_channel_reply``) and a plain FYI both leave it False, so both
      are excluded, and it is unforgeable — ``action_required`` is in the
      Ed25519-signed field set (``arcteam.crypto._SIGNED_FIELDS``).
    * not run/gate narration (a paused run is already counted elsewhere), and
    * not a structured task/result/ack envelope (its work lives on the task
      queue).
    """
    if not message.action_required:
        return False
    if str(message.meta.get("class", "")) == _NARRATION_CLASS:
        return False
    return message.msg_type not in _STRUCTURED_KINDS


def unanswered_by_human(
    messages: list[Message],
    *,
    agents: set[str],
    humans: set[str],
    now: datetime,
    max_age_seconds: float = MAX_WAIT_AGE_SECONDS,
) -> list[Message]:
    """The agent questions on one channel that no human has answered, not stale.

    Takes the **whole** channel in chronological order, not a pre-filtered slice:
    being an agent's question and being answered by a human are two different
    populations, so dropping the humans before the reply check would make every
    answered question look unanswered.

    "Answered" is any *later* message signed by a human. Other agents replying
    does not clear it — the whole point of direction (b) is that a *person* has
    not, so only a human message in the set ``humans`` counts as a reply.

    **A "waiting question" (v1 definition):** an agent message the agent flagged
    ``action_required=True`` (see :func:`_is_waiting_question`), no human has
    answered, and that is not stale. The ``action_required`` gate is what makes
    this the operator's *action* queue rather than a channel firehose: an
    agent's auto-posted final reply and a plain FYI both leave the flag False
    and are excluded, so only a deliberate ask surfaces — and the flag is
    Ed25519-signed, so it cannot be forged.

    **Known v1 limitation (Planner-accepted, deliberate):** "silent" is
    presence-of-a-later-human-message, not thread pairing. Two flagged questions
    from the same agent where a human answers only the second mark BOTH
    answered.
    """
    result: list[Message] = []
    for index, message in enumerate(messages):
        if signed_author(message) not in agents:
            continue
        if not _is_waiting_question(message):
            continue
        if _age_seconds(str(message.ts), now) > max_age_seconds:
            continue
        later_authors = {signed_author(m) for m in messages[index + 1 :]}
        if later_authors & humans:
            continue
        result.append(message)
    return result


@dataclass(frozen=True)
class OperatorScope:
    """Which channels an operator may see through this reader (H-019 forward).

    Multi-operator is coming, and a second operator must never read the first's
    DMs through the NEEDS-YOU aggregate. Visibility is enforced HERE, at the
    reader, not left to the caller to remember. Today a single operator sees
    everything (:meth:`everything`); when H-019 lands, :meth:`for_operator`
    restricts reads to the channels that operator is a member of.
    """

    operator_did: str | None = None

    @classmethod
    def everything(cls) -> OperatorScope:
        """The single-operator default: every channel is visible."""
        return cls(operator_did=None)

    @classmethod
    def for_operator(cls, operator_did: str) -> OperatorScope:
        """Restrict visibility to channels *operator_did* is a member of."""
        return cls(operator_did=operator_did)

    def sees(self, channel: Channel) -> bool:
        """Whether this scope may read *channel*."""
        if self.operator_did is None:
            return True
        return self.operator_did in channel.members


class WaitingQuestion(BaseModel):
    """One agent question awaiting a human — a NEEDS-YOU item.

    ``agent_did`` is the signed asker (who is credited as waiting), not a
    responder heuristic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_did: str
    channel: str
    message_id: str
    ts: str
    preview: str


class _Registry(Protocol):
    """The one registry method this reader needs — entity type classification."""

    async def list_entities(self) -> list[Entity]: ...


class _Channels(Protocol):
    """The two read-only messenger methods this reader needs."""

    async def list_channels(self) -> list[Channel]: ...

    async def list_channel_messages(
        self, channel_name: str, after_seq: int = 0, limit: int = 100
    ) -> list[Message]: ...


async def waiting_on_human(
    service: _Channels,
    registry: _Registry,
    *,
    scope: OperatorScope | None = None,
    now: datetime | None = None,
) -> list[WaitingQuestion]:
    """Every agent question, fleet-wide, that no human has answered yet.

    Reads channels the *scope* may see, classifies entities into agents and
    humans off the registry, and collects each channel's un-answered agent
    questions via :func:`unanswered_by_human`. Read-only: no cursor advances,
    nothing is written.

    Attribution is the signed asker (:func:`signed_author`); an un-signable
    candidate is skipped rather than credited to the wrong agent. Scope gates
    visibility so a future second operator cannot read another's channels.
    """
    scope = scope or OperatorScope.everything()
    now = now or datetime.now(UTC)
    entities = await registry.list_entities()
    agents = {e.did for e in entities if e.type == EntityType.AGENT}
    humans = {e.did for e in entities if e.type == EntityType.USER}

    waiting: list[WaitingQuestion] = []
    for channel in await service.list_channels():
        if not scope.sees(channel):
            continue
        messages = await service.list_channel_messages(channel.name, limit=_LOOKBACK)
        for message in unanswered_by_human(messages, agents=agents, humans=humans, now=now):
            waiting.append(
                WaitingQuestion(
                    agent_did=signed_author(message),
                    channel=channel.name,
                    message_id=str(message.id),
                    ts=str(message.ts),
                    preview=str(message.body)[:_PREVIEW_CHARS],
                )
            )
    return waiting


__all__ = [
    "MAX_WAIT_AGE_SECONDS",
    "OperatorScope",
    "WaitingQuestion",
    "signed_author",
    "unanswered_by_human",
    "waiting_on_human",
]
