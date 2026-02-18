"""LLM-callable tools for the messaging module.

Creates RegisteredTool instances that the LLM can call to send/receive
messages, discover entities, manage channels, and manage tasks.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.messaging.config import MessagingConfig

_logger = logging.getLogger("arcagent.messaging.tools")


def create_messaging_tools(
    svc: Any,  # MessagingService — late import avoids hard dependency
    registry: Any,  # EntityRegistry
    config: MessagingConfig,
) -> list[RegisteredTool]:
    """Create the messaging tools for the LLM to call."""

    # Lazy import to avoid hard dependency at module load time.
    # arcteam is an optional dependency — only required when module is enabled.
    from arcteam.types import Message, MsgType, Priority

    entity_id = config.entity_id

    async def _handle_send(
        to: str = "",
        body: str = "",
        msg_type: str = "info",
        priority: str = "normal",
        thread_id: str | None = None,
        action_required: bool = False,
        **kwargs: Any,
    ) -> str:
        """Send a message to an entity, channel, or role."""
        try:
            # Validate target(s) — accept comma-separated for multi-target.
            targets = [t.strip() for t in to.split(",") if t.strip()]
            if not targets:
                return json.dumps({"error": "No recipients specified"})

            msg = Message(
                sender=entity_id,
                to=targets,
                body=body,
                msg_type=MsgType(msg_type),
                priority=Priority(priority),
                thread_id=thread_id,
                action_required=action_required,
            )
            sent = await svc.send(msg)
            _logger.info("Sent message %s to %s", sent.id, to)
            return json.dumps({
                "id": sent.id,
                "thread_id": sent.thread_id,
                "seq": sent.seq,
                "status": "sent",
            })
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_check_inbox(**kwargs: Any) -> str:
        """Check all subscribed streams for unread messages."""
        try:
            inbox = await svc.poll_all(
                entity_id,
                max_per_stream=config.max_messages_per_poll,
            )
            if not inbox:
                return json.dumps({"unread": 0, "streams": {}})

            result: dict[str, Any] = {"unread": 0, "streams": {}}
            for stream, msgs in inbox.items():
                result["unread"] += len(msgs)
                stream_msgs: list[dict[str, Any]] = []
                for m in msgs:
                    msg_data: dict[str, Any] = {
                        "seq": m.seq,
                        "id": m.id,
                        "sender": m.sender,
                        "body": m.body[:200],
                        "msg_type": m.msg_type,
                        "priority": m.priority,
                        "action_required": m.action_required,
                        "thread_id": m.thread_id,
                        "ts": m.ts,
                    }
                    # Include prior thread context for replies so the agent
                    # sees the full conversation (who originally asked, why).
                    if m.thread_id and m.thread_id != m.id:
                        thread = await svc.get_thread(stream, m.thread_id)
                        prior = [
                            {
                                "seq": t.seq,
                                "sender": t.sender,
                                "body": t.body[:200],
                                "ts": t.ts,
                            }
                            for t in thread
                            if t.seq < m.seq
                        ]
                        if prior:
                            msg_data["thread_context"] = prior
                    stream_msgs.append(msg_data)
                result["streams"][stream] = stream_msgs

            # Auto-ack if configured.
            if config.auto_ack:
                for stream, msgs in inbox.items():
                    if msgs:
                        last = msgs[-1]
                        await svc.ack(stream, entity_id, seq=last.seq, byte_pos=0)

            return json.dumps(result)
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_read_thread(
        stream: str = "",
        thread_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Read all messages in a thread."""
        try:
            if not stream or not thread_id:
                return json.dumps({"error": "stream and thread_id are required"})
            msgs = await svc.get_thread(stream, thread_id)
            return json.dumps([
                {
                    "seq": m.seq,
                    "id": m.id,
                    "sender": m.sender,
                    "body": m.body,
                    "msg_type": m.msg_type,
                    "ts": m.ts,
                    "thread_id": m.thread_id,
                }
                for m in msgs
            ])
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_list_entities(**kwargs: Any) -> str:
        """List all registered entities in the team."""
        try:
            entities = await registry.list_entities()
            return json.dumps([
                {
                    "id": e.id,
                    "name": e.name,
                    "type": e.type,
                    "roles": e.roles,
                    "capabilities": e.capabilities,
                    "status": e.status,
                }
                for e in entities
            ])
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_list_channels(**kwargs: Any) -> str:
        """List all available channels."""
        try:
            channels = await svc.list_channels()
            return json.dumps([
                {
                    "name": ch.name,
                    "description": ch.description,
                    "members": ch.members,
                }
                for ch in channels
            ])
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    return [
        RegisteredTool(
            name="messaging_send",
            description=(
                "Send a message to another agent, user, channel, or role. "
                "Use agent://name for direct messages, channel://name for channels, "
                "role://name for role-based broadcast."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": (
                            "Recipient URI (e.g. agent://brad_agent, "
                            "channel://ops, role://executor). "
                            "Comma-separate for multiple recipients."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": "Message body text",
                    },
                    "msg_type": {
                        "type": "string",
                        "enum": ["info", "request", "task", "result", "alert", "ack"],
                        "description": "Message type classification",
                        "default": "info",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "critical"],
                        "description": "Message priority",
                        "default": "normal",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": (
                            "Thread ID to continue a conversation. "
                            "Use the thread_id from the original message."
                        ),
                    },
                    "action_required": {
                        "type": "boolean",
                        "description": "Whether recipient needs to take action",
                        "default": False,
                    },
                },
                "required": ["to", "body"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_send,
            timeout_seconds=30,
            source="messaging",
        ),
        RegisteredTool(
            name="messaging_check_inbox",
            description=(
                "Check your inbox for unread messages across all subscribed "
                "streams (DMs, channels, role broadcasts). Returns unread count "
                "and message summaries."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_check_inbox,
            timeout_seconds=30,
            source="messaging",
        ),
        RegisteredTool(
            name="messaging_read_thread",
            description=(
                "Read the full conversation thread for a given thread ID. "
                "Returns all messages in chronological order."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "stream": {
                        "type": "string",
                        "description": (
                            "Stream name (e.g. arc.agent.brad_agent, arc.channel.ops)"
                        ),
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to read",
                    },
                },
                "required": ["stream", "thread_id"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_read_thread,
            timeout_seconds=30,
            source="messaging",
        ),
        RegisteredTool(
            name="messaging_list_entities",
            description=(
                "List all registered entities (agents and users) in the team. "
                "Shows their roles and capabilities for discovery."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_list_entities,
            timeout_seconds=30,
            source="messaging",
        ),
        RegisteredTool(
            name="messaging_list_channels",
            description=(
                "List all available messaging channels, their descriptions, "
                "and current members."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_list_channels,
            timeout_seconds=30,
            source="messaging",
        ),
    ]


def _load_tasks(tasks_path: Path) -> list[dict[str, Any]]:
    """Read tasks from workspace file."""
    if not tasks_path.exists():
        return []
    try:
        return json.loads(tasks_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_tasks(tasks_path: Path, tasks: list[dict[str, Any]]) -> None:
    """Persist tasks to workspace file."""
    tasks_path.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def create_task_tools(
    svc: Any,  # MessagingService — for auto-send on complete
    config: MessagingConfig,
    workspace: Path,
) -> list[RegisteredTool]:
    """Create task management tools for the LLM to call."""

    from arcteam.types import Message, MsgType, Priority

    entity_id = config.entity_id
    tasks_path = workspace / "tasks.json"

    async def _handle_task_create(
        description: str = "",
        report_to: str = "",
        **kwargs: Any,
    ) -> str:
        """Create a new task in the agent's task list."""
        if not description:
            return json.dumps({"error": "description is required"})

        tasks = _load_tasks(tasks_path)
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task: dict[str, Any] = {
            "id": task_id,
            "description": description,
            "status": "pending",
        }
        if report_to:
            task["report_to"] = report_to

        tasks.append(task)
        _save_tasks(tasks_path, tasks)

        _logger.info("Created task %s: %s", task_id, description)
        return json.dumps(task)

    async def _handle_task_list(
        status: str = "",
        **kwargs: Any,
    ) -> str:
        """List tasks, optionally filtered by status."""
        tasks = _load_tasks(tasks_path)
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        return json.dumps({"tasks": tasks, "total": len(tasks)})

    async def _handle_task_update(
        id: str = "",
        status: str = "",
        description: str = "",
        **kwargs: Any,
    ) -> str:
        """Update a task's status or description."""
        if not id:
            return json.dumps({"error": "id is required"})

        tasks = _load_tasks(tasks_path)
        target = None
        for t in tasks:
            if t["id"] == id:
                target = t
                break

        if target is None:
            return json.dumps({"error": f"Task not found: {id}"})

        if status:
            target["status"] = status
        if description:
            target["description"] = description

        _save_tasks(tasks_path, tasks)
        _logger.info("Updated task %s: status=%s", id, target["status"])
        return json.dumps(target)

    async def _handle_task_complete(
        id: str = "",
        result: str = "",
        **kwargs: Any,
    ) -> str:
        """Mark a task done and auto-send result to report_to if set."""
        if not id:
            return json.dumps({"error": "id is required"})

        tasks = _load_tasks(tasks_path)
        target = None
        for t in tasks:
            if t["id"] == id:
                target = t
                break

        if target is None:
            return json.dumps({"error": f"Task not found: {id}"})

        target["status"] = "done"
        target["result"] = result
        _save_tasks(tasks_path, tasks)

        response: dict[str, Any] = {
            "id": id,
            "status": "done",
            "result": result,
        }

        # Auto-send result to report_to entity.
        report_to = target.get("report_to", "")
        if report_to:
            try:
                msg = Message(
                    sender=entity_id,
                    to=[report_to],
                    body=result,
                    msg_type=MsgType.RESULT,
                    priority=Priority.NORMAL,
                )
                await svc.send(msg)
                response["sent_to"] = report_to
                _logger.info(
                    "Task %s done, result sent to %s", id, report_to,
                )
            except (ValueError, TypeError) as exc:
                response["send_error"] = str(exc)
                _logger.warning(
                    "Task %s done but failed to send result to %s: %s",
                    id, report_to, exc,
                )

        return json.dumps(response)

    return [
        RegisteredTool(
            name="task_create",
            description=(
                "Create a new task in your task list. Use this to plan "
                "multi-step work that persists across turns. Set report_to "
                "to auto-send results when complete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What needs to be done",
                    },
                    "report_to": {
                        "type": "string",
                        "description": (
                            "Entity URI to auto-send results to when "
                            "task completes (e.g. user://josh, agent://lead)"
                        ),
                    },
                },
                "required": ["description"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_task_create,
            timeout_seconds=10,
            source="messaging",
        ),
        RegisteredTool(
            name="task_list",
            description=(
                "List your current tasks. Optionally filter by status "
                "(pending, in_progress, waiting, done)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "waiting", "done"],
                        "description": "Filter by status",
                    },
                },
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_task_list,
            timeout_seconds=10,
            source="messaging",
        ),
        RegisteredTool(
            name="task_update",
            description=(
                "Update a task's status or description. Use status values: "
                "pending, in_progress, waiting, done."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Task ID to update",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "waiting", "done"],
                        "description": "New status",
                    },
                    "description": {
                        "type": "string",
                        "description": "Updated description",
                    },
                },
                "required": ["id"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_task_update,
            timeout_seconds=10,
            source="messaging",
        ),
        RegisteredTool(
            name="task_complete",
            description=(
                "Mark a task as done and record the result. If the task "
                "has a report_to entity, automatically sends the result "
                "as a message to that entity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Task ID to complete",
                    },
                    "result": {
                        "type": "string",
                        "description": "Summary of what was accomplished",
                    },
                },
                "required": ["id", "result"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_task_complete,
            timeout_seconds=30,
            source="messaging",
        ),
    ]
