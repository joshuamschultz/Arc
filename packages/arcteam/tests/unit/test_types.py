"""Tests for arcteam.types — models, enums, URI parsing, validation."""

import pytest

from arcteam.types import (
    AuditRecord,
    Channel,
    Cursor,
    Entity,
    EntityType,
    Message,
    MsgType,
    Priority,
    generate_message_id,
    make_uri,
    parse_uri,
)


class TestMessageModel:
    """Message Pydantic model validation."""

    def test_valid_message_minimal(self) -> None:
        msg = Message(sender="agent://a1", to=["channel://ops"], body="hello")
        assert msg.sender == "agent://a1"
        assert msg.body == "hello"
        assert msg.seq == 0
        assert msg.status == "sent"
        assert msg.msg_type == MsgType.INFO
        assert msg.priority == Priority.NORMAL
        assert not msg.action_required

    def test_valid_message_all_fields(self) -> None:
        msg = Message(
            seq=5,
            id="msg_123_abc",
            ts="2026-02-17T00:00:00Z",
            sender="user://josh",
            to=["agent://a1", "channel://ops"],
            reply_to="msg_100_xyz",
            thread_id="msg_99_root",
            msg_type=MsgType.TASK,
            priority=Priority.HIGH,
            action_required=True,
            subject="Deploy v2",
            body="Please deploy version 2",
            refs=["ref://doc1"],
            status="delivered",
            meta={"urgent": True},
        )
        assert msg.seq == 5
        assert msg.msg_type == MsgType.TASK
        assert msg.priority == Priority.HIGH
        assert msg.action_required is True
        assert len(msg.to) == 2

    def test_defaults_for_optional_fields(self) -> None:
        msg = Message(sender="agent://a1", to=["channel://ops"], body="test")
        assert msg.id == ""
        assert msg.ts == ""
        assert msg.reply_to is None
        assert msg.thread_id is None
        assert msg.refs == []
        assert msg.meta == {}

    def test_body_size_limit_under(self) -> None:
        body = "x" * 60000  # Under 64KB
        msg = Message(sender="agent://a1", to=["channel://ops"], body=body)
        assert len(msg.body) == 60000

    def test_body_size_limit_exceeded(self) -> None:
        body = "x" * 70000  # Over 64KB
        with pytest.raises(ValueError, match="64KB limit"):
            Message(sender="agent://a1", to=["channel://ops"], body=body)

    def test_body_size_multibyte(self) -> None:
        # Unicode characters can exceed byte limit before char limit
        body = "\U0001f600" * 20000  # Each emoji is 4 bytes = 80KB
        with pytest.raises(ValueError, match="64KB limit"):
            Message(sender="agent://a1", to=["channel://ops"], body=body)


class TestURIParsing:
    """URI parsing for all 4 schemes + invalid URIs."""

    def test_agent_uri(self) -> None:
        scheme, name = parse_uri("agent://procurement-01")
        assert scheme == "agent"
        assert name == "procurement-01"

    def test_user_uri(self) -> None:
        scheme, name = parse_uri("user://josh")
        assert scheme == "user"
        assert name == "josh"

    def test_channel_uri(self) -> None:
        scheme, name = parse_uri("channel://project-alpha")
        assert scheme == "channel"
        assert name == "project-alpha"

    def test_role_uri(self) -> None:
        scheme, name = parse_uri("role://procurement")
        assert scheme == "role"
        assert name == "procurement"

    def test_invalid_scheme(self) -> None:
        with pytest.raises(ValueError, match="Invalid URI"):
            parse_uri("http://example.com")

    def test_invalid_format_no_scheme(self) -> None:
        with pytest.raises(ValueError, match="Invalid URI"):
            parse_uri("just-a-name")

    def test_invalid_format_empty_name(self) -> None:
        with pytest.raises(ValueError, match="Invalid URI"):
            parse_uri("agent://")

    def test_invalid_format_special_chars(self) -> None:
        with pytest.raises(ValueError, match="Invalid URI"):
            parse_uri("agent://bad name!")

    def test_make_uri(self) -> None:
        assert make_uri("agent", "a1") == "agent://a1"
        assert make_uri("channel", "ops") == "channel://ops"

    def test_make_uri_invalid_scheme(self) -> None:
        with pytest.raises(ValueError, match="Invalid scheme"):
            make_uri("http", "example")


class TestEnumSerialization:
    """Enum serialization roundtrip."""

    def test_entity_type_roundtrip(self) -> None:
        assert EntityType("agent") == EntityType.AGENT
        assert EntityType.AGENT.value == "agent"

    def test_msg_type_roundtrip(self) -> None:
        for mt in MsgType:
            assert MsgType(mt.value) == mt

    def test_priority_roundtrip(self) -> None:
        for p in Priority:
            assert Priority(p.value) == p

    def test_enum_in_message_serialization(self) -> None:
        msg = Message(
            sender="agent://a1",
            to=["channel://ops"],
            body="test",
            msg_type=MsgType.ALERT,
            priority=Priority.CRITICAL,
        )
        data = msg.model_dump()
        assert data["msg_type"] == "alert"
        assert data["priority"] == "critical"
        restored = Message.model_validate(data)
        assert restored.msg_type == MsgType.ALERT
        assert restored.priority == Priority.CRITICAL


class TestGenerateMessageId:
    """Message ID generation."""

    def test_format(self) -> None:
        mid = generate_message_id()
        assert mid.startswith("msg_")
        parts = mid.split("_")
        assert len(parts) == 3

    def test_uniqueness(self) -> None:
        ids = {generate_message_id() for _ in range(100)}
        assert len(ids) == 100


class TestEntityModel:
    """Entity model."""

    def test_entity_defaults(self) -> None:
        e = Entity(id="agent://a1", name="Agent One", type=EntityType.AGENT)
        assert e.roles == []
        assert e.capabilities == []
        assert e.status == "active"


class TestChannelModel:
    """Channel model."""

    def test_channel_defaults(self) -> None:
        c = Channel(name="ops")
        assert c.members == []
        assert c.description == ""


class TestCursorModel:
    """Cursor model."""

    def test_cursor_defaults(self) -> None:
        c = Cursor(consumer="agent://a1", stream="arc.channel.ops")
        assert c.seq == 0
        assert c.byte_pos == 0


class TestAuditRecordModel:
    """AuditRecord model."""

    def test_audit_record_fields(self) -> None:
        r = AuditRecord(
            audit_seq=1,
            event_type="message.sent",
            subject="arc.channel.ops",
            actor_id="agent://a1",
            timestamp_utc="2026-02-17T00:00:00Z",
            detail="sent message",
        )
        assert r.classification == "UNCLASSIFIED"
        assert r.hmac_sha256 == ""
