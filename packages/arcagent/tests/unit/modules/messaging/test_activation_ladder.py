"""The activation ladder — who answers a channel post, and what it costs.

SPEC-068 D1/D4. Two properties are load-bearing and each is asserted directly
rather than inferred from a bool the caller might ignore:

* **The gate fails CLOSED.** A missing classifier, a timeout, an exception or an
  unparseable verdict all mean silence. The old gate failed open, so a broken
  model bought a full agentic turn in every member of the channel on every
  message. A cost control whose failure mode is to spend the maximum is the
  wrong shape.
* **Only a human's un-addressed post fans out.** An agent's reply is itself an
  un-addressed channel post, so without this A -> B -> A is reachable. With it
  the loop is impossible by construction, not merely bounded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from arcagent.modules.messaging import activation
from arcagent.modules.messaging.config import MessagingConfig

pytestmark = pytest.mark.asyncio

DID_ME = "did:arc:local:agent/me"
DID_OTHER = "did:arc:local:agent/other"
DID_HUMAN = "did:arc:local:user/operator"


@dataclass
class _Entity:
    did: str
    type: Any


@dataclass
class _Identity:
    did: str = DID_ME


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
    def __init__(self, later: list[Any] | None = None) -> None:
        self.later = later or []

    async def list_channel_messages(self, **_kw: Any) -> list[Any]:
        return self.later


@dataclass
class _State:
    config: MessagingConfig
    identity: Any
    registry: Any
    svc: Any
    oneshot_fn: Any = None
    agent_name: str = "me"
    telemetry: Any = None
    channel_last_woken: dict[str, float] = field(default_factory=dict)
    channel_breakers: dict[str, Any] = field(default_factory=dict)


def _state(*, classify: Any = None, entities: list[_Entity] | None = None, **cfg: Any) -> _State:
    from arcteam.types import EntityType

    default = [
        _Entity(DID_HUMAN, EntityType.USER),
        _Entity(DID_OTHER, EntityType.AGENT),
        _Entity(DID_ME, EntityType.AGENT),
    ]
    return _State(
        config=MessagingConfig(entity_id="me", entity_name="Me", entity_role="sales", **cfg),
        identity=_Identity(),
        registry=_Registry(entities if entities is not None else default),
        svc=_Svc(),
        oneshot_fn=classify,
    )


async def _classify_yes(**_kw: Any) -> str:
    return "YES"


async def _classify_no(**_kw: Any) -> str:
    return "NO"


# --------------------------------------------------------------------------
# D1a — the gate fails closed
# --------------------------------------------------------------------------


async def test_explicit_yes_wakes() -> None:
    decision = await activation.decide(_Msg(), _state(classify=_classify_yes))
    assert decision.wake and decision.reason == "relevant"


async def test_explicit_no_stays_silent() -> None:
    decision = await activation.decide(_Msg(), _state(classify=_classify_no))
    assert not decision.wake and decision.reason == "not_relevant"


@pytest.mark.parametrize("verdict", ["", "   ", "maybe", "I think so", "42"])
async def test_unparseable_verdict_stays_silent(verdict: str) -> None:
    """Anything not recognisably YES is not a decision to spend a turn."""

    async def garbled(**_kw: Any) -> str:
        return verdict

    decision = await activation.decide(_Msg(), _state(classify=garbled))
    assert not decision.wake and decision.reason == "not_relevant"


async def test_gate_exception_stays_silent() -> None:
    async def boom(**_kw: Any) -> str:
        raise RuntimeError("provider down")

    decision = await activation.decide(_Msg(), _state(classify=boom))
    assert not decision.wake and decision.reason == "gate_failed"


async def test_gate_timeout_stays_silent() -> None:
    async def hang(**_kw: Any) -> str:
        await asyncio.sleep(10)
        return "YES"

    st = _state(classify=hang, triage_timeout_seconds=0.05)
    decision = await activation.decide(_Msg(), st)
    assert not decision.wake and decision.reason == "gate_failed"


async def test_missing_classifier_stays_silent() -> None:
    decision = await activation.decide(_Msg(), _state(classify=None))
    assert not decision.wake and decision.reason == "gate_unavailable"


async def test_repeated_gate_failure_opens_the_breaker() -> None:
    """A gate that keeps failing stops being called at all (D4d)."""
    calls = 0

    async def boom(**_kw: Any) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider down")

    st = _state(classify=boom, triage_failure_threshold=2, channel_cooldown_seconds=0)
    for _ in range(2):
        assert not (await activation.decide(_Msg(), st)).wake
    assert calls == 2

    decision = await activation.decide(_Msg(), st)
    assert not decision.wake and decision.reason == "breaker_open"
    assert calls == 2, "breaker must stop the call, not merely discard its result"


# --------------------------------------------------------------------------
# D4 — loop prevention
# --------------------------------------------------------------------------


async def test_own_message_never_wakes() -> None:
    msg = _Msg(sender=DID_ME, signer_did=DID_ME)
    decision = await activation.decide(msg, _state(classify=_classify_yes))
    assert not decision.wake and decision.reason == "self"


async def test_agent_broadcast_never_fans_out() -> None:
    """An agent's un-addressed channel post wakes nobody — the loop guard."""
    msg = _Msg(sender=DID_OTHER, signer_did=DID_OTHER)
    decision = await activation.decide(msg, _state(classify=_classify_yes))
    assert not decision.wake and decision.reason == "agent_broadcast"


