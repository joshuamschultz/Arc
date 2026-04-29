"""Decorator-form messaging module — SPEC-021.

Seven capabilities mirror :class:`MessagingModule`'s startup registrations:

  * ``agent:assemble_prompt`` (priority 50)  — inject team context + roster.
  * ``agent:ready``           (priority 100) — bind agent.chat() callback.
  * ``agent:shutdown``        (priority 100) — cancel poll task, log stop.
  * ``messaging_send``        (@tool)        — send a message to entity/channel/role.
  * ``messaging_check_inbox`` (@tool)        — poll all streams for unread messages.
  * ``messaging_read_thread`` (@tool)        — read full conversation thread.
  * ``messaging_list_entities`` (@tool)      — list registered team entities.
  * ``messaging_list_channels`` (@tool)      — list available channels.
  * ``messaging_poll_loop``   (@background_task, interval=1.0) — inbox poller.

File tools (``store_team_file`` / ``list_team_files``) are registered only
when ``team_root`` resolves to an existing path; they are omitted here
because the decorator pattern does not support conditional registration at
decoration time. Use the legacy :class:`MessagingModule` path when file
tools are required, or register them manually post-startup.

State is shared via :mod:`arcagent.modules.messaging._runtime`; the agent
configures it once at startup and the capabilities read it lazily.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from arcagent.modules.messaging import _runtime
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.messaging.capabilities")

# Seconds between poll loop iterations — matches the legacy loop behaviour
# of sleeping config.poll_interval_seconds between cycles, but the
# @background_task scheduler ticks at this rate. The loop body checks the
# config value at runtime so it is still configurable.
_POLL_TICK = 1.0

# Streams collection name — mirrors arcteam.messenger.STREAMS_COLLECTION.
_STREAMS_COLLECTION = "streams"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _stream_end_byte_pos(svc: Any, stream: str) -> int:
    """Best-effort stream-end byte offset for ack cursor.

    Falls back to ``0`` when the backend lacks the helper. Cursor seek
    is an optimisation, not a correctness requirement.
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


async def _build_roster() -> str:
    """Build XML roster from EntityRegistry with TTL caching.

    Uses model_dump(exclude_defaults=True) so new Entity fields appear
    automatically without code changes.
    """
    st = _runtime.state()
    now = time.monotonic()
    ttl = st.config.roster_ttl_seconds

    if st.roster_cache is not None and (now - st.roster_cache_time) < ttl:
        return st.roster_cache

    entities = await st.registry.list_entities()
    if not entities:
        st.roster_cache = ""
        st.roster_cache_time = now
        return ""

    lines = ["<team-roster>"]
    for entity in entities:
        data = entity.model_dump(exclude_defaults=True)
        safe_name = xml_escape(str(data.get("name", "")), {'"': "&quot;"})
        attrs = f'name="{safe_name}"'
        if "id" in data:
            safe_id = xml_escape(str(data["id"]), {'"': "&quot;"})
            attrs += f' id="{safe_id}"'

        lines.append(f"  <entity {attrs}>")
        for key, value in data.items():
            if key in ("name", "id"):
                continue
            # Validate key is a safe XML element name (NCName).
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_\-\.]*", key):
                _logger.warning("Skipping entity field with unsafe key: %r", key)
                continue
            if isinstance(value, list):
                safe_val = xml_escape(", ".join(str(v) for v in value))
            else:
                safe_val = xml_escape(str(value))
            lines.append(f"    <{key}>{safe_val}</{key}>")
        lines.append("  </entity>")

    lines.append("</team-roster>")

    st.roster_cache = "\n".join(lines)
    st.roster_cache_time = now

    if st.telemetry is not None:
        st.telemetry.audit_event(
            "prompt.roster_rebuilt",
            {"entity_count": len(entities)},
        )

    return st.roster_cache


