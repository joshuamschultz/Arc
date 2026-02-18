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
    async def test_nine_tools_registered(
        self, started_module,
    ) -> None:
        _, ctx = started_module
        calls = ctx.tool_registry.register.call_args_list
        names = [c.args[0].name for c in calls]
        assert len(names) == 9
        assert "messaging_send" in names
        assert "messaging_check_inbox" in names
        assert "messaging_read_thread" in names
        assert "messaging_list_entities" in names
        assert "messaging_list_channels" in names
        assert "task_create" in names
        assert "task_list" in names
        assert "task_update" in names
        assert "task_complete" in names


class TestSendTool:
    @pytest.mark.asyncio
    async def test_send_to_another_agent(
        self, started_module, tmp_path: Path,
    ) -> None:
        module, ctx = started_module
        # Register a second agent so we can message them.
        from arcteam.types import Entity, EntityType

        await module._registry.register(Entity(
            id="agent://brad",
            name="Brad",
            type=EntityType.AGENT,
            roles=["executor"],
        ))

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
        self, started_module,
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
        self, started_module,
    ) -> None:
        module, ctx = started_module
        # Register sender and send a message to our agent.
        from arcteam.types import Entity, EntityType, Message

        await module._registry.register(Entity(
            id="agent://sender",
            name="Sender",
            type=EntityType.AGENT,
        ))
        await module._svc.send(Message(
            sender="agent://sender",
            to=["agent://tool_tester"],
            body="You have a task",
        ))

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
        self, started_module,
    ) -> None:
        """When a reply arrives, check_inbox includes prior thread messages."""
        module, ctx = started_module
        from arcteam.types import Entity, EntityType, Message

        await module._registry.register(Entity(
            id="agent://alice",
            name="Alice",
            type=EntityType.AGENT,
        ))

        # Step 1: Alice sends original message to our agent.
        original = await module._svc.send(Message(
            sender="agent://alice",
            to=["agent://tool_tester"],
            body="Please do X then report to user://josh",
        ))

        # Ack the original so it's "read".
        await module._svc.ack(
            "arc.agent.tool_tester", "agent://tool_tester",
            seq=original.seq, byte_pos=0,
        )

        # Step 2: Our agent sends request to alice (part of multi-step).
        outbound = await module._svc.send(Message(
            sender="agent://tool_tester",
            to=["agent://alice"],
            body="What are your capabilities?",
        ))

        # Step 3: Alice replies in the same thread.
        reply = await module._svc.send(Message(
            sender="agent://alice",
            to=["agent://tool_tester"],
            body="Here are my capabilities: ...",
            thread_id=original.id,
        ))

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
        self, started_module,
    ) -> None:
        """New messages (thread_id == id) don't include thread_context."""
        module, ctx = started_module
        from arcteam.types import Entity, EntityType, Message

        await module._registry.register(Entity(
            id="agent://bob",
            name="Bob",
            type=EntityType.AGENT,
        ))

        await module._svc.send(Message(
            sender="agent://bob",
            to=["agent://tool_tester"],
            body="Fresh message, no reply",
        ))

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
        self, started_module,
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
        self, started_module,
    ) -> None:
        module, ctx = started_module
        from arcteam.types import Channel

        await module._svc.create_channel(Channel(
            name="ops",
            description="Operations",
            members=["agent://tool_tester"],
        ))

        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "messaging_list_channels",
        )
        result = await tool.execute()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "ops"


# --- Task Management Tools ---