async def test_unknown_sender_does_not_fan_out() -> None:
    """Fail-closed on provenance: an unresolvable sender is not a human."""
    msg = _Msg(sender="did:arc:local:agent/ghost", signer_did="did:arc:local:agent/ghost")
    decision = await activation.decide(msg, _state(classify=_classify_yes))
    assert not decision.wake and decision.reason == "agent_broadcast"


async def test_hop_budget_terminates_a_mention_chain() -> None:
    msg = _Msg(hop=activation.MAX_HOP, mentions=[DID_ME])
    decision = await activation.decide(msg, _state(classify=_classify_yes))
    assert not decision.wake and decision.reason == "hop_exhausted"


async def test_mention_below_the_hop_budget_still_wakes() -> None:
    msg = _Msg(hop=activation.MAX_HOP - 1, mentions=[DID_ME])
    decision = await activation.decide(msg, _state())
    assert decision.wake and decision.reason == "mentioned"


# --------------------------------------------------------------------------
# Mention scope and bypasses (SPEC-055, preserved)
# --------------------------------------------------------------------------


async def test_mention_bypasses_the_gate_entirely() -> None:
    """The deterministic override that makes fail-closed safe to choose."""
    msg = _Msg(mentions=[DID_ME], sender=DID_OTHER, signer_did=DID_OTHER)
    decision = await activation.decide(msg, _state(classify=None))
    assert decision.wake and decision.reason == "mentioned"


async def test_message_naming_other_agents_does_not_wake_me() -> None:
    msg = _Msg(mentions=[DID_OTHER])
    decision = await activation.decide(msg, _state(classify=_classify_yes))
    assert not decision.wake and decision.reason == "addressed_to_others"


async def test_critical_always_wakes() -> None:
    msg = _Msg(priority="critical", mentions=[DID_OTHER], sender=DID_OTHER, signer_did=DID_OTHER)
    decision = await activation.decide(msg, _state(classify=None))
    assert decision.wake and decision.reason == "critical"


async def test_direct_message_wakes_without_a_gate() -> None:
    msg = _Msg(to=["agent://me"], sender=DID_OTHER, signer_did=DID_OTHER)
    decision = await activation.decide(msg, _state(classify=None))
    assert decision.wake and decision.reason == "direct"


# --------------------------------------------------------------------------
# D1c — blast radius
# --------------------------------------------------------------------------


async def test_cooldown_silences_a_rapid_second_message() -> None:
    st = _state(classify=_classify_yes)
    assert (await activation.decide(_Msg(), st)).wake
    activation.record_activation(st, "work")
    decision = await activation.decide(_Msg(id="msg_2"), st)
    assert not decision.wake and decision.reason == "cooldown"


async def test_cooldown_does_not_silence_a_mention() -> None:
    st = _state(classify=_classify_yes)
    activation.record_activation(st, "work")
    decision = await activation.decide(_Msg(mentions=[DID_ME]), st)
    assert decision.wake and decision.reason == "mentioned"


async def test_answer_cap_silences_the_third_agent() -> None:
    st = _state(classify=_classify_yes)
    st.svc.later = [
        _Msg(sender="did:arc:local:agent/a", seq=2),
        _Msg(sender="did:arc:local:agent/b", seq=3),
    ]
    decision = await activation.decide(_Msg(), st)
    assert not decision.wake and decision.reason == "answer_cap"


async def test_answer_cap_ignores_repeats_from_one_responder() -> None:
    """Two messages from the same agent are one answer, not two."""
    st = _state(classify=_classify_yes)
    st.svc.later = [
        _Msg(sender="did:arc:local:agent/a", seq=2),
        _Msg(sender="did:arc:local:agent/a", seq=3),
    ]
    decision = await activation.decide(_Msg(), st)
    assert decision.wake and decision.reason == "relevant"


async def test_answer_cap_does_not_silence_a_mention() -> None:
    st = _state(classify=_classify_yes)
    st.svc.later = [
        _Msg(sender="did:arc:local:agent/a", seq=2),
        _Msg(sender="did:arc:local:agent/b", seq=3),
    ]
    decision = await activation.decide(_Msg(mentions=[DID_ME]), st)
    assert decision.wake and decision.reason == "mentioned"


# --------------------------------------------------------------------------
# The overheard rule (7812264f) survives the rewrite
# --------------------------------------------------------------------------


async def test_unaddressed_channel_post_is_overheard() -> None:
    assert activation.is_overheard(_Msg()) is True


async def test_a_mention_is_not_overheard() -> None:
    assert activation.is_overheard(_Msg(mentions=[DID_ME])) is False


async def test_a_direct_message_is_not_overheard() -> None:
    assert activation.is_overheard(_Msg(to=["agent://me"])) is False


async def test_critical_is_not_overheard() -> None:
    assert activation.is_overheard(_Msg(priority="critical")) is False