async def _process_inbox(inbox: dict[str, list[Any]]) -> None:
    """Format inbox messages and route through agent.chat().

    Batches all unread messages into a single prompt. Serialised via lock
    to prevent concurrent processing. Acks after successful processing when
    auto_ack is configured.
    """
    st = _runtime.state()

    all_messages: list[dict[str, Any]] = []
    for stream, msgs in inbox.items():
        for m in msgs:
            all_messages.append(
                {
                    "stream": stream,
                    "seq": m.seq,
                    "id": m.id,
                    "sender": m.sender,
                    "body": m.body,
                    "msg_type": m.msg_type,
                    "priority": m.priority,
                    "action_required": m.action_required,
                    "thread_id": m.thread_id,
                    "ts": m.ts,
                }
            )

    if not all_messages:
        return

    async with st.processing_lock:
        lines = ["You have new messages. Read each carefully and act on them:", ""]
        for msg in all_messages:
            # Sanitise all inter-agent fields before prompt interpolation (LLM01).
            sender = sanitize_text(str(msg["sender"]), max_length=200)
            body = sanitize_text(str(msg["body"]), max_length=4000)
            msg_type = sanitize_text(str(msg["msg_type"]), max_length=50)
            priority = sanitize_text(str(msg["priority"]), max_length=50)

            action_flag = " [ACTION REQUIRED]" if msg["action_required"] else ""
            lines.append(f"**From {sender}** ({msg_type}, {priority} priority){action_flag}:")
            lines.append(f"> {body}")
            if msg["thread_id"] and msg["thread_id"] != msg["id"]:
                thread_id = sanitize_text(str(msg["thread_id"]), max_length=200)
                lines.append(f"  (thread: {thread_id})")
            lines.append("")

        lines.append(
            "Process each message: reply to questions, execute tasks, "
            "and report results. Use messaging_send to respond to senders."
        )

        prompt = "\n".join(lines)
        entity_id = st.config.entity_id

        try:
            _logger.info(
                "Processing %d inbox message(s) through agent.chat()",
                len(all_messages),
            )
            await st.agent_chat_fn(prompt)

            if st.config.auto_ack:
                for stream, msgs in inbox.items():
                    if msgs:
                        last = msgs[-1]
                        byte_pos = await _stream_end_byte_pos(st.svc, stream)
                        await st.svc.ack(
                            stream,
                            entity_id,
                            seq=last.seq,
                            byte_pos=byte_pos,
                        )
        except Exception:
            _logger.exception("Failed to process inbox messages via chat")


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


@hook(event="agent:assemble_prompt", priority=50)
async def inject_messaging_sections(ctx: Any) -> None:
    """Inject team messaging behaviour and roster into the system prompt."""
    sections = ctx.data.get("sections")
    if sections is None or not isinstance(sections, dict):
        return
    st = _runtime.state()

    # Sanitise identity fields before prompt interpolation (LLM01, ASI06).
    entity_id = sanitize_text(st.config.entity_id, max_length=200)
    entity_name = sanitize_text(
        st.config.entity_name or st.config.entity_id,
        max_length=200,
    )

    lines = [
        "## Team Messaging",
        "",
        f"You are **{entity_name}** (`{entity_id}`) on a team.",
        "",
        "### Autonomy Principle",
        "",
        "You are an autonomous agent. Work silently and efficiently.",
        "**Do NOT narrate your actions or report routine status.**",
        "Only contact the user (`notify_user`) when you have:",
        "- A meaningful result or finding worth sharing",
        "- A question that requires human judgment",
        "- A blocker that needs human intervention",
        "",
        "If your inbox is empty or a routine check has no findings, "
        "just move on. No notification needed.",
    ]

    if st.last_unread:
        total = sum(st.last_unread.values())
        lines.append("")
        lines.append(f"You have {total} unread message(s). Check inbox and handle them.")
        for stream, count in st.last_unread.items():
            safe_stream = sanitize_text(stream, max_length=200)
            lines.append(f"  - {safe_stream}: {count}")

    lines.extend(
        [
            "",
            "### Communication Rules",
            "",
            "- Reply to `action_required: true` DMs promptly.",
            "- Channel messages are FYI — only respond if relevant to your role.",
            "- Use `thread_id` from the original message when replying in threads.",
            "- If stuck, message the relevant teammate. Don't work in silence.",
            "- Use `notify_user` for the human. Use `messaging_send` for agents/channels.",
        ]
    )

    roster = await _build_roster()
    if roster:
        lines.append("")
        lines.append(roster)

    sections["teams"] = "\n".join(lines)


@hook(event="agent:ready", priority=100)
async def messaging_bind_chat(ctx: Any) -> None:
    """Bind agent.chat() callback for inbox message processing."""
    data = ctx.data if hasattr(ctx, "data") else {}
    chat_fn = data.get("chat_fn")
    if chat_fn is not None:
        st = _runtime.state()
        st.agent_chat_fn = chat_fn
        _logger.info("Bound agent_chat_fn for message processing")


