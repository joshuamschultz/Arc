"""The anti-pile-on filter and the deferred sweep (ADR-032).

Two mechanisms that bracket the routing decision on either side.

**Before**: an agent cannot see another agent's un-addressed post, in anything
it reads. That ends pile-on and acknowledgement loops by construction rather
than by budget — an agent that cannot see a reply cannot reply to it.

**After**: a scheduled pass picks up questions the immediate route missed. Every
reason the fast path stays silent is bounded and deliberate, and every one of
them still leaves a human looking at an unanswered question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from arcteam.types import Channel

from arcagent.modules.messaging import activation, sweep
from arcagent.modules.messaging.config import MessagingConfig

HUMAN = "did:arc:local:user/operator"
ME = "did:arc:local:agent/me"
PEER = "did:arc:local:agent/peer"


@dataclass
class _Entity:
    did: str
    type: Any


@dataclass
class _Msg:
    sender: str = HUMAN
    signer_did: str = ""
    body: str = "who has the NNL requirements?"
    mentions: list[str] = field(default_factory=list)
    id: str = "m1"
    seq: int = 1
    ts: str = ""

    def __post_init__(self) -> None:
        self.signer_did = self.signer_did or self.sender
        self.ts = self.ts or datetime.now(UTC).isoformat()


def _old(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------------
# The context filter
# --------------------------------------------------------------------------


PEERS = {PEER}
IDENTITY = SimpleNamespace(did=ME)


class TestContextFilter:
    def test_a_teammates_un_addressed_post_is_hidden(self) -> None:
        """The mechanism: what an agent cannot see, it cannot pile onto."""
        assert activation.hidden_from_context(_Msg(sender=PEER), IDENTITY, PEERS)

    def test_a_humans_message_is_always_visible(self) -> None:
        """Filtering the work would be worse than the pile-on it prevents."""
        assert not activation.hidden_from_context(_Msg(sender=HUMAN), IDENTITY, PEERS)

    def test_this_agents_own_message_is_visible(self) -> None:
        assert not activation.hidden_from_context(_Msg(sender=ME), IDENTITY, PEERS)

    def test_a_teammate_that_names_this_agent_is_visible(self) -> None:
        """An explicit address is a request, not chatter."""
        message = _Msg(sender=PEER, mentions=[ME])

        assert not activation.hidden_from_context(message, IDENTITY, PEERS)

    def test_a_teammate_that_names_someone_else_is_hidden(self) -> None:
        message = _Msg(sender=PEER, mentions=["did:arc:local:agent/third"])

        assert activation.hidden_from_context(message, IDENTITY, PEERS)

    def test_it_reads_the_signer_over_the_claimed_sender(self) -> None:
        """``sender`` is a field; ``signer_did`` is what the signature binds."""
        forged = _Msg(sender=HUMAN, signer_did=PEER)

        assert activation.hidden_from_context(forged, IDENTITY, PEERS)


class TestResolvingThePeerSet:
    async def test_it_lists_every_agent_but_this_one(self) -> None:
        from arcteam.types import EntityType

        st = SimpleNamespace(
            identity=IDENTITY,
            registry=SimpleNamespace(
                list_entities=_returning(
                    [
                        _Entity(HUMAN, EntityType.USER),
                        _Entity(ME, EntityType.AGENT),
                        _Entity(PEER, EntityType.AGENT),
                    ]
                )
            ),
        )

        assert await activation.other_agent_dids(st) == {PEER}

    async def test_an_unreadable_roster_hides_nothing(self) -> None:
        """Fail-open here: hiding on an unknown roster would blank out the work."""

        async def _boom() -> list[Any]:
            raise RuntimeError("registry down")

        st = SimpleNamespace(identity=IDENTITY, registry=SimpleNamespace(list_entities=_boom))

        assert await activation.other_agent_dids(st) == set()


def _returning(value: Any) -> Any:
    async def _call() -> Any:
        return value

    return _call


# --------------------------------------------------------------------------
# The deferred sweep
# --------------------------------------------------------------------------


class TestWhatCountsAsMissed:
    def test_an_old_question_nobody_answered(self) -> None:
        messages = [_Msg(id="q", ts=_old(10))]

        assert [m.id for m in sweep.unanswered(messages, humans={HUMAN}, older_than=180)] == ["q"]

    def test_a_question_an_agent_answered_is_not_missed(self) -> None:
        """The reply check must see the agents, or every answered question is swept again."""
        messages = [_Msg(id="q", ts=_old(10)), _Msg(id="a", sender=PEER, ts=_old(9))]

        assert sweep.unanswered(messages, humans={HUMAN}, older_than=180) == []

    def test_an_agents_own_post_is_never_a_question(self) -> None:
        messages = [_Msg(id="chatter", sender=PEER, ts=_old(10))]

        assert sweep.unanswered(messages, humans={HUMAN}, older_than=180) == []

    def test_a_question_still_inside_its_grace_window_waits(self) -> None:
        """Long enough that a normal reply lands first, or the sweep races the fast path."""
        assert sweep.unanswered([_Msg(id="q")], humans={HUMAN}, older_than=180) == []

    def test_an_addressed_question_is_left_to_the_agent_it_named(self) -> None:
        """Sweeping a mention would produce a second answer, not a first one."""
        messages = [_Msg(id="q", ts=_old(10), mentions=[PEER])]

        assert sweep.unanswered(messages, humans={HUMAN}, older_than=180) == []

    def test_the_asker_repeating_themselves_is_not_an_answer(self) -> None:
        messages = [_Msg(id="q", ts=_old(10)), _Msg(id="q2", ts=_old(9))]

        assert [m.id for m in sweep.unanswered(messages, humans={HUMAN}, older_than=180)] == [
            "q",
            "q2",
        ]

    def test_an_unreadable_timestamp_waits_rather_than_sweeping(self) -> None:
        assert (
            sweep.unanswered([_Msg(id="q", ts="not a date")], humans={HUMAN}, older_than=180) == []
        )


@dataclass
class _SweepState:
    config: MessagingConfig
    identity: Any
    registry: Any
    svc: Any
    swept: set[str] = field(default_factory=set)


def _state(
    *, messages: list[Any] | None = None, responder: str = ME, members: list[str] | None = None
) -> _SweepState:
    from arcteam.types import EntityType

    channel = Channel(
        name="work",
        members=members if members is not None else [HUMAN, ME, PEER],
        responder=responder,
    )
    return _SweepState(
        config=MessagingConfig(entity_id="me", entity_name="me"),
        identity=IDENTITY,
        registry=SimpleNamespace(
            list_entities=_returning(
                [
                    _Entity(HUMAN, EntityType.USER),
                    _Entity(ME, EntityType.AGENT),
                    _Entity(PEER, EntityType.AGENT),
                ]
            )
        ),
        svc=SimpleNamespace(
            list_channels=_returning([channel]),
            list_channel_messages=_channel_messages(messages or [_Msg(id="q", ts=_old(10))]),
        ),
    )


def _channel_messages(messages: list[Any]) -> Any:
    async def _call(**_kw: Any) -> list[Any]:
        return messages

    return _call


class TestTheSweepPass:
    async def test_the_responder_picks_up_a_missed_question(self) -> None:
        woken: list[Any] = []

        picked = await sweep.run_once(_state(), _record(woken))

        assert picked == 1
        assert woken[0][0].id == "q"

    async def test_an_agent_that_is_not_the_responder_picks_up_nothing(self) -> None:
        """Deterministic, so exactly one agent acts with no coordination."""
        woken: list[Any] = []

        assert await sweep.run_once(_state(responder=PEER), _record(woken)) == 0

    async def test_a_channel_this_agent_is_not_in_is_skipped(self) -> None:
        woken: list[Any] = []

        assert await sweep.run_once(_state(members=[HUMAN, PEER]), _record(woken)) == 0

    async def test_a_message_is_never_picked_up_twice(self) -> None:
        woken: list[Any] = []
        state = _state()

        await sweep.run_once(state, _record(woken))
        await sweep.run_once(state, _record(woken))

        assert len(woken) == 1

    async def test_a_failed_wake_is_not_retried_forever(self) -> None:
        """A backstop that retries a failing wake every pass is a runaway."""
        attempts: list[Any] = []

        async def _explode(message: Any, _channel: str) -> None:
            attempts.append(message)
            raise RuntimeError("agent busy")

        state = _state()
        await sweep.run_once(state, _explode)
        await sweep.run_once(state, _explode)

        assert len(attempts) == 1

    async def test_one_pass_is_bounded(self) -> None:
        woken: list[Any] = []
        many = [_Msg(id=f"q{i}", ts=_old(10)) for i in range(10)]
        state = _state(messages=many)

        picked = await sweep.run_once(state, _record(woken))

        assert picked == state.config.sweep_max_per_tick

    async def test_disabling_it_stops_every_pass(self) -> None:
        woken: list[Any] = []
        state = _state()
        state.config = state.config.model_copy(update={"sweep_enabled": False})

        assert await sweep.run_once(state, _record(woken)) == 0

    async def test_an_unreadable_channel_list_does_not_break_the_loop(self) -> None:
        woken: list[Any] = []
        state = _state()

        async def _boom() -> list[Any]:
            raise RuntimeError("bus down")

        state.svc.list_channels = _boom

        assert await sweep.run_once(state, _record(woken)) == 0

    async def test_an_agents_own_post_is_never_swept(self) -> None:
        """Only a human's question is a question."""
        woken: list[Any] = []
        state = _state(messages=[_Msg(id="chatter", sender=PEER, ts=_old(10))])

        assert await sweep.run_once(state, _record(woken)) == 0


def _record(sink: list[Any]) -> Any:
    async def _call(message: Any, channel: str) -> None:
        sink.append((message, channel))

    return _call


@pytest.mark.parametrize("attribute", ["find_missed", "run_once", "unanswered"])
def test_the_sweep_exposes_its_policy_for_inspection(attribute: str) -> None:
    assert hasattr(sweep, attribute)
