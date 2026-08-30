"""Direction (b): an agent asked, the human is silent (``arcteam.waiting``).

The outbound mirror of the deferred sweep's ``unanswered``. These tests pin the
four things the Planner correction called out: attribution is the *signed asker*
(never a responder heuristic), no double-counting of a narrated/paused run,
"silent" and the age-out are defined and enforced, and scope gates visibility.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arcteam.types import Channel, Entity, EntityType, Message, MsgType
from arcteam.waiting import (
    MAX_WAIT_AGE_SECONDS,
    OperatorScope,
    unanswered_by_human,
    waiting_on_human,
)

_AGENT = "did:arc:agent/olivia"
_AGENT_2 = "did:arc:agent/felix"
_HUMAN = "did:arc:user/operator"

_NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def _ts(seconds_ago: float) -> str:
    return (_NOW - timedelta(seconds=seconds_ago)).isoformat()


def _msg(
    *,
    signer_did: str,
    body: str = "question?",
    seconds_ago: float = 60.0,
    msg_type: MsgType = MsgType.INFO,
    meta: dict[str, object] | None = None,
    mid: str = "m1",
) -> Message:
    return Message(
        id=mid,
        ts=_ts(seconds_ago),
        sender=signer_did,
        signer_did=signer_did,
        to=["channel://ops"],
        body=body,
        msg_type=msg_type,
        meta=meta or {},
    )


# ---------------------------------------------------------------------------
# The pure predicate — unanswered_by_human
# ---------------------------------------------------------------------------


class TestPredicate:
    def test_agent_question_with_no_human_reply_is_returned(self) -> None:
        messages = [_msg(signer_did=_AGENT, body="need a decision")]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert [m.id for m in out] == ["m1"]

    def test_agent_question_answered_by_human_is_excluded(self) -> None:
        messages = [
            _msg(signer_did=_AGENT, seconds_ago=120, mid="q"),
            _msg(signer_did=_HUMAN, seconds_ago=60, body="do it", mid="a"),
        ]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert out == []

    def test_a_later_agent_reply_does_not_clear_the_question(self) -> None:
        # Direction (b) waits on a PERSON. Another agent chattering after the
        # question must not clear it — only a human message does, so "q" stays.
        # (The peer message itself also qualifies — see the v1-limitation test.)
        messages = [
            _msg(signer_did=_AGENT, seconds_ago=120, mid="q"),
            _msg(signer_did=_AGENT_2, seconds_ago=60, body="I agree", mid="peer"),
        ]
        out = unanswered_by_human(
            messages, agents={_AGENT, _AGENT_2}, humans={_HUMAN}, now=_NOW
        )
        assert "q" in {m.id for m in out}

    def test_v1_surfaces_any_unanswered_agent_message_not_only_questions(self) -> None:
        # DOCUMENTED v1 limitation (Planner-accepted): "silent" is presence-based
        # — any agent message with no later human reply qualifies, including a
        # plain statement. Question-vs-statement detection is explicitly out of
        # scope for v1. Both the ask and the peer statement are returned.
        messages = [
            _msg(signer_did=_AGENT, seconds_ago=120, mid="q"),
            _msg(signer_did=_AGENT_2, seconds_ago=60, body="I agree", mid="peer"),
        ]
        out = unanswered_by_human(
            messages, agents={_AGENT, _AGENT_2}, humans={_HUMAN}, now=_NOW
        )
        assert {m.id for m in out} == {"q", "peer"}

    def test_human_question_no_agent_reply_is_not_returned(self) -> None:
        # This is direction (a) — the agent's backlog, sweep.unanswered's job.
        # It must never leak into the operator's action queue.
        messages = [_msg(signer_did=_HUMAN, body="anyone alive?")]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert out == []

    def test_narration_is_excluded_by_structural_provenance(self) -> None:
        # A workflow run narrates itself onto the channel under an agent DID; it
        # is already surfaced via approvals / review-tasks. Excluded on the
        # RunNarrator's structural mark, never by matching body text.
        messages = [
            _msg(signer_did=_AGENT, body="Waiting on gate g1", meta={"class": "narration"})
        ]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert out == []

    @pytest.mark.parametrize(
        "kind", [MsgType.TASK, MsgType.TASK_ASSIGNED, MsgType.RESULT, MsgType.ACK]
    )
    def test_structured_work_envelopes_are_excluded(self, kind: MsgType) -> None:
        messages = [_msg(signer_did=_AGENT, msg_type=kind)]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert out == []

    def test_a_stale_orphan_ages_out(self) -> None:
        messages = [_msg(signer_did=_AGENT, seconds_ago=MAX_WAIT_AGE_SECONDS + 1)]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert out == []

    def test_a_fresh_question_within_the_cap_stays(self) -> None:
        messages = [_msg(signer_did=_AGENT, seconds_ago=MAX_WAIT_AGE_SECONDS - 1)]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert [m.id for m in out] == ["m1"]

    def test_unsigned_candidate_is_not_attributed(self) -> None:
        # No signer_did => not in the agent set => skipped, never mis-credited.
        messages = [_msg(signer_did="", body="who am I?")]
        out = unanswered_by_human(
            messages, agents={_AGENT}, humans={_HUMAN}, now=_NOW
        )
        assert out == []


# ---------------------------------------------------------------------------
# The reader — waiting_on_human (over fake service + registry)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def list_entities(self) -> list[Entity]:
        return list(self._entities)


class _FakeChannels:
    def __init__(self, channels: dict[str, tuple[Channel, list[Message]]]) -> None:
        self._channels = channels
        self.reads: list[str] = []

    async def list_channels(self) -> list[Channel]:
        return [ch for ch, _ in self._channels.values()]

    async def list_channel_messages(
        self, channel_name: str, after_seq: int = 0, limit: int = 100
    ) -> list[Message]:
        self.reads.append(channel_name)
        return list(self._channels[channel_name][1])


def _entity(did: str, etype: EntityType) -> Entity:
    handle = did.rsplit("/", 1)[-1]
    return Entity(did=did, handle=handle, id=did, name=handle, type=etype)


def _registry() -> _FakeRegistry:
    return _FakeRegistry(
        [
            _entity(_AGENT, EntityType.AGENT),
            _entity(_AGENT_2, EntityType.AGENT),
            _entity(_HUMAN, EntityType.USER),
        ]
    )


class TestReader:
    async def test_returns_waiting_question_with_signed_asker(self) -> None:
        ch = Channel(name="ops", members=[_AGENT, _HUMAN])
        service = _FakeChannels({"ops": (ch, [_msg(signer_did=_AGENT, body="need a call")])})
        out = await waiting_on_human(service, _registry(), now=_NOW)
        assert len(out) == 1
        assert out[0].agent_did == _AGENT
        assert out[0].channel == "ops"
        assert out[0].preview == "need a call"

    async def test_attribution_is_the_asker_across_two_agents(self) -> None:
        # Fleet-wide: the RIGHT agent is credited, not the channel's responder.
        ch = Channel(name="ops", members=[_AGENT, _AGENT_2, _HUMAN])
        service = _FakeChannels(
            {"ops": (ch, [_msg(signer_did=_AGENT_2, body="felix asks", mid="fx")])}
        )
        out = await waiting_on_human(service, _registry(), now=_NOW)
        assert [q.agent_did for q in out] == [_AGENT_2]

    async def test_empty_when_no_channels(self) -> None:
        out = await waiting_on_human(_FakeChannels({}), _registry(), now=_NOW)
        assert out == []

    async def test_scope_for_operator_hides_channels_it_is_not_in(self) -> None:
        mine = Channel(name="mine", members=[_AGENT, _HUMAN])
        theirs = Channel(name="theirs", members=[_AGENT, "did:arc:user/other"])
        service = _FakeChannels(
            {
                "mine": (mine, [_msg(signer_did=_AGENT, mid="a")]),
                "theirs": (theirs, [_msg(signer_did=_AGENT, mid="b")]),
            }
        )
        out = await waiting_on_human(
            service, _registry(), scope=OperatorScope.for_operator(_HUMAN), now=_NOW
        )
        assert [q.channel for q in out] == ["mine"]
        # The unreadable channel was never even read (visibility at the reader).
        assert "theirs" not in service.reads
