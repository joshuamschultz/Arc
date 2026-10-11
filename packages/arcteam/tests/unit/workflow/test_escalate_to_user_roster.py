"""REQ-622 — ``escalate_to`` names a registered human user, at authoring AND run start.

An escalation hands a human decision to whoever it names. Naming an agent would
let a model approve its own run (ASI09), so an ``escalate_to`` DID must be a
registered, active ``user`` entity:

* the validator refuses an agent or stranger DID against a known user roster,
  and an EMPTY roster refuses every DID (fail closed). ``users=None`` means "no
  roster was available to this author" and defers to run start;
* an ``escalate_to = "role:X"`` must be a role at least one active human USER
  holds — a role only an agent holds is refused. Approver roles keep their rule
  (any active member's role);
* the fleet fills that roster from the team registry for agent authoring;
* run start asks the live registry and refuses any DID that is not a user, and
  any escalation role no user holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import AgentIdentity
from arctrust.signer import InProcessSigner

from arcteam.agent_fleet import ArcTeamFleet, FleetDirectoryAdapter
from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityStatus, EntityType
from arcteam.workflow import KnownReferences, parse_definition, validate_definition
from arcteam.workflow.runner import WorkflowRunError
from arcteam.workflow.stores import RegistryOwnerResolver

from ._human_steps import ALICE, CAROL, Clock, Harness, approval_flow
from .conftest import SALES_DID, Registry, minimal_document

USER_DID = "did:arc:telegram:4242"
AGENT_DID = "did:arc:local:agent/1111aaaa"


def _escalating(escalate_to: str) -> dict[str, Any]:
    document = minimal_document()
    document["node"].append(
        {
            "id": "approve",
            "kind": "gate",
            "gate": "human:approve",
            "needs": ["b"],
            "timeout_s": 3600,
            "on_timeout": "escalate",
            "escalate_to": escalate_to,
        }
    )
    return document


def _escalation_issues(document: dict[str, Any], known: KnownReferences) -> list[Any]:
    issues = validate_definition(parse_definition(document), known=known)
    return [i for i in issues if i.field == "escalate_to"]


# -- validator -------------------------------------------------------------------


def test_validator_refuses_an_agent_did_against_a_real_user_roster() -> None:
    known = KnownReferences(agents=frozenset({"@sales"}), users=frozenset({USER_DID}))

    (issue,) = _escalation_issues(_escalating(AGENT_DID), known)

    assert issue.node_id == "approve"
    assert issue.observed == AGENT_DID
    assert issue.admissible == (USER_DID,)


def test_validator_refuses_a_did_when_the_user_roster_is_empty() -> None:
    """A roster that was read and holds no user leaves nobody to escalate to."""
    known = KnownReferences(agents=frozenset({"@sales"}), users=frozenset())

    (issue,) = _escalation_issues(_escalating(USER_DID), known)

    assert issue.observed == USER_DID
    assert issue.admissible == ()


def test_validator_with_no_user_roster_defers_the_did_check_to_run_start() -> None:
    """An author with no team bus has no roster; run start is the backstop."""
    known = KnownReferences(agents=frozenset({"@sales"}))

    assert _escalation_issues(_escalating(AGENT_DID), known) == []


def test_validator_refuses_an_escalation_role_only_an_agent_holds() -> None:
    known = KnownReferences(
        agents=frozenset({"@sales"}),
        roles=frozenset({"supervisor", "sales"}),
        users=frozenset({USER_DID}),
        user_roles=frozenset({"supervisor"}),
    )

    (issue,) = _escalation_issues(_escalating("role:sales"), known)

    assert issue.observed == "role:sales"
    assert issue.admissible == ("role:supervisor",)


def test_validator_accepts_an_escalation_role_a_user_holds() -> None:
    """Approvers keep today's rule: an agent-held role is still a declared role."""
    known = KnownReferences(
        agents=frozenset({"@sales"}),
        roles=frozenset({"supervisor", "sales"}),
        users=frozenset({USER_DID}),
        user_roles=frozenset({"supervisor"}),
    )
    document = _escalating("role:supervisor")
    document["node"][-1]["approvers"] = ["role:sales"]

    assert validate_definition(parse_definition(document), known=known) == ()


def test_validator_with_an_empty_user_role_roster_refuses_every_escalation_role() -> None:
    known = KnownReferences(
        agents=frozenset({"@sales"}), users=frozenset(), user_roles=frozenset()
    )

    (issue,) = _escalation_issues(_escalating("role:supervisor"), known)

    assert issue.admissible == ()


# -- the registry roster -----------------------------------------------------------


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    agent = AgentIdentity.generate(org="local", agent_type="agent")
    for did, handle, kind, status, roles in (
        (USER_DID, "josh", EntityType.USER, EntityStatus.active, ["supervisor"]),
        ("did:arc:telegram:9", "gone", EntityType.USER, EntityStatus.revoked, ["finance"]),
        (agent.did, "sales", EntityType.AGENT, EntityStatus.active, ["sales"]),
    ):
        await registry.register(
            Entity(
                did=did,
                handle=handle,
                id=f"x://{handle}",
                name=handle,
                type=kind,
                status=status,
                roles=roles,
            )
        )
    return registry


