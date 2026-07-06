"""Tests for C1 Team model + TeamStore (REQ-010)."""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.storage import MemoryBackend
from arcteam.team import Team, TeamStore

DID_A = "did:arc:test:agent/alice"
DID_B = "did:arc:test:agent/bob"


@pytest.fixture
async def store() -> TeamStore:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    return TeamStore(backend, audit)


def _team(team_id: str = "ops", members: list[str] | None = None) -> Team:
    return Team(
        id=team_id,
        name=team_id.title(),
        members=members or [],
        default_channel=f"channel://{team_id}",
    )


class TestTeamModel:
    """Team model shape and defaults."""

    def test_defaults(self) -> None:
        team = _team()
        assert team.members == []
        assert team.goal_ref is None

    def test_no_shared_member_lists(self) -> None:
        a, b = _team("a"), _team("b")
        a.members.append(DID_A)
        assert b.members == []


class TestCreateAndGet:
    async def test_create_and_get(self, store: TeamStore) -> None:
        await store.create(_team("ops", members=[DID_A]))
        team = await store.get("ops")
        assert team is not None
        assert team.name == "Ops"
        assert team.members == [DID_A]
        assert team.created != ""

    async def test_get_missing_returns_none(self, store: TeamStore) -> None:
        assert await store.get("ghost") is None

    async def test_create_duplicate_raises(self, store: TeamStore) -> None:
        await store.create(_team("ops"))
        with pytest.raises(ValueError, match="already exists"):
            await store.create(_team("ops"))


class TestList:
    async def test_list_teams(self, store: TeamStore) -> None:
        await store.create(_team("ops"))
        await store.create(_team("research"))
        teams = await store.list_teams()
        assert {t.id for t in teams} == {"ops", "research"}


class TestMembership:
    async def test_add_member(self, store: TeamStore) -> None:
        await store.create(_team("ops"))
        await store.add_member("ops", DID_A)
        team = await store.get("ops")
        assert team is not None
        assert team.members == [DID_A]

    async def test_add_member_idempotent(self, store: TeamStore) -> None:
        await store.create(_team("ops", members=[DID_A]))
        await store.add_member("ops", DID_A)
        team = await store.get("ops")
        assert team is not None
        assert team.members == [DID_A]

    async def test_remove_member(self, store: TeamStore) -> None:
        await store.create(_team("ops", members=[DID_A, DID_B]))
        await store.remove_member("ops", DID_A)
        team = await store.get("ops")
        assert team is not None
        assert team.members == [DID_B]

    async def test_add_member_unknown_team_raises(self, store: TeamStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            await store.add_member("ghost", DID_A)

    async def test_remove_member_unknown_team_raises(self, store: TeamStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            await store.remove_member("ghost", DID_A)


class TestAudit:
    async def test_operations_audited(self, store: TeamStore) -> None:
        await store.create(_team("ops"))
        await store.add_member("ops", DID_A)
        await store.remove_member("ops", DID_A)
        records = await store._backend.read_stream("audit", "audit", after_seq=0)
        events = {r["event_type"] for r in records}
        assert {"team.created", "team.member_added", "team.member_removed"} <= events
