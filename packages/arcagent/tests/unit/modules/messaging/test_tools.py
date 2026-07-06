"""Unit tests for messaging module tools.

Exercises the live ``create_messaging_tools`` factory directly: the runtime is
bootstrapped via ``_runtime.configure`` (the same wiring the capability path
uses) and the tools are built over its ``MessagingService``/``EntityRegistry``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity

from arcagent.modules.messaging import _bootstrap, _runtime
from arcagent.modules.messaging.tools import create_messaging_tools
from tests.unit.modules.messaging.conftest import make_config_dict, make_peer_entity


def _find_tool(tools: list, name: str):
    """Find a tool by name from the created tool list."""
    for tool in tools:
        if tool.name == name:
            return tool
    msg = f"Tool '{name}' not found"
    raise ValueError(msg)


@pytest.fixture
async def messaging_tools(tmp_path: Path) -> AsyncIterator[tuple[list, object]]:
    """Bootstrap the live runtime and yield (tools, state) for tool tests."""
    _runtime.reset()
    config = make_config_dict(
        entity_id="agent://tool_tester",
        entity_name="Tool Tester",
        roles=["executor"],
        capabilities=["task-execution"],
    )
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    team_root = tmp_path / "team"
    _runtime.configure(
        config=config,
        telemetry=MagicMock(),
        workspace=tmp_path,
        team_root=team_root,
        agent_name="tool_tester",
        identity=identity,
    )
    st = _runtime.state()
    # Register the agent itself so it can send and appears in the roster.
    await st.registry.register(
        _bootstrap.self_entity(
            entity_id="agent://tool_tester",
            entity_name="Tool Tester",
            handle="tool_tester",
            identity=identity,
            roles=["executor"],
            capabilities=["task-execution"],
        )
    )
    tools = create_messaging_tools(
        svc=st.svc,
        registry=st.registry,
        config=st.config,
        team_root=team_root,
    )
    yield tools, st
    _runtime.reset()


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_messaging_tools_created(self, messaging_tools) -> None:
        tools, _ = messaging_tools
        names = [t.name for t in tools]
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
    async def test_send_to_another_agent(self, messaging_tools) -> None:
        tools, st = messaging_tools
        # Register a second agent so we can message them.
        await st.registry.register(make_peer_entity("brad", "Brad", roles=["executor"]))

        tool = _find_tool(tools, "messaging_send")
        result = await tool.execute(to="agent://brad", body="Hello Brad!")
        data = json.loads(result)
        assert data["status"] == "sent"
        assert "id" in data
        assert "thread_id" in data

    @pytest.mark.asyncio
    async def test_send_no_recipient_errors(self, messaging_tools) -> None:
        tools, _ = messaging_tools
        tool = _find_tool(tools, "messaging_send")
        result = await tool.execute(to="", body="hello")
        data = json.loads(result)
        assert "error" in data


class TestCheckInboxTool:
    @pytest.mark.asyncio
    async def test_empty_inbox(self, messaging_tools) -> None:
        tools, _ = messaging_tools
        tool = _find_tool(tools, "messaging_check_inbox")
        result = await tool.execute()
        data = json.loads(result)
        assert data["unread"] == 0

    @pytest.mark.asyncio
    async def test_inbox_with_message(self, messaging_tools) -> None:
        tools, st = messaging_tools
        from arcteam.types import Message

        await st.registry.register(make_peer_entity("sender", "Sender"))
        await st.svc.send(
            Message(
                sender="agent://sender",
                to=["agent://tool_tester"],
                body="You have a task",
            )
        )

        tool = _find_tool(tools, "messaging_check_inbox")
        result = await tool.execute()
        data = json.loads(result)
        assert data["unread"] >= 1


class TestCheckInboxThreadContext:
    @pytest.mark.asyncio
    async def test_reply_includes_thread_context(self, messaging_tools) -> None:
        """When a reply arrives, check_inbox includes prior thread messages."""
        tools, st = messaging_tools
        from arcteam.types import Message

        await st.registry.register(make_peer_entity("alice", "Alice"))

        # Step 1: Alice sends original message to our agent.
        original = await st.svc.send(
            Message(
                sender="agent://alice",
                to=["agent://tool_tester"],
                body="Please do X then report to user://josh",
            )
        )

        # Ack the original so it's "read".
        await st.svc.ack(
            "arc.agent.tool_tester",
            "agent://tool_tester",
            seq=original.seq,
            byte_pos=0,
        )

        # Step 2: Our agent sends request to alice (part of multi-step).
        await st.svc.send(
            Message(
                sender="agent://tool_tester",
                to=["agent://alice"],
                body="What are your capabilities?",
            )
        )

        # Step 3: Alice replies in the same thread.
        reply = await st.svc.send(
            Message(
                sender="agent://alice",
                to=["agent://tool_tester"],
                body="Here are my capabilities: ...",
                thread_id=original.id,
            )
        )

        tool = _find_tool(tools, "messaging_check_inbox")
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
    async def test_no_thread_context_for_new_messages(self, messaging_tools) -> None:
        """New messages (thread_id == id) don't include thread_context."""
        tools, st = messaging_tools
        from arcteam.types import Message

        await st.registry.register(make_peer_entity("bob", "Bob"))

        await st.svc.send(
            Message(
                sender="agent://bob",
                to=["agent://tool_tester"],
                body="Fresh message, no reply",
            )
        )

        tool = _find_tool(tools, "messaging_check_inbox")
        result = await tool.execute()
        data = json.loads(result)
        msg = data["streams"]["arc.agent.tool_tester"][0]
        assert "thread_context" not in msg


class TestListEntitiesTool:
    @pytest.mark.asyncio
    async def test_list_entities(self, messaging_tools) -> None:
        tools, _ = messaging_tools
        tool = _find_tool(tools, "messaging_list_entities")
        result = await tool.execute()
        data = json.loads(result)
        # At least our own agent should be registered.
        assert len(data) >= 1
        ids = [e["id"] for e in data]
        assert "agent://tool_tester" in ids


class TestListChannelsTool:
    @pytest.mark.asyncio
    async def test_list_channels_empty(self, messaging_tools) -> None:
        tools, _ = messaging_tools
        tool = _find_tool(tools, "messaging_list_channels")
        result = await tool.execute()
        data = json.loads(result)
        assert data == []

    @pytest.mark.asyncio
    async def test_list_channels_after_create(self, messaging_tools) -> None:
        tools, st = messaging_tools
        from arcteam.types import Channel

        await st.svc.create_channel(
            Channel(
                name="ops",
                description="Operations",
                members=["agent://tool_tester"],
            )
        )

        tool = _find_tool(tools, "messaging_list_channels")
        result = await tool.execute()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "ops"
