"""Team model and DID-keyed team store (REQ-010).

A :class:`Team` names a set of member entities (by DID), a default channel to
post to, and an optional goal reference. :class:`TeamStore` persists teams via
any :class:`~arcteam.storage.StorageBackend` (in-memory for tests, NATS in
production) and audits every mutation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from arcteam.audit import AuditLogger
from arcteam.storage import StorageBackend

TEAM_COLLECTION = "teams"


class Team(BaseModel):
    """A named group of member entities coordinated together.

    members: the member DIDs (identity is sourced from ``arctrust``; the team
    never mints or resolves identities, it only references them).
    default_channel: the channel ref new team traffic posts to by default.
    goal_ref: optional pointer to the team's goal artifact.
    """

    id: str
    name: str
    members: list[str] = Field(default_factory=list)
    default_channel: str
    goal_ref: str | None = None
    created: str = ""


def _team_key(team_id: str) -> str:
    """Flatten a team id into a filesystem-safe storage key."""
    return team_id.replace(":", "_").replace("/", "_")


class TeamStore:
    """Persist and mutate teams on a :class:`StorageBackend`, auditing each op."""

    def __init__(self, backend: StorageBackend, audit: AuditLogger) -> None:
        self._backend = backend
        self._audit = audit

    async def create(self, team: Team) -> Team:
        """Persist a new team. Rejects a duplicate id."""
        key = _team_key(team.id)
        if await self._backend.read(TEAM_COLLECTION, key) is not None:
            raise ValueError(f"Team already exists: {team.id}")
        if not team.created:
            team.created = datetime.now(UTC).isoformat()

        await self._backend.write(TEAM_COLLECTION, key, team.model_dump())
        await self._audit.log(
            event_type="team.created",
            subject="team",
            actor_id=team.id,
            detail=f"Created team {team.name!r} with {len(team.members)} member(s)",
            target_id=team.id,
        )
        return team

    async def get(self, team_id: str) -> Team | None:
        """Read a team by id, or None if absent."""
        data = await self._backend.read(TEAM_COLLECTION, _team_key(team_id))
        if data is None:
            return None
        return Team.model_validate(data)

    async def list_teams(self) -> list[Team]:
        """Enumerate all teams."""
        records = await self._backend.query(TEAM_COLLECTION)
        return [Team.model_validate(r) for r in records]

    async def add_member(self, team_id: str, did: str) -> Team:
        """Add a member DID to a team (idempotent). Audits the addition."""
        team = await self._require(team_id)
        if did not in team.members:
            team.members.append(did)
            await self._backend.write(TEAM_COLLECTION, _team_key(team_id), team.model_dump())
        await self._audit.log(
            event_type="team.member_added",
            subject="team",
            actor_id=team_id,
            detail=f"Added member {did} to team {team_id}",
            target_id=did,
        )
        return team

    async def remove_member(self, team_id: str, did: str) -> Team:
        """Remove a member DID from a team. Audits the removal."""
        team = await self._require(team_id)
        if did in team.members:
            team.members.remove(did)
            await self._backend.write(TEAM_COLLECTION, _team_key(team_id), team.model_dump())
        await self._audit.log(
            event_type="team.member_removed",
            subject="team",
            actor_id=team_id,
            detail=f"Removed member {did} from team {team_id}",
            target_id=did,
        )
        return team

    async def _require(self, team_id: str) -> Team:
        team = await self.get(team_id)
        if team is None:
            raise ValueError(f"Team not found: {team_id}")
        return team
