"""Tests for arcteam.mentions — @mention extraction + attention flags (REQ-004)."""

from __future__ import annotations

import pytest
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.mentions import extract_mentions
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message, Priority


class TestExtractMentions:
    """extract_mentions is pure: regex @[a-z0-9_-]+ -> ordered unique handles."""

    def test_single_mention(self) -> None:
        assert extract_mentions("hi @builder") == ["builder"]

    def test_multiple_mentions_ordered_unique(self) -> None:
        assert extract_mentions("@a hello @b and @a again") == ["a", "b"]

    def test_no_mentions(self) -> None:
        assert extract_mentions("nothing here") == []

    def test_hyphen_and_underscore(self) -> None:
        assert extract_mentions("ping @proc-01 and @agent_two") == ["proc-01", "agent_two"]


@pytest.fixture
async def svc() -> MessagingService:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    await registry.register(
        Entity(
            did="did:arc:test:agent/a1",
            handle="a1",
            id="agent://a1",
            name="A1",
            type=EntityType.AGENT,
            roles=[],
        )
    )
    await registry.register(
        Entity(
            did="did:arc:test:agent/builder",
            handle="builder",
            id="agent://builder",
            name="Builder",
            type=EntityType.AGENT,
            roles=[],
        )
    )
    service = MessagingService(backend, registry, audit)
    await service.create_channel(Channel(name="ops", members=["agent://a1", "agent://builder"]))
    return service


class TestSendPopulatesMentions:
    """On send, a body @mention is recorded as a DID and raises attention."""

    async def test_mention_recorded_as_did(self, svc: MessagingService) -> None:
        sent = await svc.send(
            Message(sender="agent://a1", to=["channel://ops"], body="hi @builder")
        )
        assert sent.mentions == ["did:arc:test:agent/builder"]

    async def test_mention_sets_attention_flags(self, svc: MessagingService) -> None:
        sent = await svc.send(
            Message(sender="agent://a1", to=["channel://ops"], body="hi @builder")
        )
        assert sent.action_required is True
        assert sent.priority == Priority.HIGH

    async def test_mention_does_not_downgrade_critical(self, svc: MessagingService) -> None:
        sent = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://ops"],
                body="urgent @builder",
                priority=Priority.CRITICAL,
            )
        )
        assert sent.priority == Priority.CRITICAL

    async def test_unknown_body_mention_ignored(self, svc: MessagingService) -> None:
        sent = await svc.send(Message(sender="agent://a1", to=["channel://ops"], body="hi @ghost"))
        assert sent.mentions == []
        assert sent.action_required is False

    async def test_no_mention_leaves_flags_untouched(self, svc: MessagingService) -> None:
        sent = await svc.send(
            Message(sender="agent://a1", to=["channel://ops"], body="plain text")
        )
        assert sent.mentions == []
        assert sent.action_required is False
        assert sent.priority == Priority.NORMAL
