"""H-040 Slice 1 — the three fail-closed enrollment chokepoints.

Each chokepoint has a dedicated test that goes RED without its guard (the forged /
un-enrolled member would register, route, or run) and GREEN with it:

  1. Registry admission  — :func:`arcteam.harness.enrollment.admit_registration`,
     wired into ``EntityRegistry.register``.
  2. Roster eligibility  — :func:`arcteam.harness.enrollment.is_eligible`, wired
     into ``FleetDirectoryAdapter.list_agents``.
  3. Dispatch / start     — :func:`arcteam.harness.enrollment.guard_dispatch`.
"""

from __future__ import annotations

import pytest
from arctrust.identity import AgentIdentity
from arctrust.policy import (
    OperatorApprovalAuthority,
    enrollment_to_wire,
    sign_enrollment_grant,
)
from arctrust.signer import InProcessSigner

from arcteam.agent_fleet import FleetDirectoryAdapter
from arcteam.audit import AuditLogger
from arcteam.harness.enrollment import (
    EnrollmentDenied,
    guard_dispatch,
    member_admitted,
)
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityStatus, EntityType

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _operator() -> OperatorApprovalAuthority:
    return OperatorApprovalAuthority(AgentIdentity.generate(org="operator", agent_type="approver"))


def _resolver_for(operator: OperatorApprovalAuthority):
    """A trust-store stand-in: returns the operator key ONLY for its own DID."""

    def resolve(approver_did: str) -> bytes:
        if approver_did == operator.did:
            return operator.public_key
        raise KeyError(f"unknown operator {approver_did!r}")

    return resolve


def _enrolled_hermes(operator: OperatorApprovalAuthority, handle: str = "hermes") -> Entity:
    member = AgentIdentity.generate(org="acme", agent_type="hermes")
    grant = sign_enrollment_grant(
        operator=operator,
        did=member.did,
        handle=handle,
        harness="hermes",
        member_public_key=member.public_key,
        capabilities=frozenset({"chat"}),
        clearance="UNCLASSIFIED",
        audit_mode="boundary",
        not_before="2026-08-29T00:00:00+00:00",
        nonce=f"n-{handle}",
    )
    return Entity(
        did=member.did,
        handle=handle,
        id=f"agent://{handle}",
        name=handle.title(),
        type=EntityType.AGENT,
        public_key=member.public_key.hex(),
        harness="hermes",
        enrollment=enrollment_to_wire(grant),
    )


def _native_agent(handle: str = "native") -> Entity:
    identity = AgentIdentity.generate(org="acme", agent_type="executor")
    return Entity(
        did=identity.did,
        handle=handle,
        id=f"agent://{handle}",
        name=handle.title(),
        type=EntityType.AGENT,
        public_key=identity.public_key.hex(),
        harness="arcagent",
    )


@pytest.fixture
async def registry_and_operator() -> tuple[EntityRegistry, OperatorApprovalAuthority]:
    operator = _operator()
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit, resolve_operator_key=_resolver_for(operator))
    return registry, operator


# ---------------------------------------------------------------------------
# Chokepoint 1 — registry admission
# ---------------------------------------------------------------------------


async def test_chokepoint1_admits_a_properly_enrolled_foreign_member(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator)
    await registry.register(hermes)  # must not raise
    assert await registry.get(hermes.did) is not None


async def test_chokepoint1_refuses_an_unenrolled_foreign_member(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator).model_copy(update={"enrollment": None})
    with pytest.raises(EnrollmentDenied):
        await registry.register(hermes)
    assert await registry.get(hermes.did) is None  # never written


async def test_chokepoint1_refuses_a_forged_grant_signature(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator)
    forged_wire = dict(hermes.enrollment or {})
    forged_wire["signature"] = "AAAA"  # base64 garbage
    forged = hermes.model_copy(update={"enrollment": forged_wire})
    with pytest.raises(EnrollmentDenied):
        await registry.register(forged)


