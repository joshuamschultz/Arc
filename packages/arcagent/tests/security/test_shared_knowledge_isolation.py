"""Abuse cases for connection-scoped knowledge stores (P18-4, ASI03 / LLM02).

One store serves every agent granted a connection, so the boundary is no longer
a directory per agent: it is the subscription each read and each write is
checked against. These cases attack that boundary directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend

from arcagent.connected_data import MappingDeniedError
from arcagent.extension.knowledge_subscriptions import (
    KnowledgeSubscription,
    KnowledgeSubscriptions,
    knowledge_principal,
)
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data.shared import SharedKnowledge, SubscriberAuthority

_A = "did:arc:test:agent-a"
_B = "did:arc:test:agent-b"


def _subscription(agent_did: str, *, approval_id: str = "approval-a") -> KnowledgeSubscription:
    return KnowledgeSubscription(
        agent_did=agent_did,
        connection_id="wiki",
        source_id="pool",
        approval_id=approval_id,
        profile="lexical",
    )


def _shared(backend: FakeBackend, root: Path, agent_did: str = _A) -> SharedKnowledge:
    async def opener() -> FakeBackend:
        return backend

    return SharedKnowledge(
        agent_did=agent_did,
        arcstore_opener=opener,
        embedder=lambda: None,
        profile=lambda: "lexical",
        root=lambda: root,
    )


def _source() -> SourceDescription:
    return SourceDescription(connection_id="wiki", source_kind="confluence", account_id="acct")


@pytest.mark.asyncio
async def test_a_writer_whose_subscription_is_gone_is_refused_mid_run() -> None:
    registry = KnowledgeSubscriptions(FakeBackend(), actor_did=_A)
    await registry.put(_subscription(_A))
    authority = SubscriberAuthority(registry, _A, "wiki", "approval-a")
    assert await authority.authorized_homes() is not None

    await registry.delete(_A, "wiki")

    assert await authority.authorized_homes() is None


@pytest.mark.asyncio
async def test_a_writer_cannot_ride_a_subscription_verified_against_another_approval() -> None:
    registry = KnowledgeSubscriptions(FakeBackend(), actor_did=_A)
    await registry.put(_subscription(_A, approval_id="approval-new"))

    stale = SubscriberAuthority(registry, _A, "wiki", "approval-old")

    assert await stale.authorized_homes() is None


@pytest.mark.asyncio
async def test_an_agent_lists_only_its_own_subscriptions() -> None:
    backend = FakeBackend()
    registry = KnowledgeSubscriptions(backend, actor_did=_A)
    await registry.put(_subscription(_A))
    await registry.put(_subscription(_B))

    mine = await registry.for_agent(_A)

    assert [row.agent_did for row in mine] == [_A]
    assert {row.agent_did for row in await registry.for_connection("wiki")} == {_A, _B}


@pytest.mark.asyncio
async def test_a_malformed_subscription_row_authorizes_nothing() -> None:
    backend = FakeBackend()
    registry = KnowledgeSubscriptions(backend, actor_did=_A)
    await backend.mutable_write(
        KnowledgeSubscriptions.COLLECTION,
        "forged",
        {"agent_did": _A, "connection_id": "wiki", "source_id": "pool"},
        actor_did=_A,
    )

    assert await registry.for_agent(_A) == []


@pytest.mark.asyncio
async def test_a_readers_port_can_write_nothing(tmp_path: Path) -> None:
    reader = await _shared(FakeBackend(), tmp_path).reader("wiki")
    try:
        with pytest.raises(MappingDeniedError):
            await reader.require_approved_mapping(_source())
    finally:
        await reader.aclose()


@pytest.mark.asyncio
async def test_the_store_is_filed_under_no_agents_identity() -> None:
    principal = knowledge_principal("wiki")

    assert principal.startswith("did:arc:knowledge:")
    assert principal not in {_A, _B}
    assert knowledge_principal("wiki") == principal != knowledge_principal("mail")


def test_a_store_embedded_one_way_is_not_read_another(tmp_path: Path) -> None:
    backend = FakeBackend()
    first = _shared(backend, tmp_path)
    assert first.claim_profile("wiki")

    other = SharedKnowledge(
        agent_did=_B,
        arcstore_opener=first._arcstore_opener,
        embedder=lambda: None,
        profile=lambda: "another-model",
        root=lambda: tmp_path,
    )

    assert not other.profile_compatible("wiki")
    assert not other.claim_profile("wiki")
    assert first.claim_profile("wiki")
