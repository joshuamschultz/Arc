"""SPEC-038 F4 — messaging_send stamps the message classification from the
sender's clearance (a floor bound to identity), so the arcteam no-write-down
gate has an honest producer instead of a defaulted-UNCLASSIFIED label.

Drives the LIVE decorator ``messaging_send`` (capabilities.py) over a real
MessagingService + roster — not a hand-built Message with an explicit label.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity
from arctrust.classification import Classification

from arcagent.modules.messaging import _bootstrap, _runtime
from arcagent.modules.messaging.capabilities import messaging_send
from tests.unit.modules.messaging.conftest import make_config_dict, make_operator_signer

pytestmark = pytest.mark.asyncio


async def _configure(tmp_path: Path, *, clearance: Classification) -> None:
    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    identity.clearance = clearance
    _runtime.configure(
        config=make_config_dict(entity_id="agent://sender", entity_name="Sender"),
        telemetry=MagicMock(),
        workspace=tmp_path,
        team_root=tmp_path / "team",
        agent_name="sender",
        identity=identity,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    await st.registry.register(
        _bootstrap.self_entity(
            entity_id="agent://sender",
            entity_name="Sender",
            handle="sender",
            identity=identity,
            roles=["executor"],
            capabilities=["task-execution"],
        )
    )
    # A CUI-cleared recipient — cannot receive a SECRET-stamped message.
    from arcteam.types import Entity, EntityType

    await st.registry.register(
        Entity(
            did="did:arc:test:agent/lo",
            handle="lo",
            id="agent://lo",
            name="Low",
            type=EntityType.AGENT,
            clearance="CUI",
        )
    )


async def test_secret_sender_message_to_cui_recipient_refused(tmp_path: Path) -> None:
    await _configure(tmp_path, clearance=Classification.SECRET)
    result = json.loads(await messaging_send(to="agent://lo", body="sensitive"))
    # The producer stamped SECRET (sender clearance); the messenger no-write-down
    # gate refused delivery to the CUI recipient (previously dormant — the
    # message defaulted to UNCLASSIFIED and always flowed).
    assert "error" in result
    assert "classification" in result["error"].lower()
    _runtime.reset()


async def test_unclassified_sender_message_delivered(tmp_path: Path) -> None:
    await _configure(tmp_path, clearance=Classification.UNCLASSIFIED)
    result = json.loads(await messaging_send(to="agent://lo", body="hello"))
    assert result.get("status") == "sent"
    _runtime.reset()
