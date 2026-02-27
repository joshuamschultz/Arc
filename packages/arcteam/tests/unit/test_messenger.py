"""Tests for arcteam.messenger — MessagingService core operations."""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message, MsgType, Priority


@pytest.fixture
async def svc() -> MessagingService:
    """Full messaging service with registered entities."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)

    # Register test entities
    await registry.register(
        Entity(
            id="agent://a1",
            name="Agent One",
            type=EntityType.AGENT,
            roles=["ops", "dev"],
        )
    )
    await registry.register(
        Entity(
            id="agent://a2",
            name="Agent Two",
            type=EntityType.AGENT,
            roles=["ops"],
        )
    )
    await registry.register(
        Entity(
            id="user://josh",
            name="Josh",
            type=EntityType.USER,
            roles=["admin"],
        )
    )

    # Create a channel with members
    await svc.create_channel(
        Channel(
            name="project-alpha",
            description="Project Alpha",
            members=["agent://a1", "agent://a2", "user://josh"],
        )
    )

    return svc


class TestSendToChannel:
    """Send to channel: message appears in channel stream with correct seq."""

    async def test_send_to_channel(self, svc: MessagingService) -> None:
        msg = Message(
            sender="agent://a1",
            to=["channel://project-alpha"],
            body="Hello channel!",
        )
        sent = await svc.send(msg)
        assert sent.seq == 1
        assert sent.id.startswith("msg_")
        assert sent.ts != ""
        assert sent.status == "sent"

        # Verify it's in the stream
        messages = await svc.poll("arc.channel.project-alpha", "agent://a2")
        assert len(messages) == 1
        assert messages[0].body == "Hello channel!"


class TestSendToRole:
    """Send to role: message appears in role stream."""

    async def test_send_to_role(self, svc: MessagingService) -> None:
        msg = Message(
            sender="user://josh",
            to=["role://ops"],
            body="All ops agents: check in",
        )
        sent = await svc.send(msg)
        assert sent.seq == 1

        # Agent a1 (ops role) can poll the role stream
        messages = await svc.poll("arc.role.ops", "agent://a1")
        assert len(messages) == 1
        assert messages[0].body == "All ops agents: check in"


class TestSendDM:
    """Send to agent (DM): message appears in agent inbox stream."""

    async def test_send_dm(self, svc: MessagingService) -> None:
        msg = Message(
            sender="user://josh",
            to=["agent://a1"],
            body="Hey agent, check this out",
        )
        await svc.send(msg)
        messages = await svc.poll("arc.agent.a1", "agent://a1")
        assert len(messages) == 1
        assert messages[0].sender == "user://josh"


class TestSendMultipleTargets:
    """Send to multiple targets: message appears in all."""

    async def test_multi_target(self, svc: MessagingService) -> None:
        msg = Message(
            sender="agent://a1",
            to=["channel://project-alpha", "agent://a2"],
            body="Cross-post",
        )
        await svc.send(msg)

        ch_msgs = await svc.poll("arc.channel.project-alpha", "agent://a2")
        dm_msgs = await svc.poll("arc.agent.a2", "agent://a2")
        assert len(ch_msgs) == 1
        assert len(dm_msgs) == 1


class TestAutoAssign:
    """Auto-assign: seq is monotonic, id is unique, ts is set, thread_id auto-set."""

    async def test_monotonic_seq(self, svc: MessagingService) -> None:
        for i in range(5):
            await svc.send(
                Message(
                    sender="agent://a1",
                    to=["channel://project-alpha"],
                    body=f"msg {i}",
                )
            )
        messages = await svc.poll("arc.channel.project-alpha", "agent://a2")
        seqs = [m.seq for m in messages]
        assert seqs == [1, 2, 3, 4, 5]

    async def test_auto_thread_id(self, svc: MessagingService) -> None:
        """New message gets thread_id = own id."""
        sent = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="new thread",
            )
        )
        assert sent.thread_id == sent.id


class TestThreading:
    """Threading: explicit thread_id groups messages into conversations."""

    async def test_reply_thread(self, svc: MessagingService) -> None:
        original = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="original message",
            )
        )
        reply = await svc.send(
            Message(
                sender="agent://a2",
                to=["channel://project-alpha"],
                body="reply to original",
                thread_id=original.id,
            )
        )
        assert reply.thread_id == original.id


class TestPoll:
    """Poll: returns messages after cursor, respects limit."""

    async def test_poll_respects_limit(self, svc: MessagingService) -> None:
        for i in range(10):
            await svc.send(
                Message(
                    sender="agent://a1",
                    to=["channel://project-alpha"],
                    body=f"msg {i}",
                )
            )
        messages = await svc.poll("arc.channel.project-alpha", "agent://a2", max_messages=3)
        assert len(messages) == 3

    async def test_poll_no_cursor_reads_from_beginning(self, svc: MessagingService) -> None:
        await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="first",
            )
        )
        messages = await svc.poll("arc.channel.project-alpha", "agent://a2")
        assert len(messages) == 1
        assert messages[0].body == "first"


class TestAck:
    """Ack: cursor advances, subsequent poll starts from new position."""

    async def test_ack_advances_cursor(self, svc: MessagingService) -> None:
        for i in range(5):
            await svc.send(
                Message(
                    sender="agent://a1",
                    to=["channel://project-alpha"],
                    body=f"msg {i}",
                )
            )

        # Poll all 5
        messages = await svc.poll("arc.channel.project-alpha", "agent://a2")
        assert len(messages) == 5

        # Ack the 3rd message
        await svc.ack("arc.channel.project-alpha", "agent://a2", seq=3, byte_pos=0)

        # Poll again — should only get messages after seq 3
        messages = await svc.poll("arc.channel.project-alpha", "agent://a2")
        assert len(messages) == 2
        assert messages[0].seq == 4

    async def test_ack_rejects_backward(self, svc: MessagingService) -> None:
        await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="msg",
            )
        )
        await svc.ack("arc.channel.project-alpha", "agent://a2", seq=5, byte_pos=0)
        await svc.ack("arc.channel.project-alpha", "agent://a2", seq=2, byte_pos=0)  # Backward

        cursor = await svc.get_cursor("arc.channel.project-alpha", "agent://a2")
        assert cursor is not None
        assert cursor.seq == 5  # Didn't go backward


class TestPollAll:
    """poll_all: returns messages from all subscribed streams."""

    async def test_poll_all(self, svc: MessagingService) -> None:
        # Send to channel
        await svc.send(
            Message(
                sender="user://josh",
                to=["channel://project-alpha"],
                body="channel msg",
            )
        )
        # Send DM to a1
        await svc.send(
            Message(
                sender="agent://a2",
                to=["agent://a1"],
                body="dm msg",
            )
        )
        # Send to role
        await svc.send(
            Message(
                sender="user://josh",
                to=["role://ops"],
                body="role msg",
            )
        )

        result = await svc.poll_all("agent://a1")
        assert "arc.agent.a1" in result
        assert "arc.channel.project-alpha" in result
        assert "arc.role.ops" in result


class TestChannelManagement:
    """Channel create/join/leave."""

    async def test_channel_join_leave(self, svc: MessagingService) -> None:
        await svc.create_channel(Channel(name="new-channel"))
        await svc.join_channel("new-channel", "agent://a1")

        channels = await svc.list_channels()
        new_ch = next(c for c in channels if c.name == "new-channel")
        assert "agent://a1" in new_ch.members

        await svc.leave_channel("new-channel", "agent://a1")
        channels = await svc.list_channels()
        new_ch = next(c for c in channels if c.name == "new-channel")
        assert "agent://a1" not in new_ch.members

    async def test_join_nonexistent_channel(self, svc: MessagingService) -> None:
        with pytest.raises(ValueError, match="not found"):
            await svc.join_channel("nonexistent", "agent://a1")


class TestBodyTooLarge:
    """Body too large: rejected, DLQ entry created."""

    async def test_body_too_large_dlq(self, svc: MessagingService) -> None:
        # Use model_construct to bypass Pydantic validation (simulates programmatic input)
        big_body = "x" * 70000
        msg = Message.model_construct(
            seq=0,
            id="",
            ts="",
            sender="agent://a1",
            to=["channel://project-alpha"],
            thread_id=None,
            msg_type=MsgType.INFO,
            priority=Priority.NORMAL,
            action_required=False,
            body=big_body,
            refs=[],
            status="sent",
            meta={},
        )
        with pytest.raises(ValueError, match="64KB"):
            await svc.send(msg)
        dlq = await svc.dlq_list()
        assert len(dlq) >= 1
        assert dlq[0]["meta"]["dlq_reason"] == "body_too_large"


class TestInvalidURI:
    """Invalid URI: rejected, DLQ entry created."""

    async def test_invalid_uri_dlq(self, svc: MessagingService) -> None:
        with pytest.raises(ValueError, match="Invalid URI"):
            await svc.send(
                Message(
                    sender="agent://a1",
                    to=["http://bad-uri"],
                    body="test",
                )
            )
        dlq = await svc.dlq_list()
        assert any(d["meta"].get("dlq_reason") == "invalid_address" for d in dlq)


class TestUnregisteredSender:
    """Unregistered sender: rejected, DLQ entry created."""

    async def test_unregistered_sender_dlq(self, svc: MessagingService) -> None:
        with pytest.raises(ValueError, match="not registered"):
            await svc.send(
                Message(
                    sender="agent://unknown",
                    to=["channel://project-alpha"],
                    body="test",
                )
            )
        dlq = await svc.dlq_list()
        assert any(d["meta"].get("dlq_reason") == "sender_unauthorized" for d in dlq)


class TestAuditOnSend:
    """All send operations generate audit records."""

    async def test_send_audit(self, svc: MessagingService) -> None:
        await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="audited message",
            )
        )
        records = await svc._backend.read_stream("audit", "audit", after_seq=0, limit=1000)
        send_records = [r for r in records if r["event_type"] == "message.sent"]
        assert len(send_records) >= 1


class TestGetThread:
    """get_thread: returns all messages in thread chronologically."""

    async def test_get_thread(self, svc: MessagingService) -> None:
        original = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="original",
            )
        )
        await svc.send(
            Message(
                sender="agent://a2",
                to=["channel://project-alpha"],
                body="reply 1",
                thread_id=original.id,
            )
        )
        await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="reply 2",
                thread_id=original.id,
            )
        )
        # Unrelated message
        await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="not in thread",
            )
        )

        thread = await svc.get_thread("arc.channel.project-alpha", original.id)
        assert len(thread) == 3
        assert thread[0].body == "original"
        assert thread[1].body == "reply 1"
        assert thread[2].body == "reply 2"


class TestChannelMembership:
    """FR-7: Non-member cannot send to channel."""

    async def test_non_member_rejected(self, svc: MessagingService) -> None:
        """Agent not in channel is rejected with DLQ entry."""
        # Create a channel without user://josh as member
        await svc.create_channel(Channel(name="private", members=["agent://a1"]))
        with pytest.raises(ValueError, match="not a member"):
            await svc.send(
                Message(
                    sender="agent://a2",
                    to=["channel://private"],
                    body="should fail",
                )
            )
        dlq = await svc.dlq_list()
        assert any(d["meta"].get("dlq_reason") == "not_channel_member" for d in dlq)

    async def test_member_allowed(self, svc: MessagingService) -> None:
        """Channel member can send successfully."""
        sent = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="member message",
            )
        )
        assert sent.seq >= 1


class TestDeepThreading:
    """Deep threading: all replies in a thread share the same thread_id."""

    async def test_deep_thread_same_id(self, svc: MessagingService) -> None:
        original = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="thread root",
            )
        )
        reply1 = await svc.send(
            Message(
                sender="agent://a2",
                to=["channel://project-alpha"],
                body="first reply",
                thread_id=original.id,
            )
        )
        # All replies pass the same thread_id
        reply2 = await svc.send(
            Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body="reply to reply",
                thread_id=original.id,
            )
        )
        assert reply1.thread_id == original.id
        assert reply2.thread_id == original.id


class TestStaleCursorCleanup:
    """FR-11: Stale cursor cleanup."""

    async def test_cleanup_removes_old_cursors(self, svc: MessagingService) -> None:
        # Create a cursor by acking
        await svc.ack("arc.channel.project-alpha", "agent://a1", seq=1, byte_pos=0)

        # Cleanup with 0 hours threshold (everything is stale)
        removed = await svc.cleanup_stale_cursors(max_age_hours=0)
        assert removed >= 1

    async def test_cleanup_keeps_recent_cursors(self, svc: MessagingService) -> None:
        await svc.ack("arc.channel.project-alpha", "agent://a1", seq=1, byte_pos=0)

        # Cleanup with 24h threshold (nothing should be removed)
        removed = await svc.cleanup_stale_cursors(max_age_hours=24)
        assert removed == 0
