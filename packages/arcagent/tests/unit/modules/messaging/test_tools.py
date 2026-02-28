"""Unit tests for messaging module tools."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.messaging import MessagingModule
from tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_ctx,
    make_team_config,
)


def _find_tool(tools: list, name: str):
    """Find a tool by name from registered calls."""
    for call in tools:
        tool = call.args[0]
        if tool.name == name:
            return tool
    msg = f"Tool '{name}' not found"
    raise ValueError(msg)


@pytest.fixture
async def started_module(tmp_path: Path):
    """Start a messaging module and yield (module, ctx) for tool tests."""
    config = make_config_dict(
        entity_id="agent://tool_tester",
        entity_name="Tool Tester",
        roles=["executor"],
        capabilities=["task-execution"],
    )
    team_config = make_team_config(str(tmp_path / "team"))
    module = MessagingModule(
        config=config,
        team_config=team_config,
        telemetry=MagicMock(),
        workspace=tmp_path,
    )
    ctx = make_ctx(tmp_path)
    await module.startup(ctx)
    yield module, ctx
    await module.shutdown()


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_messaging_tools_registered(
        self,
        started_module,
    ) -> None:
        _, ctx = started_module
        calls = ctx.tool_registry.register.call_args_list
        names = [c.args[0].name for c in calls]
        assert len(names) == 7
        assert "messaging_send" in names
        assert "messaging_check_inbox" in names
        assert "messaging_read_thread" in names
        assert "messaging_list_entities" in names
        assert "messaging_list_channels" in names
        assert "store_team_file" in names
        assert "list_team_files" in names


class TestSendTool:
    @pytest.mark.asyncio
    async def test_send_to_another_agent(
        self,
        started_module,
        tmp_path: Path,
    ) -> None:
        module, ctx = started_module
        # Register a second agent so we can message them.
        from arcteam.types import Entity, EntityType

        await module._registry.register(
            Entity(
                id="agent://brad",
                name="Brad",
                type=EntityType.AGENT,
                roles=["executor"],
            )
        )

        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_send",
        )
        result = await tool.execute(
            to="agent://brad",
            body="Hello Brad!",
        )
        data = json.loads(result)
        assert data["status"] == "sent"
        assert "id" in data
        assert "thread_id" in data

    @pytest.mark.asyncio
    async def test_send_no_recipient_errors(
        self,
        started_module,
    ) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_send",
        )
        result = await tool.execute(to="", body="hello")
        data = json.loads(result)
        assert "error" in data


class TestCheckInboxTool:
    @pytest.mark.asyncio
    async def test_empty_inbox(self, started_module) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_check_inbox",
        )
        result = await tool.execute()
        data = json.loads(result)
        assert data["unread"] == 0

    @pytest.mark.asyncio
    async def test_inbox_with_message(
        self,
        started_module,
    ) -> None:
        module, ctx = started_module
        # Register sender and send a message to our agent.
        from arcteam.types import Entity, EntityType, Message

        await module._registry.register(
            Entity(
                id="agent://sender",
                name="Sender",
                type=EntityType.AGENT,
            )
        )
        await module._svc.send(
            Message(
                sender="agent://sender",
                to=["agent://tool_tester"],
                body="You have a task",
            )
        )

        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_check_inbox",
        )
        result = await tool.execute()
        data = json.loads(result)
        assert data["unread"] >= 1


class TestCheckInboxThreadContext:
    @pytest.mark.asyncio
    async def test_reply_includes_thread_context(
        self,
        started_module,
    ) -> None:
        """When a reply arrives, check_inbox includes prior thread messages."""
        module, ctx = started_module
        from arcteam.types import Entity, EntityType, Message

        await module._registry.register(
            Entity(
                id="agent://alice",
                name="Alice",
                type=EntityType.AGENT,
            )
        )

        # Step 1: Alice sends original message to our agent.
        original = await module._svc.send(
            Message(
                sender="agent://alice",
                to=["agent://tool_tester"],
                body="Please do X then report to user://josh",
            )
        )

        # Ack the original so it's "read".
        await module._svc.ack(
            "arc.agent.tool_tester",
            "agent://tool_tester",
            seq=original.seq,
            byte_pos=0,
        )

        # Step 2: Our agent sends request to alice (part of multi-step).
        await module._svc.send(
            Message(
                sender="agent://tool_tester",
                to=["agent://alice"],
                body="What are your capabilities?",
            )
        )

        # Step 3: Alice replies in the same thread.
        reply = await module._svc.send(
            Message(
                sender="agent://alice",
                to=["agent://tool_tester"],
                body="Here are my capabilities: ...",
                thread_id=original.id,
            )
        )

        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_check_inbox",
        )
        result = await tool.execute()
        data = json.loads(result)

        assert data["unread"] >= 1
        stream_msgs = data["streams"]["arc.agent.tool_tester"]
        # Find the reply message.
        reply_msg = [m for m in stream_msgs if m["id"] == reply.id]
        assert len(reply_msg) == 1
        assert "thread_context" in reply_msg[0]
        # Thread context should include the original message.
        senders = [t["sender"] for t in reply_msg[0]["thread_context"]]
        assert "agent://alice" in senders

    @pytest.mark.asyncio
    async def test_no_thread_context_for_new_messages(
        self,
        started_module,
    ) -> None:
        """New messages (thread_id == id) don't include thread_context."""
        module, ctx = started_module
        from arcteam.types import Entity, EntityType, Message

        await module._registry.register(
            Entity(
                id="agent://bob",
                name="Bob",
                type=EntityType.AGENT,
            )
        )

        await module._svc.send(
            Message(
                sender="agent://bob",
                to=["agent://tool_tester"],
                body="Fresh message, no reply",
            )
        )

        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_check_inbox",
        )
        result = await tool.execute()
        data = json.loads(result)
        msg = data["streams"]["arc.agent.tool_tester"][0]
        assert "thread_context" not in msg


class TestListEntitiesTool:
    @pytest.mark.asyncio
    async def test_list_entities(self, started_module) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_list_entities",
        )
        result = await tool.execute()
        data = json.loads(result)
        # At least our own agent should be registered.
        assert len(data) >= 1
        ids = [e["id"] for e in data]
        assert "agent://tool_tester" in ids


class TestListChannelsTool:
    @pytest.mark.asyncio
    async def test_list_channels_empty(
        self,
        started_module,
    ) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_list_channels",
        )
        result = await tool.execute()
        data = json.loads(result)
        assert data == []

    @pytest.mark.asyncio
    async def test_list_channels_after_create(
        self,
        started_module,
    ) -> None:
        module, ctx = started_module
        from arcteam.types import Channel

        await module._svc.create_channel(
            Channel(
                name="ops",
                description="Operations",
                members=["agent://tool_tester"],
            )
        )

        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_list_channels",
        )
        result = await tool.execute()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "ops"