async def test_registry_roster_knows_only_active_users(registry: EntityRegistry) -> None:
    resolver = RegistryOwnerResolver(registry)
    agent_did = next(e.did for e in await registry.list_entities() if e.handle == "sales")

    assert await resolver.is_user(USER_DID) is True
    assert await resolver.is_user(agent_did) is False
    assert await resolver.is_user("did:arc:telegram:9") is False
    assert await resolver.is_user("did:arc:telegram:1") is False
    assert await resolver.is_user("josh") is False


async def test_registry_user_roles_are_only_active_users_roles(registry: EntityRegistry) -> None:
    resolver = RegistryOwnerResolver(registry)

    assert await resolver.user_roles() == frozenset({"supervisor"})
    assert await resolver.declared_roles() == frozenset({"supervisor", "sales"})


async def test_fleet_directory_lists_only_active_users_and_their_roles(
    registry: EntityRegistry,
) -> None:
    directory = FleetDirectoryAdapter(registry)

    assert await directory.list_users() == {USER_DID: frozenset({"supervisor"})}


async def test_fleet_control_plane_validates_escalate_to_against_the_user_roster(
    tmp_path: Path,
) -> None:
    opened = ArcTeamFleet().open_control_plane(
        root=tmp_path,
        tier="personal",
        operator_public_key=None,
        audit=None,
        known_agents=lambda: frozenset({"@sales"}),
        known_users=lambda: {USER_DID: frozenset({"supervisor"})},
        runner=object(),
        runs=object(),
    )
    assert opened is not None
    plane, _definitions = opened

    refused_did = await plane.create(_escalating(AGENT_DID), actor_did=USER_DID)
    refused_role = await plane.create(_escalating("role:sales"), actor_did=USER_DID)
    accepted = await plane.create(_escalating(USER_DID), actor_did=USER_DID)

    assert not refused_did.ok
    assert [e.field for e in refused_did.errors] == ["escalate_to"]
    assert not refused_role.ok
    assert [e.observed for e in refused_role.errors] == ["role:sales"]
    assert accepted.ok, accepted.errors


# -- run start ---------------------------------------------------------------------


def _harness(tmp_path: Path, escalate_to: str, registry: Registry | None = None) -> Harness:
    flow = approval_flow(timeout_s=60, on_timeout="escalate", escalate_to=escalate_to)
    if registry is None:
        return Harness(FakeBackend(), flow, tmp_path, Clock())
    return Harness(FakeBackend(), flow, tmp_path, Clock(), registry=registry)


async def test_run_start_refuses_an_escalate_to_naming_an_agent_did(tmp_path: Path) -> None:
    h = _harness(tmp_path, SALES_DID)

    with pytest.raises(WorkflowRunError, match="escalate_to"):
        await h.start()

    refused = [e for e in h.sink.events if e.action == "workflow.run.refused"]
    assert refused and refused[0].outcome == "escalate_to_not_a_user"
    assert await h.runs.count_runs_for_workflow("orders") == 0


async def test_run_start_refuses_an_escalate_to_did_with_no_user_roster(tmp_path: Path) -> None:
    roles = {ALICE: ("planner",), CAROL: ("supervisor",)}
    h = _harness(tmp_path, CAROL, Registry({"sales": SALES_DID}, roles=roles))

    with pytest.raises(WorkflowRunError, match="escalate_to"):
        await h.start()


async def test_run_start_accepts_an_escalate_to_naming_a_registered_user(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, CAROL)

    run_id = await h.start()

    assert await h.run_status(run_id) == "running"


def _agent_holds_night_shift() -> Registry:
    roles = {ALICE: ("planner",), CAROL: ("supervisor",), SALES_DID: ("night_shift",)}
    return Registry({"sales": SALES_DID}, roles=roles, users=frozenset({ALICE, CAROL}))


async def test_run_start_refuses_an_escalation_role_only_an_agent_holds(tmp_path: Path) -> None:
    h = _harness(tmp_path, "role:night_shift", _agent_holds_night_shift())

    with pytest.raises(WorkflowRunError, match="escalate_to"):
        await h.start()

    refused = [e for e in h.sink.events if e.action == "workflow.run.refused"]
    assert refused and refused[0].outcome == "unknown_gate_role"
    assert await h.runs.count_runs_for_workflow("orders") == 0


async def test_run_start_accepts_an_escalation_role_a_user_holds(tmp_path: Path) -> None:
    h = _harness(tmp_path, "role:supervisor", _agent_holds_night_shift())

    run_id = await h.start()

    assert await h.run_status(run_id) == "running"
