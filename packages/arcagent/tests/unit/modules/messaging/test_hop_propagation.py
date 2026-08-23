"""A reply carries one hop more than the message that caused it (SPEC-068 D4b).

The predicate half of the hop budget is easy and is covered in
``test_activation_ladder.py``. This file covers the half that actually makes it
work: the counter has to be *set* on the way out. A hop budget that every sender
leaves at zero is a guard that never fires — the same "correct predicate, dead
activating wiring" shape this repo keeps finding, and the reason ``7812264f``
asserted the delivery call rather than the predicate.

It also pins that ``hop`` is inside the signature. A loop guard an adversary can
clear is not a guard, so a message whose hop is edited in transit must fail
verification exactly as an edited body does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.core import turn_context
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import messaging_send

pytestmark = pytest.mark.asyncio


async def _configure(tmp_path: Path) -> Any:
    from arcteam import composition as bootstrap

    ident = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="Me"),
        workspace=tmp_path,
        identity=ident,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    await st.registry.register(
        bootstrap.self_entity(
            entity_id="agent://me",
            entity_name="Me",
            handle="me",
            identity=ident,
            roles=[],
            capabilities=[],
        )
    )
    return st


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    turn_context.set_inbound_hop(0)
    yield
    turn_context.set_inbound_hop(0)
    _runtime.reset()


async def test_reply_from_a_human_driven_turn_is_hop_one(tmp_path: Path) -> None:
    st = await _configure(tmp_path)
    turn_context.set_inbound_hop(0)

    await messaging_send(to="agent://me", body="on it")

    msgs = await st.svc.poll("arc.agent.me", "agent://me")
    assert [m.hop for m in msgs] == [1]


async def test_reply_from_an_agent_driven_turn_increments(tmp_path: Path) -> None:
    """The chain deepens: a reply to a hop-1 message is hop 2, which stops it."""
    st = await _configure(tmp_path)
    turn_context.set_inbound_hop(1)

    await messaging_send(to="agent://me", body="passing it on")

    msgs = await st.svc.poll("arc.agent.me", "agent://me")
    assert [m.hop for m in msgs] == [2]


async def test_hop_is_covered_by_the_signature() -> None:
    """Editing hop in transit must invalidate the envelope, like editing a body."""
    from arcteam.crypto import sign_message, verify_message
    from arcteam.types import Message
    from arctrust import generate_keypair

    kp = generate_keypair()
    msg = Message(sender="agent://a", to=["channel://work"], body="hello", hop=0)
    msg.signer_did = "did:arc:local:agent/a"
    msg.nonce = "n1"
    sign_message(msg, kp.private_key)
    assert verify_message(msg, kp.public_key)

    msg.hop = 99
    assert not verify_message(msg, kp.public_key), "hop must be inside the signature"