class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_create_task_returns_id(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        result = await tool.execute(
            description="Ask my_agent about capabilities",
            report_to="user://josh",
        )
        data = json.loads(result)
        assert "id" in data
        assert data["status"] == "pending"
        assert data["description"] == "Ask my_agent about capabilities"
        assert data["report_to"] == "user://josh"

    @pytest.mark.asyncio
    async def test_create_task_persists_to_file(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        await tool.execute(
            description="Step 1: gather info",
        )
        # Verify file exists with the task
        tasks_file = tmp_path / "tasks.json"
        assert tasks_file.exists()
        tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
        assert len(tasks) == 1
        assert tasks[0]["description"] == "Step 1: gather info"

    @pytest.mark.asyncio
    async def test_create_multiple_tasks_increments_id(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r1 = json.loads(await tool.execute(description="Task 1"))
        r2 = json.loads(await tool.execute(description="Task 2"))
        assert r1["id"] != r2["id"]
        # Both should be in the file
        tasks = json.loads(
            (tmp_path / "tasks.json").read_text(encoding="utf-8"),
        )
        assert len(tasks) == 2


class TestTaskList:
    @pytest.mark.asyncio
    async def test_list_empty(self, started_module) -> None:
        _, ctx = started_module
        tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_list",
        )
        result = await tool.execute()
        data = json.loads(result)
        assert data["tasks"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_returns_created_tasks(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        await create.execute(description="Task A")
        await create.execute(description="Task B")

        list_tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_list",
        )
        result = await list_tool.execute()
        data = json.loads(result)
        assert data["total"] == 2
        descriptions = [t["description"] for t in data["tasks"]]
        assert "Task A" in descriptions
        assert "Task B" in descriptions

    @pytest.mark.asyncio
    async def test_list_filter_by_status(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r1 = json.loads(await create.execute(description="Task A"))
        await create.execute(description="Task B")

        update = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_update",
        )
        await update.execute(id=r1["id"], status="in_progress")

        list_tool = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_list",
        )
        result = await list_tool.execute(status="pending")
        data = json.loads(result)
        assert data["total"] == 1
        assert data["tasks"][0]["description"] == "Task B"


class TestTaskUpdate:
    @pytest.mark.asyncio
    async def test_update_status(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r = json.loads(await create.execute(description="Do work"))
        task_id = r["id"]

        update = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_update",
        )
        result = await update.execute(id=task_id, status="in_progress")
        data = json.loads(result)
        assert data["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_update_nonexistent_task(
        self, started_module,
    ) -> None:
        _, ctx = started_module
        update = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_update",
        )
        result = await update.execute(id="nonexistent", status="done")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_persists(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r = json.loads(await create.execute(description="Task"))
        update = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_update",
        )
        await update.execute(id=r["id"], status="in_progress")
        # Re-read from disk
        tasks = json.loads(
            (tmp_path / "tasks.json").read_text(encoding="utf-8"),
        )
        assert tasks[0]["status"] == "in_progress"


class TestTaskComplete:
    @pytest.mark.asyncio
    async def test_complete_marks_done(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r = json.loads(await create.execute(description="Gather info"))
        complete = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_complete",
        )
        result = await complete.execute(
            id=r["id"],
            result="Info gathered successfully",
        )
        data = json.loads(result)
        assert data["status"] == "done"
        assert data["result"] == "Info gathered successfully"

    @pytest.mark.asyncio
    async def test_complete_auto_sends_to_report_to(
        self, started_module, tmp_path: Path,
    ) -> None:
        module, ctx = started_module
        # Register the target entity
        from arcteam.types import Entity, EntityType

        await module._registry.register(Entity(
            id="user://josh",
            name="Josh",
            type=EntityType.USER,
        ))

        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r = json.loads(await create.execute(
            description="Ask agent about caps",
            report_to="user://josh",
        ))
        complete = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_complete",
        )
        result = await complete.execute(
            id=r["id"],
            result="Agent has: file ops, browser, memory",
        )
        data = json.loads(result)
        assert data["status"] == "done"
        assert data.get("sent_to") == "user://josh"

        # Verify message was sent to user://josh's stream
        from arcteam.storage import FileBackend

        team_root = module._resolve_team_root()
        backend = FileBackend(team_root)
        msgs = await backend.read_stream(
            "messages/streams", "arc.agent.josh", after_seq=0,
        )
        assert len(msgs) >= 1
        bodies = [m.get("body", "") for m in msgs]
        assert any("Agent has: file ops, browser, memory" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_complete_without_report_to(
        self, started_module, tmp_path: Path,
    ) -> None:
        _, ctx = started_module
        create = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_create",
        )
        r = json.loads(await create.execute(description="Solo task"))
        complete = _find_tool(
            ctx.tool_registry.register.call_args_list,
            "task_complete",
        )
        result = await complete.execute(id=r["id"], result="Done")
        data = json.loads(result)
        assert data["status"] == "done"
        assert "sent_to" not in data
