"""Shared fixtures for the ``tasks`` module tests — SPEC-056 Phase B.

The fleet these fixtures build is a fake, not the real orchestration layer, and
that is the point: an agent reaches other agents only through the
``arcagent.fleet`` seam, so its own tests can satisfy that seam directly. If
they needed the orchestration package to run, the agent would not really be
standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


def make_operator_signer() -> Any:
    """Build a deployment operator ``Signer`` for an audit chain.

    Distinct from any agent identity — the audited subject must not be its
    own audit authority (SPEC-037 F4).
    """
    from arctrust import OperatorKey

    return OperatorKey.generate().into_signer()


class FakeFleetMember(BaseModel):
    """Satisfies ``arcagent.fleet.FleetMember``."""

    model_config = ConfigDict(frozen=True)

    did: str
    handle: str
    name: str
    capabilities: tuple[str, ...] = ()


@dataclass
class FakeFleetDirectory:
    """Satisfies ``arcagent.fleet.FleetDirectory``.

    Resolution mirrors the real contract: ``@handle``, ``agent://handle``, a
    bare handle or a raw DID resolve to a DID, and anything naming nobody
    raises ``ValueError`` rather than resolving to silence.
    """

    members: list[FakeFleetMember] = field(default_factory=list)

    async def register(self, member: FakeFleetMember) -> None:
        self.members.append(member)

    async def resolve(self, ref: str) -> str:
        if ref.startswith("did:"):
            if any(member.did == ref for member in self.members):
                return ref
            raise ValueError(f"unknown handle: {ref}")
        wanted = ref.removeprefix("@")
        if "://" in wanted:
            scheme, _, wanted = wanted.partition("://")
            if scheme in ("channel", "role"):
                return ref
        for member in self.members:
            if member.handle == wanted:
                return member.did
        raise ValueError(f"unknown handle: {ref}")

    async def list_agents(self) -> tuple[FakeFleetMember, ...]:
        return tuple(self.members)


@dataclass
class FakeFleetMessenger:
    """Satisfies ``arcagent.fleet.FleetMessenger``, recording what was sent."""

    sent: list[Any] = field(default_factory=list)

    async def send_notice(self, notice: Any) -> None:
        self.sent.append(notice)


def make_registry() -> FakeFleetDirectory:
    """A fresh fleet directory for ``@handle`` resolution."""
    return FakeFleetDirectory()


def make_peer_entity(
    handle: str, name: str | None = None, roles: list[str] | None = None
) -> FakeFleetMember:
    """Build a DID-keyed peer so ``@handle`` refs resolve in tests."""
    from arctrust import AgentIdentity

    del roles  # the seam exposes capabilities, not registry roles
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    return FakeFleetMember(
        did=identity.did,
        handle=handle,
        name=name or handle.title(),
    )
