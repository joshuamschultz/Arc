"""Team roster: join team membership with registry identity + live presence.

This is the canonical roster for Arc. ``arcgateway.team_roster`` builds a
disk-derived fleet view for the UI and is superseded by this one — new callers
should snapshot here, where membership, identity, and presence are joined from
the DID-keyed registry rather than re-parsed from ``arcagent.toml`` files.
"""

from __future__ import annotations

from pydantic import BaseModel

from arcteam.registry import EntityRegistry
from arcteam.team import Team
from arcteam.types import EntityStatus


class RosterEntry(BaseModel):
    """One member row: identity (``handle``/``did``) joined with live presence."""

    handle: str
    did: str
    status: EntityStatus
    capabilities: list[str]


class Roster:
    """Builds a point-in-time roster for a team from the entity registry."""

    def __init__(self, registry: EntityRegistry) -> None:
        self._registry = registry

    async def snapshot(self, team: Team) -> list[RosterEntry]:
        """Return one :class:`RosterEntry` per team member, in membership order.

        Each member DID is joined against the registry for its handle,
        presence status, and capabilities. A member with no registry record is
        reported ``offline`` — membership is authoritative, presence follows.
        """
        entries: list[RosterEntry] = []
        for did in team.members:
            entity = await self._registry.get(did)
            if entity is None:
                entries.append(
                    RosterEntry(
                        handle=did,
                        did=did,
                        status=EntityStatus.offline,
                        capabilities=[],
                    )
                )
                continue
            entries.append(
                RosterEntry(
                    handle=entity.handle,
                    did=entity.did,
                    status=entity.status,
                    capabilities=entity.capabilities,
                )
            )
        return entries
