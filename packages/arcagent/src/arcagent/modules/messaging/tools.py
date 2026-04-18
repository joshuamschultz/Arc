"""LLM-callable tools for the messaging module.

Creates RegisteredTool instances that the LLM can call to send/receive
messages, discover entities, manage channels, and store team files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.messaging.config import MessagingConfig

_logger = logging.getLogger("arcagent.messaging.tools")

# Messenger uses this collection name for streams — must match
# arcteam.messenger.STREAMS_COLLECTION. Kept local to avoid a hard
# import dependency on arcteam from the tools surface.
_STREAMS_COLLECTION = "streams"


async def _stream_end_byte_pos(svc: Any, stream: str) -> int:
    """Best-effort fetch of the stream end byte offset.

    Falls back to ``0`` if the backend doesn't expose the helper
    (older backend implementations). Never raises — cursor seek is an
    optimization, not a correctness guarantee.
    """
    backend = getattr(svc, "_backend", None)
    get_end = getattr(backend, "get_stream_end_byte_pos", None)
    if get_end is None:
        return 0
    try:
        return int(await get_end(_STREAMS_COLLECTION, stream))
    except Exception:
        _logger.debug("stream end byte_pos fetch failed; using 0", exc_info=True)
        return 0


def create_messaging_tools(
    svc: Any,  # MessagingService — late import avoids hard dependency
    registry: Any,  # EntityRegistry
    config: MessagingConfig,
    team_root: Path | None = None,
) -> list[RegisteredTool]:
    """Create the messaging tools for the LLM to call."""

    # Lazy import to avoid hard dependency at module load time.
    # arcteam is an optional dependency — only required when module is enabled.
    from arcteam.types import Message, MsgType, Priority

    entity_id = config.entity_id
    entity_name = config.entity_name or entity_id

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
            return json.dumps(
                {
                    "id": sent.id,
                    "thread_id": sent.thread_id,
                    "seq": sent.seq,
                    "status": "sent",
                }
            )
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
                        # SPEC-017 R-005: store the real end-of-stream
                        # byte offset so the next poll can seek past
                        # already-consumed bytes.
                        byte_pos = await _stream_end_byte_pos(svc, stream)
                        await svc.ack(
                            stream, entity_id, seq=last.seq, byte_pos=byte_pos
                        )

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
            return json.dumps(
                [
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
                ]
            )
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_list_entities(**kwargs: Any) -> str:
        """List all registered entities in the team."""
        try:
            entities = await registry.list_entities()
            return json.dumps(
                [
                    {
                        "id": e.id,
                        "name": e.name,
                        "type": e.type,
                        "roles": e.roles,
                        "capabilities": e.capabilities,
                        "status": e.status,
                    }
                    for e in entities
                ]
            )
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_list_channels(**kwargs: Any) -> str:
        """List all available channels."""
        try:
            channels = await svc.list_channels()
            return json.dumps(
                [
                    {
                        "name": ch.name,
                        "description": ch.description,
                        "members": ch.members,
                    }
                    for ch in channels
                ]
            )
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    # -- Team file storage tool (requires team_root) --

    async def _handle_store_team_file(
        file_path: str = "",
        **kwargs: Any,
    ) -> str:
        """Store a file in the team's shared directory."""
        if team_root is None:
            return json.dumps({"error": "Team root not configured"})
        try:
            from arcteam.files import TeamFileStore

            store = TeamFileStore(team_root)
            result = await store.store(
                source_path=Path(file_path),
                agent_name=entity_name,
            )
            return json.dumps({"status": "stored", **result})
        except (FileNotFoundError, ValueError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_list_team_files(
        agent_name: str = "",
        **kwargs: Any,
    ) -> str:
        """List files in the team's shared directory."""
        if team_root is None:
            return json.dumps({"error": "Team root not configured"})
        try:
            from arcteam.files import TeamFileStore

            store = TeamFileStore(team_root)
            files = await store.list_files(
                agent_name=agent_name or None,
            )
            return json.dumps({"files": files, "count": len(files)})
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

    tools = [
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
                "List all available messaging channels, their descriptions, and current members."
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

    # Only register file tools when team_root is available
    if team_root is not None:
        tools.extend([
            RegisteredTool(
                name="store_team_file",
                description=(
                    "Store a file in the team's shared directory so other agents "
                    "can access it. Use the file path from a received attachment."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": (
                                "Path to the file to share (e.g. from a downloaded "
                                "Slack/Telegram attachment in the inbox)."
                            ),
                        },
                    },
                    "required": ["file_path"],
                },
                transport=ToolTransport.NATIVE,
                execute=_handle_store_team_file,
                timeout_seconds=30,
                source="messaging",
            ),
            RegisteredTool(
                name="list_team_files",
                description=(
                    "List files in the team's shared directory. "
                    "Optionally filter by agent name."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": (
                                "Filter to a specific agent's files. "
                                "Leave empty to list all shared files."
                            ),
                        },
                    },
                },
                transport=ToolTransport.NATIVE,
                execute=_handle_list_team_files,
                timeout_seconds=30,
                source="messaging",
            ),
        ])

    return tools
