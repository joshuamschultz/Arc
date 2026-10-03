"""Interactive standing grants — "Always allow" on a trifecta approval (2026-10-03).

The operator approves a combination once and it stays approved. The grant is a
:class:`ScenarioGrant` whose ``origin`` is :data:`INTERACTIVE_ORIGIN` and whose
``connection`` is the destination class of the egress that was approved. A later
call by the same agent is covered when every leg it holds (session + call) sits
inside the approved composition AND any egress it adds goes to the approved
destination through the approved verb. Federal never honours one.
"""

from __future__ import annotations

import pytest

from arctrust.identity import AgentIdentity
from arctrust.policy import (
    INTERACTIVE_ORIGIN,
    ScenarioGrant,
    scenario_grant_from_wire,
    scenario_grant_to_wire,
    sign_scenario_grant,
    verify_interactive_grant,
    verify_scenario_grant,
)

_TRIFECTA = frozenset({"private_data", "external_comms", "untrusted_input"})


def _grant(operator: AgentIdentity, agent_did: str, **overrides: object) -> ScenarioGrant:
    fields: dict[str, object] = {
        "agent_did": agent_did,
        "tool_name": "dropbox_upload",
        "composition": _TRIFECTA,
        "origin": INTERACTIVE_ORIGIN,
        "connection": "personal_dropbox",
    }
    fields.update(overrides)
    return sign_scenario_grant(operator=operator, **fields)  # type: ignore[arg-type]  # test kwargs


def _covers(grant: ScenarioGrant, agent_did: str, **overrides: object) -> bool:
    fields: dict[str, object] = {
        "agent_did": agent_did,
        "tool_name": "dropbox_upload",
        "legs": _TRIFECTA,
        "destination": "personal_dropbox",
        "tier": "personal",
    }
    fields.update(overrides)
    return verify_interactive_grant(grant, **fields)  # type: ignore[arg-type]  # test kwargs


@pytest.fixture
def operator() -> AgentIdentity:
    return AgentIdentity.generate(org="operator", agent_type="approver")


@pytest.fixture
def agent() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="executor")


def test_same_egress_to_same_destination_is_covered(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    assert _covers(_grant(operator, agent.did), agent.did)


def test_call_adding_no_egress_is_covered_by_composition(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    grant = _grant(operator, agent.did)
    assert _covers(grant, agent.did, tool_name="slack_search", destination=None)
    assert _covers(grant, agent.did, tool_name="bash", destination=None)


def test_narrower_leg_set_is_covered(operator: AgentIdentity, agent: AgentIdentity) -> None:
    grant = _grant(operator, agent.did)
    assert _covers(grant, agent.did, legs=frozenset({"external_comms"}))


def test_wider_leg_set_is_not_covered(operator: AgentIdentity, agent: AgentIdentity) -> None:
    grant = _grant(operator, agent.did, composition=frozenset({"external_comms", "private_data"}))
    assert not _covers(grant, agent.did)


def test_new_destination_is_not_covered(operator: AgentIdentity, agent: AgentIdentity) -> None:
    grant = _grant(operator, agent.did)
    assert not _covers(grant, agent.did, destination="work_dropbox")


def test_other_egress_verb_is_not_covered(operator: AgentIdentity, agent: AgentIdentity) -> None:
    grant = _grant(operator, agent.did)
    assert not _covers(grant, agent.did, tool_name="dropbox_delete")


def test_grant_earned_without_egress_never_covers_egress(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    grant = _grant(operator, agent.did, tool_name="slack_search", connection="")
    assert _covers(grant, agent.did, tool_name="read", destination=None)
    assert not _covers(grant, agent.did, tool_name="slack_search", destination="")
    assert not _covers(grant, agent.did, destination="personal_dropbox")


def test_other_agent_is_not_covered(operator: AgentIdentity, agent: AgentIdentity) -> None:
    other = AgentIdentity.generate(org="local", agent_type="executor")
    assert not _covers(_grant(operator, agent.did), other.did)


def test_federal_never_honours_a_standing_grant(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    assert not _covers(_grant(operator, agent.did), agent.did, tier="federal")


def test_self_signed_grant_is_refused(agent: AgentIdentity) -> None:
    assert not _covers(_grant(agent, agent.did), agent.did)


def test_tampered_grant_is_refused(operator: AgentIdentity, agent: AgentIdentity) -> None:
    grant = _grant(operator, agent.did)
    widened = grant.model_copy(update={"connection": "work_dropbox"})
    assert not _covers(widened, agent.did, destination="work_dropbox")


def test_unsigned_grant_is_refused(operator: AgentIdentity, agent: AgentIdentity) -> None:
    grant = _grant(operator, agent.did).model_copy(update={"signature": b""})
    assert not _covers(grant, agent.did)


def test_automation_grant_is_not_an_interactive_grant(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    grant = _grant(operator, agent.did, origin="workflow:nightly")
    assert not _covers(grant, agent.did)


def test_interactive_grant_never_satisfies_the_automation_path(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    grant = _grant(operator, agent.did)
    assert not verify_scenario_grant(
        grant,
        agent_did=agent.did,
        tool_name="dropbox_upload",
        composition=_TRIFECTA,
        origin=INTERACTIVE_ORIGIN,
        connection="personal_dropbox",
    )


def test_wire_round_trip_keeps_the_signature_valid(
    operator: AgentIdentity, agent: AgentIdentity
) -> None:
    grant = _grant(operator, agent.did)
    wire = scenario_grant_to_wire(grant)
    assert all(isinstance(value, str | list) for value in wire.values())
    assert _covers(scenario_grant_from_wire(wire), agent.did)
