"""Unit tests for the live messaging tool surface.

Exercises the ``@tool`` decorator functions in
:mod:`arcagent.modules.messaging.capabilities` directly — the same callables
the capability loader registers. The runtime is bootstrapped via
``_runtime.configure`` (the production wiring) so each tool runs over the real
``MessagingService``/``EntityRegistry`` and resolved ``team_root``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity

from arcagent.core import arcteam_bootstrap as _bootstrap
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import (
    list_team_files,
    messaging_check_inbox,
    messaging_list_channels,
    messaging_list_entities,
    messaging_send,
    store_team_file,
)
from tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
    make_peer_entity,
)


@pytest.fixture
async def messaging_state(tmp_path: Path) -> AsyncIterator[_runtime._State]:
    """Bootstrap the live runtime and yield the configured state."""
    _runtime.reset()
    config = make_config_dict(
        entity_id="agent://tool_tester",
        entity_name="tool_tester",
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
        operator_signer=make_operator_signer(),
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
    yield st
    _runtime.reset()


class TestSendTool:
    @pytest.mark.asyncio
    async def test_send_to_another_agent(self, messaging_state: _runtime._State) -> None:
        st = messaging_state
        await st.registry.register(make_peer_entity("brad", "Brad", roles=["executor"]))

        result = await messaging_send(to="agent://brad", body="Hello Brad!")
        data = json.loads(result)
        assert data["status"] == "sent"
        assert "id" in data
        assert "thread_id" in data

    @pytest.mark.asyncio
    async def test_send_no_recipient_errors(self, messaging_state: _runtime._State) -> None:
        result = await messaging_send(to="", body="hello")
        data = json.loads(result)
        assert "error" in data


class TestCheckInboxTool:
    @pytest.mark.asyncio
    async def test_empty_inbox(self, messaging_state: _runtime._State) -> None:
        result = await messaging_check_inbox()
        data = json.loads(result)
        assert data["unread"] == 0

    @pytest.mark.asyncio
    async def test_inbox_with_message(self, messaging_state: _runtime._State) -> None:
        st = messaging_state
        from arcteam.types import Message

        await st.registry.register(make_peer_entity("sender", "Sender"))
        await st.svc.send(
            Message(
                sender="agent://sender",
                to=["agent://tool_tester"],
                body="You have a task",
            )
        )

        result = await messaging_check_inbox()
        data = json.loads(result)
        assert data["unread"] >= 1


class TestCheckInboxThreadContext:
    @pytest.mark.asyncio
    async def test_reply_includes_thread_context(self, messaging_state: _runtime._State) -> None:
        """When a reply arrives, check_inbox includes prior thread messages."""
        st = messaging_state
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

        result = await messaging_check_inbox()
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
    async def test_no_thread_context_for_new_messages(self, messaging_state: _runtime._State) -> None:
        """New messages (thread_id == id) don't include thread_context."""
        st = messaging_state
        from arcteam.types import Message

        await st.registry.register(make_peer_entity("bob", "Bob"))

        await st.svc.send(
            Message(
                sender="agent://bob",
                to=["agent://tool_tester"],
                body="Fresh message, no reply",
            )
        )

        result = await messaging_check_inbox()
        data = json.loads(result)
        msg = data["streams"]["arc.agent.tool_tester"][0]
        assert "thread_context" not in msg


class TestListEntitiesTool:
    @pytest.mark.asyncio
    async def test_list_entities(self, messaging_state: _runtime._State) -> None:
        result = await messaging_list_entities()
        data = json.loads(result)
        # At least our own agent should be registered.
        assert len(data) >= 1
        ids = [e["id"] for e in data]
        assert "agent://tool_tester" in ids


class TestListChannelsTool:
    @pytest.mark.asyncio
    async def test_list_channels_empty(self, messaging_state: _runtime._State) -> None:
        result = await messaging_list_channels()
        data = json.loads(result)
        assert data == []

    @pytest.mark.asyncio
    async def test_list_channels_after_create(self, messaging_state: _runtime._State) -> None:
        st = messaging_state
        from arcteam.types import Channel

        await st.svc.create_channel(
            Channel(
                name="ops",
                description="Operations",
                members=["agent://tool_tester"],
            )
        )

        result = await messaging_list_channels()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "ops"


class TestTeamFileTools:
    @pytest.mark.asyncio
    async def test_store_then_list_team_file(self, messaging_state: _runtime._State, tmp_path: Path) -> None:
        """store_team_file shares a file that list_team_files then reports."""
        source = tmp_path / "artifact.txt"
        source.write_text("shared payload")

        stored = json.loads(await store_team_file(file_path=str(source)))
        assert stored["status"] == "stored"

        listed = json.loads(await list_team_files())
        assert listed["count"] >= 1
        filenames = [f["filename"] for f in listed["files"]]
        assert "artifact.txt" in filenames

    @pytest.mark.asyncio
    async def test_store_team_file_missing_source_errors(self, messaging_state: _runtime._State) -> None:
        result = await store_team_file(file_path="/nonexistent/nope.txt")
        data = json.loads(result)
        assert "error" in data