@hook(event="agent:shutdown", priority=100)
async def messaging_shutdown(ctx: Any) -> None:
    """Log module stop. Background poll task is cancelled by the loader."""
    del ctx  # event payload unused
    _logger.info("Messaging module stopped")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    name="messaging_send",
    description=(
        "Send a message to another agent, user, channel, or role. "
        "Use agent://name for direct messages, channel://name for channels, "
        "role://name for role-based broadcast."
    ),
    classification="state_modifying",
    when_to_use="Send a message to a teammate, channel, or role.",
)
async def messaging_send(
    to: str,
    body: str,
    msg_type: str = "info",
    priority: str = "normal",
    thread_id: str | None = None,
    action_required: bool = False,
) -> str:
    """Send a message to an entity, channel, or role.

    ``to`` accepts a comma-separated list for multi-target dispatch.
    ``msg_type`` must be one of: info, request, task, result, alert, ack.
    ``priority`` must be one of: low, normal, high, critical.
    """
    from arcteam.types import Message, MsgType, Priority

    st = _runtime.state()
    try:
        targets = [t.strip() for t in to.split(",") if t.strip()]
        if not targets:
            return json.dumps({"error": "No recipients specified"})

        msg = Message(
            sender=st.config.entity_id,
            to=targets,
            body=body,
            msg_type=MsgType(msg_type),
            priority=Priority(priority),
            thread_id=thread_id,
            action_required=action_required,
        )
        sent = await st.svc.send(msg)
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


@tool(
    name="messaging_check_inbox",
    description=(
        "Check your inbox for unread messages across all subscribed "
        "streams (DMs, channels, role broadcasts). Returns unread count "
        "and message summaries."
    ),
    classification="state_modifying",
    when_to_use="Check for new messages from teammates or channels.",
)
async def messaging_check_inbox() -> str:
    """Poll all subscribed streams and return unread messages.

    Auto-acks consumed messages when ``auto_ack`` is configured.
    Thread context is included for reply messages so the agent sees the
    full conversation.
    """
    st = _runtime.state()
    try:
        inbox = await st.svc.poll_all(
            st.config.entity_id,
            max_per_stream=st.config.max_messages_per_poll,
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
                if m.thread_id and m.thread_id != m.id:
                    thread = await st.svc.get_thread(stream, m.thread_id)
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

        if st.config.auto_ack:
            for stream, msgs in inbox.items():
                if msgs:
                    last = msgs[-1]
                    byte_pos = await _stream_end_byte_pos(st.svc, stream)
                    await st.svc.ack(stream, st.config.entity_id, seq=last.seq, byte_pos=byte_pos)

        return json.dumps(result)
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="messaging_read_thread",
    description=(
        "Read the full conversation thread for a given thread ID. "
        "Returns all messages in chronological order."
    ),
    classification="read_only",
    when_to_use="Read all messages in a thread to understand context before replying.",
)
async def messaging_read_thread(stream: str, thread_id: str) -> str:
    """Read all messages in a thread, ordered chronologically."""
    st = _runtime.state()
    try:
        msgs = await st.svc.get_thread(stream, thread_id)
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


@tool(
    name="messaging_list_entities",
    description=(
        "List all registered entities (agents and users) in the team. "
        "Shows their roles and capabilities for discovery."
    ),
    classification="read_only",
    when_to_use="Discover teammates, their roles, and capabilities.",
)
async def messaging_list_entities() -> str:
    """Return all registered team entities as JSON."""
    st = _runtime.state()
    try:
        entities = await st.registry.list_entities()
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


@tool(
    name="messaging_list_channels",
    description=(
        "List all available messaging channels, their descriptions, and current members."
    ),
    classification="read_only",
    when_to_use="Discover available channels before broadcasting to one.",
)
async def messaging_list_channels() -> str:
    """Return all available channels as JSON."""
    st = _runtime.state()
    try:
        channels = await st.svc.list_channels()
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


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


@background_task(
    name="messaging_poll_loop",
    interval=_POLL_TICK,
)
async def messaging_poll_loop(_ctx: Any) -> None:
    """Background inbox poller — routes new messages through agent.chat().

    Ticks every ``_POLL_TICK`` second. The actual inter-poll delay is
    governed by ``config.poll_interval_seconds``; the task sleeps for the
    remainder of that interval after each cycle so the scheduler overhead
    stays constant.

    Falls back to unread-count caching when ``agent_chat_fn`` is not yet
    bound (i.e. before ``agent:ready`` fires).
    """
    # Give services one second to initialise before the first poll.
    await asyncio.sleep(1.0)

    while True:
        try:
            st = _runtime.state()
            inbox = await st.svc.poll_all(
                st.config.entity_id,
                max_per_stream=st.config.max_messages_per_poll,
            )
            st.last_unread = {stream: len(msgs) for stream, msgs in inbox.items()}

            if st.agent_chat_fn is not None:
                await _process_inbox(inbox)

        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Messaging poll error")

        try:
            st = _runtime.state()
            interval = st.config.poll_interval_seconds
        except RuntimeError:
            interval = 5.0
        await asyncio.sleep(interval)


__all__ = [
    "inject_messaging_sections",
    "messaging_bind_chat",
    "messaging_check_inbox",
    "messaging_list_channels",
    "messaging_list_entities",
    "messaging_poll_loop",
    "messaging_read_thread",
    "messaging_send",
    "messaging_shutdown",
]
