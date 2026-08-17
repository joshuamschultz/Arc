"""The activation ladder — who answers a channel post, and what it costs.

ADR-032. The reported failure is the acceptance test and it leads this file: six
agents and one operator in a channel, one agent holds the NNL technical
requirements, the message names nobody, and **that agent must be the one that
wakes**. The design it replaces asked each agent "is this relevant to you?" with
no tools, no memory and no retrieval, and all six said no — including the one
holding the document.

Four further properties are load-bearing, each asserted directly rather than
inferred from a bool the caller might ignore:

* **Silence is never the fallback.** When nothing ranks, a named responder still
  answers. An unanswered question is indistinguishable from a broken system.
* **An explicit address resolves before any scoring or model call.** It is the
  cheapest correct decision in the system.
* **Only a human's un-addressed post fans out.** An agent's reply is itself an
  un-addressed channel post, so without this A -> B -> A is reachable.
* **The router is O(1) in the size of the channel**, and runs only when the
  deterministic ranking was too close to decide on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from arcteam.digest import AgentDigest, DigestEntry
from arcteam.types import Channel

from arcagent.modules.messaging import activation
from arcagent.modules.messaging.config import MessagingConfig

DID_HUMAN = "did:arc:local:user/operator"

# Six agents and one operator, as reported. Exactly one filed the NNL document.
FLEET: dict[str, list[str]] = {
    "brand": ["Q3 brand voice guidelines", "logo usage rules"],
    "sales": ["pipeline review notes", "pricing sheet for Acme"],
    "ops": ["NNL technical requirements", "deployment runbook"],
    "finance": ["monthly P&L summary", "vendor invoices"],
    "hr": ["onboarding checklist", "leave policy"],
    "legal": ["master services agreement", "NDA template"],
}


def did(handle: str) -> str:
    return f"did:arc:local:agent/{handle}"


@dataclass
class _Entity:
    did: str
    type: Any


@dataclass
class _Identity:
    did: str


@dataclass
class _Msg:
    """The subset of ``arcteam.types.Message`` the ladder reads."""

    body: str = "anyone about?"
    sender: str = DID_HUMAN
    signer_did: str = DID_HUMAN
    to: list[str] = field(default_factory=lambda: ["channel://work"])
    mentions: list[str] = field(default_factory=list)
    priority: str = "normal"
    hop: int = 0
    seq: int = 1
    id: str = "msg_1"


class _Registry:
    def __init__(self, entities: list[_Entity]) -> None:
        self._entities = entities

    async def list_entities(self) -> list[_Entity]:
        return self._entities


class _Svc:
    def __init__(self, channel: Channel, later: list[Any] | None = None) -> None:
        self._channel = channel
        self.later = later or []

    async def list_channels(self) -> list[Channel]:
        return [self._channel]

    async def list_channel_messages(self, **_kw: Any) -> list[Any]:
        return self.later


class _Digests:
    def __init__(self, digests: list[AgentDigest]) -> None:
        self._digests = digests

    async def list_digests(self) -> list[AgentDigest]:
        return list(self._digests)


@dataclass
class _State:
    config: MessagingConfig
    identity: Any
    registry: Any
    svc: Any
    digests: Any
    oneshot_fn: Any = None
    agent_name: str = "me"
    telemetry: Any = None
    channel_last_woken: dict[str, float] = field(default_factory=dict)
    channel_breakers: dict[str, Any] = field(default_factory=dict)


def _digest(handle: str, titles: list[str]) -> AgentDigest:
    return AgentDigest(
        agent_did=did(handle),
        handle=handle,
        entries=[
            DigestEntry(
                artifact_id=f"{handle}-{i}",
                title=title,
                entities=[word for word in title.split() if word.isupper()],
            )
            for i, title in enumerate(titles)
        ],
    )


def _state(
    *,
    me: str = "ops",
    fleet: dict[str, list[str]] | None = None,
    responder: str = "",
    members: list[str] | None = None,
    router: Any = None,
    later: list[Any] | None = None,
    **cfg: Any,
) -> _State:
    from arcteam.types import EntityType

    roster = FLEET if fleet is None else fleet
    channel = Channel(
        name="work",
        members=members if members is not None else [DID_HUMAN, *(did(h) for h in roster)],
        responder=responder,
    )
    return _State(
        config=MessagingConfig(entity_id=me, entity_name=me, **cfg),
        identity=_Identity(did(me)),
        registry=_Registry(
            [
                _Entity(DID_HUMAN, EntityType.USER),
                *[_Entity(did(handle), EntityType.AGENT) for handle in roster],
            ]
        ),
        svc=_Svc(channel, later),
        digests=_Digests([_digest(handle, titles) for handle, titles in roster.items()]),
        oneshot_fn=router,
        agent_name=me,
    )


# --------------------------------------------------------------------------
# The reported failure
# --------------------------------------------------------------------------


class TestTheReportedFailure:
    async def test_the_agent_holding_the_document_wakes(self) -> None:
        """Six agents, nobody named, and the one with the answer is the one that runs."""
        decision = await activation.decide(
            _Msg(body="who has the technical requirements for NNL?"), _state(me="ops")
        )

        assert decision.wake
        assert decision.reason == "routed"

    @pytest.mark.parametrize("bystander", ["brand", "sales", "finance", "hr", "legal"])
    async def test_the_five_who_do_not_hold_it_stay_silent(self, bystander: str) -> None:
        """The other half of a right answer: exactly one agent replies, not six."""
        decision = await activation.decide(
            _Msg(body="who has the technical requirements for NNL?"), _state(me=bystander)
        )

        assert not decision.wake

    async def test_it_costs_no_model_call(self) -> None:
        """A clear ranking decides on its own — the router is for ties, not routine."""
        calls: list[dict[str, Any]] = []

        async def router(**kwargs: Any) -> str:
            calls.append(kwargs)
            return "ops"

        await activation.decide(
            _Msg(body="who has the technical requirements for NNL?"),
            _state(me="ops", router=router),
        )

        assert calls == []

    async def test_a_different_question_wakes_a_different_agent(self) -> None:
        """Proof the win was retrieval and not a fixed ordering."""
        chosen = await activation.decide(_Msg(body="what is our leave policy?"), _state(me="hr"))
        not_chosen = await activation.decide(
            _Msg(body="what is our leave policy?"), _state(me="ops")
        )

        assert chosen.wake
        assert not not_chosen.wake


# --------------------------------------------------------------------------
# Silence is never the fallback
# --------------------------------------------------------------------------


class TestNothingScores:
    async def test_the_named_responder_answers(self) -> None:
        decision = await activation.decide(
            _Msg(body="zzzz qqqq"), _state(me="sales", responder=did("sales"))
        )

        assert decision.wake
        assert decision.reason == "default_responder"

    async def test_nobody_else_does(self) -> None:
        decision = await activation.decide(
            _Msg(body="zzzz qqqq"), _state(me="ops", responder=did("sales"))
        )

        assert not decision.wake
        assert decision.reason == "routed_away"

    async def test_an_unconfigured_channel_still_answers(self) -> None:
        """Exactly one agent, deterministically, with no responder configured."""
        woken = [
            handle
            for handle in FLEET
            if (await activation.decide(_Msg(body="zzzz qqqq"), _state(me=handle))).wake
        ]

        assert len(woken) == 1

    async def test_the_responder_is_not_silenced_by_its_own_cooldown(self) -> None:
        """Cooldown stops one agent dominating a room; it is not a reason for silence."""
        state = _state(me="sales", responder=did("sales"))
        activation.record_activation(state, "work")

        decision = await activation.decide(_Msg(body="zzzz qqqq"), state)

        assert decision.wake

    async def test_a_room_with_no_agent_members_names_nobody(self) -> None:
        decision = await activation.decide(
            _Msg(body="zzzz qqqq"), _state(me="ops", members=[DID_HUMAN])
        )

        assert not decision.wake
        assert decision.reason == "no_responder"


# --------------------------------------------------------------------------
# Explicit address, before anything else
# --------------------------------------------------------------------------


class TestExplicitAddress:
    async def test_a_mention_wakes_its_target_without_scoring_it(self) -> None:
        """A mention beats the router: it is not ranked, it is obeyed."""
        state = _state(me="hr")
        decision = await activation.decide(
            _Msg(body="who has the technical requirements for NNL?", mentions=[did("hr")]), state
        )

        assert decision.wake
        assert decision.reason == "mentioned"

    async def test_a_mention_reaches_no_digest_and_no_model(self) -> None:
        """The cheapest correct decision in the system must not sit behind a gate."""

        class _Exploding:
            async def list_digests(self) -> list[AgentDigest]:
                raise AssertionError("routing ran before an explicit address was honoured")

        async def router(**_kw: Any) -> str:
            raise AssertionError("a model was called to confirm an explicit address")

        state = _state(me="hr", router=router)
        state.digests = _Exploding()

        assert (await activation.decide(_Msg(mentions=[did("hr")]), state)).wake

    async def test_naming_someone_else_silences_the_agent_that_would_have_ranked(self) -> None:
        decision = await activation.decide(
            _Msg(body="who has the technical requirements for NNL?", mentions=[did("hr")]),
            _state(me="ops"),
        )

        assert not decision.wake
        assert decision.reason == "addressed_to_others"

    async def test_a_mention_survives_a_cooldown(self) -> None:
        state = _state(me="hr")
        activation.record_activation(state, "work")

        assert (await activation.decide(_Msg(mentions=[did("hr")]), state)).wake


# --------------------------------------------------------------------------
# The single-call tiebreak
# --------------------------------------------------------------------------


TIED = {"ops": ["NNL technical requirements"], "sales": ["NNL technical requirements"]}


class TestRouterTiebreak:
    async def test_one_prompt_carries_every_candidate(self) -> None:
        """What the per-agent gate structurally could not do: compare candidates."""
        seen: list[str] = []

        async def router(*, system: str, **_kw: Any) -> str:
            seen.append(system)
            return "ops"

        await activation.decide(
            _Msg(body="NNL technical requirements"),
            _state(me="ops", fleet=TIED, router=router),
        )

        assert len(seen) == 1
        assert "ops" in seen[0]
        assert "sales" in seen[0]

    async def test_the_agent_the_router_names_wakes(self) -> None:
        async def router(**_kw: Any) -> str:
            return "sales"

        decision = await activation.decide(
            _Msg(body="NNL technical requirements"),
            _state(me="sales", fleet=TIED, router=router),
        )

        assert decision.wake
        assert decision.reason == "router_selected"

    async def test_the_agent_it_does_not_name_stays_silent(self) -> None:
        async def router(**_kw: Any) -> str:
            return "sales"

        decision = await activation.decide(
            _Msg(body="NNL technical requirements"),
            _state(me="ops", fleet=TIED, router=router),
        )

        assert not decision.wake
        assert decision.reason == "router_selected_other"

    async def test_a_broken_router_leaves_the_ranking_standing_rather_than_silence(self) -> None:
        """A router that cannot decide must not turn a ranked message into an unanswered one."""

        async def router(**_kw: Any) -> str:
            raise RuntimeError("model down")

        woken = [
            handle
            for handle in TIED
            if (
                await activation.decide(
                    _Msg(body="NNL technical requirements"),
                    _state(me=handle, fleet=TIED, router=router),
                )
            ).wake
        ]

        assert len(woken) == 1

    async def test_repeated_router_failure_opens_the_breaker(self) -> None:
        async def router(**_kw: Any) -> str:
            raise RuntimeError("model down")

        state = _state(me="ops", fleet=TIED, router=router, route_failure_threshold=2)
        for _ in range(3):
            await activation.decide(_Msg(body="NNL technical requirements"), state)

        assert (await activation.decide(_Msg(body="NNL requirements"), state)).reason == (
            "breaker_open"
        )

    async def test_no_router_bound_still_leaves_the_ranking_standing(self) -> None:
        decision = await activation.decide(
            _Msg(body="NNL technical requirements"), _state(me="ops", fleet=TIED)
        )

        assert decision.reason == "routed_tie_unbroken"


# --------------------------------------------------------------------------
# Loop guards
# --------------------------------------------------------------------------


class TestLoopGuards:
    async def test_an_agents_reply_does_not_wake_another_agent(self) -> None:
        """The strong guard: A -> B -> A is impossible, not merely bounded."""
        reply = _Msg(body="NNL technical requirements are with me", sender=did("sales"))
        reply.signer_did = did("sales")

        decision = await activation.decide(reply, _state(me="ops"))

        assert not decision.wake
        assert decision.reason == "agent_broadcast"

    async def test_an_agent_does_not_wake_on_its_own_post(self) -> None:
        own = _Msg(sender=did("ops"), signer_did=did("ops"))

        assert (await activation.decide(own, _state(me="ops"))).reason == "self"

    async def test_an_unregistered_sender_does_not_fan_out(self) -> None:
        stranger = _Msg(sender="did:arc:unknown", signer_did="did:arc:unknown")

        assert not (await activation.decide(stranger, _state(me="ops"))).wake

    async def test_the_hop_budget_ends_an_addressed_chain(self) -> None:
        exhausted = _Msg(mentions=[did("ops")], hop=activation.MAX_HOP)

        assert (await activation.decide(exhausted, _state(me="ops"))).reason == "hop_exhausted"

    async def test_critical_priority_reaches_every_member(self) -> None:
        urgent = _Msg(body="zzzz", priority="critical")

        assert (await activation.decide(urgent, _state(me="hr"))).wake

    async def test_a_direct_message_needs_no_routing(self) -> None:
        dm = _Msg(to=[did("ops")])

        assert (await activation.decide(dm, _state(me="ops"))).reason == "direct"


# --------------------------------------------------------------------------
# Bounds on a routed wake
# --------------------------------------------------------------------------


class TestBounds:
    async def test_a_routed_agent_in_cooldown_stays_silent(self) -> None:
        state = _state(me="ops")
        activation.record_activation(state, "work")

        decision = await activation.decide(_Msg(body="NNL technical requirements"), state)

        assert not decision.wake
        assert decision.reason == "cooldown"

    async def test_a_routed_agent_stays_silent_once_the_room_has_answered(self) -> None:
        answers = [_Msg(sender=did("a")), _Msg(sender=did("b"))]

        decision = await activation.decide(
            _Msg(body="NNL technical requirements"), _state(me="ops", later=answers)
        )

        assert decision.reason == "answer_cap"

    async def test_an_unreadable_index_does_not_fan_out(self) -> None:
        class _Broken:
            async def list_digests(self) -> list[AgentDigest]:
                raise RuntimeError("store down")

        state = _state(me="ops")
        state.digests = _Broken()

        decision = await activation.decide(_Msg(body="NNL requirements"), state)

        assert not decision.wake
        assert decision.reason == "routing_unavailable"

    async def test_the_decision_names_the_candidates_it_ranked(self) -> None:
        """A wrong route must leave evidence; self-assessment never did."""
        decision = await activation.decide(
            _Msg(body="who has the technical requirements for NNL?"), _state(me="ops")
        )

        assert "ops" in decision.candidates


class TestOverheard:
    def test_an_un_addressed_channel_post_is_answered_but_not_retained(self) -> None:
        assert activation.is_overheard(_Msg())

    def test_a_mention_is_addressed_and_therefore_retained(self) -> None:
        assert not activation.is_overheard(_Msg(mentions=[did("ops")]))

    def test_a_direct_message_is_retained(self) -> None:
        assert not activation.is_overheard(_Msg(to=[did("ops")]))
