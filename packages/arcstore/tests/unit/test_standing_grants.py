"""arcstore ``standing_grants`` — the durable home of an operator's "Always allow".

One row per scope (agent + tool + composition + destination). The agent reads the
active rows fresh on every gate hit, so a revoke takes effect on the very next call.
"""

from __future__ import annotations

from arcstore.backends.memory import FakeBackend
from arcstore.standing_grants import StandingGrant, StandingGrantStore, standing_grant_id

_AGENT = "did:arc:local:executor/aaaaaaaa"
_OTHER = "did:arc:local:executor/bbbbbbbb"
_OPERATOR = "did:arc:operator:approver/cccccccc"


def _row(agent_did: str = _AGENT, destination: str = "personal_dropbox") -> StandingGrant:
    return StandingGrant(
        id=standing_grant_id(
            agent_did=agent_did,
            tool="dropbox_upload",
            composition=["private_data", "external_comms", "untrusted_input"],
            destination=destination,
        ),
        agent_did=agent_did,
        agent_label="Olivia",
        tool="dropbox_upload",
        composition=["private_data", "external_comms", "untrusted_input"],
        destination=destination,
        grant={"signature": "x"},
        granted_by=_OPERATOR,
        source_approval_id="req1",
    )


async def _store() -> StandingGrantStore:
    backend = FakeBackend()
    await backend.start()
    return StandingGrantStore(backend)


async def test_put_then_list_active_for_agent() -> None:
    store = await _store()
    await store.put(_row(), actor_did=_OPERATOR)
    await store.put(_row(agent_did=_OTHER), actor_did=_OPERATOR)

    rows = await store.active_for(_AGENT)

    assert [r.agent_did for r in rows] == [_AGENT]
    assert rows[0].status == "active"
    assert rows[0].granted_at is not None
    assert rows[0].use_count == 0


async def test_id_is_stable_per_scope_and_order_independent() -> None:
    a = standing_grant_id(agent_did=_AGENT, tool="t", composition=["b", "a"], destination="d")
    b = standing_grant_id(agent_did=_AGENT, tool="t", composition=["a", "b"], destination="d")
    c = standing_grant_id(agent_did=_AGENT, tool="t", composition=["a", "b"], destination="d2")
    assert a == b
    assert a != c


async def test_revoke_drops_it_from_active_at_once() -> None:
    store = await _store()
    row = await store.put(_row(), actor_did=_OPERATOR)

    revoked = await store.revoke(row.id, actor_did=_OPERATOR)

    assert revoked is not None
    assert revoked.status == "revoked"
    assert revoked.revoked_by == _OPERATOR
    assert await store.active_for(_AGENT) == []
    assert await store.revoke(row.id, actor_did=_OPERATOR) is None


async def test_record_use_counts_only_active_grants() -> None:
    store = await _store()
    row = await store.put(_row(), actor_did=_OPERATOR)

    assert await store.record_use(row.id, actor_did=_AGENT)
    assert await store.record_use(row.id, actor_did=_AGENT)
    got = await store.get(row.id)
    assert got is not None
    assert got.use_count == 2
    assert got.last_used_at is not None

    await store.revoke(row.id, actor_did=_OPERATOR)
    assert not await store.record_use(row.id, actor_did=_AGENT)


async def test_regrant_after_revoke_is_active_again() -> None:
    store = await _store()
    row = await store.put(_row(), actor_did=_OPERATOR)
    await store.revoke(row.id, actor_did=_OPERATOR)

    await store.put(_row(), actor_did=_OPERATOR)

    assert [r.id for r in await store.active_for(_AGENT)] == [row.id]


async def test_list_filters_by_agent_and_status() -> None:
    store = await _store()
    first = await store.put(_row(), actor_did=_OPERATOR)
    await store.put(_row(destination="work_dropbox"), actor_did=_OPERATOR)
    await store.revoke(first.id, actor_did=_OPERATOR)

    assert len(await store.list(agent_did=_AGENT)) == 2
    assert [r.destination for r in await store.list(agent_did=_AGENT, status="active")] == [
        "work_dropbox"
    ]