async def test_chokepoint1_refuses_a_self_supplied_approver_key(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    """A grant signed by an operator NOT in the trust store is refused."""
    registry, _real = registry_and_operator
    rogue = _operator()  # attacker authority — resolver has no key for its DID
    hermes = _enrolled_hermes(rogue)
    with pytest.raises(EnrollmentDenied):
        await registry.register(hermes)


async def test_chokepoint1_refuses_a_tampered_public_key(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    """Swapping ``public_key`` after the grant was signed breaks the pinned key (TOCTOU)."""
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator)
    attacker = AgentIdentity.generate(org="acme", agent_type="hermes")
    tampered = hermes.model_copy(update={"public_key": attacker.public_key.hex()})
    with pytest.raises(EnrollmentDenied):
        await registry.register(tampered)


async def test_chokepoint1_leaves_native_registration_unchanged(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    registry, _real = registry_and_operator
    native = _native_agent()
    await registry.register(native)  # native trusted by identity — no grant needed
    assert await registry.get(native.did) is not None


# ---------------------------------------------------------------------------
# Chokepoint 2 — roster eligibility
# ---------------------------------------------------------------------------


async def test_chokepoint2_eligible_lists_only_verified_active_members(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator)
    native = _native_agent()
    await registry.register(hermes)
    await registry.register(native)

    directory = FleetDirectoryAdapter(registry, resolve_operator_key=_resolver_for(operator))
    eligible = {e.did for e in await directory.list_agents()}
    assert hermes.did in eligible
    assert native.did in eligible


async def test_chokepoint2_excludes_a_revoked_member(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator)
    await registry.register(hermes)
    revoked = (await registry.get(hermes.did)).model_copy(update={"status": EntityStatus.revoked})
    await registry.update(revoked)

    directory = FleetDirectoryAdapter(registry, resolve_operator_key=_resolver_for(operator))
    assert hermes.did not in {e.did for e in await directory.list_agents()}


async def test_chokepoint2_excludes_a_foreign_member_that_no_longer_verifies(
    registry_and_operator: tuple[EntityRegistry, OperatorApprovalAuthority],
) -> None:
    """With no operator key available (revoked from trust store), a foreign member is ineligible."""
    registry, operator = registry_and_operator
    hermes = _enrolled_hermes(operator)
    await registry.register(hermes)

    def empty_resolver(_did: str) -> bytes:
        raise KeyError("operator revoked from trust store")

    directory = FleetDirectoryAdapter(registry, resolve_operator_key=empty_resolver)
    assert hermes.did not in {e.did for e in await directory.list_agents()}


# ---------------------------------------------------------------------------
# Chokepoint 3 — dispatch / start
# ---------------------------------------------------------------------------


def test_chokepoint3_allows_a_verified_member() -> None:
    operator = _operator()
    hermes = _enrolled_hermes(operator)
    guard_dispatch(hermes, resolve_operator_key=_resolver_for(operator))  # must not raise


def test_chokepoint3_refuses_an_unenrolled_member() -> None:
    operator = _operator()
    hermes = _enrolled_hermes(operator).model_copy(update={"enrollment": None})
    with pytest.raises(EnrollmentDenied):
        guard_dispatch(hermes, resolve_operator_key=_resolver_for(operator))


def test_chokepoint3_refuses_a_tampered_member_even_past_admission() -> None:
    """Forged code never runs: a swapped key breaks the pinned-key check at dispatch."""
    operator = _operator()
    hermes = _enrolled_hermes(operator)
    attacker = AgentIdentity.generate(org="acme", agent_type="hermes")
    tampered = hermes.model_copy(update={"public_key": attacker.public_key.hex()})
    assert not member_admitted(tampered, _resolver_for(operator))
    with pytest.raises(EnrollmentDenied):
        guard_dispatch(tampered, resolve_operator_key=_resolver_for(operator))


def test_chokepoint3_refuses_a_revoked_member() -> None:
    operator = _operator()
    hermes = _enrolled_hermes(operator).model_copy(update={"status": EntityStatus.revoked})
    with pytest.raises(EnrollmentDenied):
        guard_dispatch(hermes, resolve_operator_key=_resolver_for(operator))
