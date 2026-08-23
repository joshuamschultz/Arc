"""Fleet-owned lifecycle for installing shared knowledge on composed agents."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from arcteam.shared_knowledge.attachment import SharedKnowledgeAttachment
from arcteam.shared_knowledge.service import FleetSharedKnowledgeService
from arcteam.team import Team

_EXTENSION_ID = "arcteam.shared_knowledge"


class _AttachmentHost(Protocol):
    async def attach_extension(
        self, extension_id: str, attachment: SharedKnowledgeAttachment
    ) -> object: ...

    async def detach_extension(self, extension_id: str) -> tuple[str, ...]: ...


class _Access(Protocol):
    @property
    def caller_did(self) -> str: ...

    @property
    def clearance(self) -> str: ...


class _Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class ComposedSharedKnowledgeAgent:
    """One already-started ArcAgent and the trusted context it contributes."""

    agent: _AttachmentHost
    personal_knowledge: object
    access: _Access
    signer: _Signer
    audit_sink: Any = None


class FleetSharedKnowledgeComposition:
    """Install, reload, and remove the fleet shared-knowledge extension surface."""

    def __init__(self, team: Team, service: FleetSharedKnowledgeService) -> None:
        self._team = team
        self._service = service

    async def start(self, agents: Iterable[ComposedSharedKnowledgeAgent]) -> None:
        """Attach the shared-knowledge tools to every authorized, started team member."""
        for agent in agents:
            self._require_member(agent.access.caller_did)
            await agent.agent.attach_extension(_EXTENSION_ID, self._attachment(agent))

    async def reload(self, agent: ComposedSharedKnowledgeAgent) -> None:
        """Replace one member's attachment atomically after composition changes."""
        self._require_member(agent.access.caller_did)
        await agent.agent.attach_extension(_EXTENSION_ID, self._attachment(agent))

    async def stop(self, agents: Iterable[ComposedSharedKnowledgeAgent]) -> None:
        """Remove shared-knowledge tools from every supplied composed agent."""
        for agent in agents:
            await agent.agent.detach_extension(_EXTENSION_ID)

    def _attachment(self, agent: ComposedSharedKnowledgeAgent) -> SharedKnowledgeAttachment:
        return SharedKnowledgeAttachment(
            self._service,
            personal_knowledge=cast(Any, agent.personal_knowledge),
            access=cast(Any, agent.access),
            signer=agent.signer,
            audit_sink=agent.audit_sink,
        )

    def _require_member(self, did: str) -> None:
        if did not in self._team.members:
            raise PermissionError("shared knowledge attachment requires team membership")


__all__ = ["ComposedSharedKnowledgeAgent", "FleetSharedKnowledgeComposition"]
