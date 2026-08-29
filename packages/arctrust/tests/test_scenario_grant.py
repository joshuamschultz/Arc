"""Scenario grants — approve a recurring automated scenario once, not every call.

A one-shot ``ApprovalGrant`` binds to a call hash, so an unattended workflow can
never finish: every night's Jira issue has different arguments, so it is a
different call, so it re-prompts a human who is asleep. A ``ScenarioGrant``
binds instead to the five facts that make an action *the same scenario* every
time it recurs — who acts, which tool, which forbidden composition is waived,
which automated driver runs it, and which external connection it reaches.

The security stance that keeps this enterprise-safe is ``origin``: a grant only
matches a non-interactive driver (a workflow, a schedule). Free-form chat
carries no origin, so an ad-hoc request still needs per-call approval. The
waiver follows the automation, never the agent at large.
"""

from __future__ import annotations

import pytest

from arctrust.identity import AgentIdentity
from arctrust.policy import (
    GlobalLayer,
    PolicyContext,
    ScenarioGrant,
    ToolCall,
    scenario_key,
    sign_scenario_grant,
    verify_scenario_grant,
)

_COMPOSITION = frozenset({"external_comms", "private_data"})


def _call(
    *,
    agent_did: str,
    tool_name: str = "jira_create_issue",
    arguments: dict[str, object] | None = None,
    capability_tags: frozenset[str] = frozenset({"external_comms"}),
) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments=arguments or {"summary": "one"},
        agent_did=agent_did,
        session_id="task:wf-run-1269cdcfc246-create_jira_tasks-0",
        classification="UNCLASSIFIED",
        capability_tags=capability_tags,
    )


def _ctx(
    *,
    grants: tuple[ScenarioGrant, ...] = (),
    origin: str | None = "workflow:nightly-meeting-ingest",
    connection: str | None = "jira",
    session_capabilities: frozenset[str] = frozenset({"private_data"}),
) -> PolicyContext:
    return PolicyContext(
        tier="enterprise",
        policy_version="v1",
        bundle_age_seconds=1.0,
        session_capabilities=session_capabilities,
        origin=origin,
        connection=connection,
        scenario_grants=grants,
    )


def _layer() -> GlobalLayer:
    return GlobalLayer(deny_rules={}, forbidden_compositions=[_COMPOSITION])


def _grant(operator: AgentIdentity, agent_did: str, **overrides: object) -> ScenarioGrant:
    fields: dict[str, object] = {
        "agent_did": agent_did,
        "tool_name": "jira_create_issue",
        "composition": _COMPOSITION,
        "origin": "workflow:nightly-meeting-ingest",
        "connection": "jira",
    }
    fields.update(overrides)
    return sign_scenario_grant(operator=operator, **fields)  # type: ignore[arg-type]


async def test_a_matching_scenario_allows_a_call_that_would_otherwise_be_denied() -> None:
    agent = AgentIdentity.generate("test", "sales")
    operator = AgentIdentity.generate("test", "operator")
    grant = _grant(operator, agent.did)

    denied = await _layer().evaluate(_call(agent_did=agent.did), _ctx())
    allowed = await _layer().evaluate(_call(agent_did=agent.did), _ctx(grants=(grant,)))

    assert denied.outcome == "deny"
    assert allowed.outcome == "allow"


async def test_the_same_scenario_holds_across_different_arguments() -> None:
    """The whole point: tonight's issue and tomorrow's are one scenario."""
    agent = AgentIdentity.generate("test", "sales")
    operator = AgentIdentity.generate("test", "operator")
    grant = _grant(operator, agent.did)

    first = await _layer().evaluate(
        _call(agent_did=agent.did, arguments={"summary": "monday"}), _ctx(grants=(grant,))
    )
    second = await _layer().evaluate(
        _call(agent_did=agent.did, arguments={"summary": "tuesday"}), _ctx(grants=(grant,))
    )

    assert first.outcome == "allow" and second.outcome == "allow"


@pytest.mark.parametrize(
    "context_change",
    [
        {"origin": "workflow:some-other-workflow"},
        {"origin": None},
        {"connection": "confluence"},
    ],
)
async def test_a_grant_does_not_travel_outside_its_scenario(
    context_change: dict[str, object],
) -> None:
    """A different driver or connection is a different scenario — still gated.

    ``origin=None`` is the interactive case: a human typing in chat gets no
    silent waiver from an automation grant.
    """
    agent = AgentIdentity.generate("test", "sales")
    operator = AgentIdentity.generate("test", "operator")
    grant = _grant(operator, agent.did)

    decision = await _layer().evaluate(
        _call(agent_did=agent.did), _ctx(grants=(grant,), **context_change)
    )

    assert decision.outcome == "deny"


async def test_a_grant_does_not_travel_to_another_agent_or_tool() -> None:
    agent = AgentIdentity.generate("test", "sales")
    other = AgentIdentity.generate("test", "marketer")
    operator = AgentIdentity.generate("test", "operator")
    grant = _grant(operator, agent.did)

    wrong_agent = await _layer().evaluate(_call(agent_did=other.did), _ctx(grants=(grant,)))
    wrong_tool = await _layer().evaluate(
        _call(agent_did=agent.did, tool_name="jira_add_comment"), _ctx(grants=(grant,))
    )

    assert wrong_agent.outcome == "deny"
    assert wrong_tool.outcome == "deny"


async def test_an_agent_can_never_grant_its_own_scenario() -> None:
    """ASI09 — self-approval is the one thing a signature must never buy."""
    agent = AgentIdentity.generate("test", "sales")
    self_signed = _grant(agent, agent.did)

    decision = await _layer().evaluate(_call(agent_did=agent.did), _ctx(grants=(self_signed,)))

    assert decision.outcome == "deny"
    assert (
        verify_scenario_grant(
            self_signed,
            agent_did=agent.did,
            tool_name="jira_create_issue",
            composition=_COMPOSITION,
            origin="workflow:nightly-meeting-ingest",
            connection="jira",
        )
        is False
    )


async def test_a_tampered_grant_is_refused() -> None:
    agent = AgentIdentity.generate("test", "sales")
    operator = AgentIdentity.generate("test", "operator")
    grant = _grant(operator, agent.did)
    forged = grant.model_copy(update={"tool_name": "jira_add_comment"})

    decision = await _layer().evaluate(
        _call(agent_did=agent.did, tool_name="jira_add_comment"), _ctx(grants=(forged,))
    )

    assert decision.outcome == "deny"


async def test_a_grant_only_waives_the_composition_it_names() -> None:
    """Approving external comms must not silently waive an unrelated combination."""
    agent = AgentIdentity.generate("test", "sales")
    operator = AgentIdentity.generate("test", "operator")
    grant = _grant(operator, agent.did, composition=frozenset({"external_comms"}))

    decision = await _layer().evaluate(_call(agent_did=agent.did), _ctx(grants=(grant,)))

    assert decision.outcome == "deny"


def test_the_scenario_key_is_stable_and_order_independent() -> None:
    """The key is what an operator sees and what storage dedups on."""
    first = scenario_key(
        agent_did="did:arc:test:sales",
        tool_name="jira_create_issue",
        composition=frozenset({"external_comms", "private_data"}),
        origin="workflow:nightly-meeting-ingest",
        connection="jira",
    )
    second = scenario_key(
        agent_did="did:arc:test:sales",
        tool_name="jira_create_issue",
        composition=frozenset({"private_data", "external_comms"}),
        origin="workflow:nightly-meeting-ingest",
        connection="jira",
    )

    assert first == second
    assert "jira_create_issue" in first
