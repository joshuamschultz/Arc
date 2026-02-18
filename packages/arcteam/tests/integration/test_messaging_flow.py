"""Integration tests for ArcTeam messaging — full FileBackend flows."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import FileBackend
from arcteam.types import Channel, Entity, EntityType, Message, MsgType, Priority


@pytest.fixture
async def svc(tmp_path: Path) -> MessagingService:
    """Full service stack with FileBackend on temp directory."""
    backend = FileBackend(root=tmp_path)
    audit = AuditLogger(backend, hmac_key=b"integration-test-key")
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)
    return svc


@pytest.fixture
async def populated_svc(svc: MessagingService) -> MessagingService:
    """Service with pre-registered entities and channels."""
    registry = svc._registry

    # Register entities
    await registry.register(Entity(
        id="agent://proc-01", name="Procurement 01", type=EntityType.AGENT,
        roles=["procurement", "research"],
    ))
    await registry.register(Entity(
        id="agent://proc-02", name="Procurement 02", type=EntityType.AGENT,
        roles=["procurement"],
    ))
    await registry.register(Entity(
        id="agent://analyst", name="Analyst", type=EntityType.AGENT,
        roles=["research"],
    ))
    await registry.register(Entity(
        id="user://josh", name="Josh", type=EntityType.USER,
        roles=["admin"],
    ))

    # Create channels
    await svc.create_channel(Channel(
        name="project-alpha",
        members=["agent://proc-01", "agent://proc-02", "user://josh"],
    ))
    await svc.create_channel(Channel(
        name="research",
        members=["agent://proc-01", "agent://analyst"],
    ))

    return svc


class TestFullAgentWorkflow:
    """Integration: register -> create channel -> join -> send -> poll -> ack -> poll empty."""

    async def test_full_workflow(self, populated_svc: MessagingService) -> None:
        svc = populated_svc

        # Send message to channel
        sent = await svc.send(Message(
            sender="user://josh",
            to=["channel://project-alpha"],
            body="Please analyze vendor proposals",
            msg_type=MsgType.TASK,
            priority=Priority.HIGH,
            action_required=True,
        ))
        assert sent.seq == 1

        # Agent polls
        messages = await svc.poll("arc.channel.project-alpha", "agent://proc-01")
        assert len(messages) == 1
        msg = messages[0]
        assert msg.body == "Please analyze vendor proposals"
        assert msg.priority == Priority.HIGH

        # Agent acks
        await svc.ack("arc.channel.project-alpha", "agent://proc-01", seq=1, byte_pos=0)

        # Poll again — should be empty
        messages = await svc.poll("arc.channel.project-alpha", "agent://proc-01")
        assert len(messages) == 0


class TestMultiAgentScenario:
    """Integration: 3 agents, 2 channels, role broadcast, DMs."""

    async def test_multi_agent(self, populated_svc: MessagingService) -> None:
        svc = populated_svc

        # Josh sends to channel
        await svc.send(Message(
            sender="user://josh",
            to=["channel://project-alpha"],
            body="Channel message",
        ))

        # Josh sends to role
        await svc.send(Message(
            sender="user://josh",
            to=["role://procurement"],
            body="All procurement agents: status update",
        ))

        # Josh sends DM to analyst
        await svc.send(Message(
            sender="user://josh",
            to=["agent://analyst"],
            body="Private note to analyst",
        ))

        # proc-01 polls all (has procurement+research roles, is in project-alpha+research channels)
        result = await svc.poll_all("agent://proc-01")
        assert "arc.channel.project-alpha" in result
        assert "arc.role.procurement" in result

        # analyst polls (has research role, is in research channel)
        result = await svc.poll_all("agent://analyst")
        assert "arc.agent.analyst" in result  # DM
        assert result["arc.agent.analyst"][0].body == "Private note to analyst"

        # proc-02 sees role message
        result = await svc.poll_all("agent://proc-02")
        assert "arc.role.procurement" in result
        assert "arc.channel.project-alpha" in result


class TestCursorCrashRecovery:
    """Integration: cursor crash recovery — message persists until acked."""

    async def test_crash_recovery(self, populated_svc: MessagingService) -> None:
        svc = populated_svc

        # Send message
        await svc.send(Message(
            sender="user://josh",
            to=["channel://project-alpha"],
            body="important task",
        ))

        # Agent polls but doesn't ack (simulates crash)
        messages = await svc.poll("arc.channel.project-alpha", "agent://proc-01")
        assert len(messages) == 1

        # "Restart" — poll again without acking
        messages = await svc.poll("arc.channel.project-alpha", "agent://proc-01")
        assert len(messages) == 1  # Same message redelivered
        assert messages[0].body == "important task"


class TestAuditChainVerification:
    """Integration: audit chain verification after full workflow."""

    async def test_audit_chain_valid(self, populated_svc: MessagingService) -> None:
        svc = populated_svc

        # Perform various operations
        await svc.send(Message(
            sender="user://josh", to=["channel://project-alpha"], body="msg 1",
        ))
        await svc.send(Message(
            sender="agent://proc-01", to=["agent://proc-02"], body="dm",
        ))
        await svc.join_channel("research", "agent://proc-02")

        # Verify chain
        valid, last_seq = await svc._audit.verify_chain()
        assert valid is True
        assert last_seq > 0


class TestDLQCaptures:
    """Integration: DLQ captures all failure types."""

    async def test_dlq_failures(self, populated_svc: MessagingService) -> None:
        svc = populated_svc

        # Unregistered sender
        with pytest.raises(ValueError):
            await svc.send(Message(
                sender="agent://unknown", to=["channel://project-alpha"], body="test",
            ))

        # Invalid URI
        with pytest.raises(ValueError):
            await svc.send(Message(
                sender="agent://proc-01", to=["bad://uri"], body="test",
            ))

        # Check DLQ
        dlq = await svc.dlq_list()
        assert len(dlq) >= 2
        reasons = {d["meta"]["dlq_reason"] for d in dlq}
        assert "sender_unauthorized" in reasons
        assert "invalid_address" in reasons
