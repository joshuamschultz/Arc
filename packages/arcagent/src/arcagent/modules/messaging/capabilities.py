"""Decorator-form messaging module — SPEC-021.

The live messaging surface. Capabilities register on load:

  * ``agent:assemble_prompt`` (priority 50)  — inject team context + roster.
  * ``agent:ready``           (priority 100) — bind agent.run_collected() callback.
  * ``agent:shutdown``        (priority 100) — cancel poll task, log stop.
  * ``notify_user``           (@tool)        — proactive message to the human (gateway channel).
  * ``messaging_send``        (@tool)        — send a message to entity/channel/role.
  * ``messaging_check_inbox`` (@tool)        — poll all streams for unread messages.
  * ``messaging_read_thread`` (@tool)        — read full conversation thread.
  * ``messaging_list_entities`` (@tool)      — list registered team entities.
  * ``messaging_list_channels`` (@tool)      — list available channels.
  * ``store_team_file``       (@tool)        — share a file in the team directory.
  * ``list_team_files``       (@tool)        — list files in the team directory.
  * ``messaging_inbox_loop``  (@background_task) — durable PUSH inbox consumer.

State is shared via :mod:`arcagent.modules.messaging._runtime`; the agent
configures it once at startup and the capabilities read it lazily. The file
tools read the resolved ``team_root`` from that state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from arctrust.session_identity import build_session_key

from arcagent.core import known_channels, turn_context
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.tools import _stream_end_byte_pos
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.messaging.capabilities")

# Required positive interval for the @background_task registration. The loop is
# spawned once and blocks on the durable-consumer subscription, so this value is
# a scheduler formality rather than a poll cadence (delivery is push-driven).
_POLL_TICK = 1.0

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Mid-task delivery (REQ-040/041) + push consume (REQ-021)
# ---------------------------------------------------------------------------


def _inbox_session(sender_did: str, identity: Any) -> str:
    """Session identity for a teammate message, from its one owner (REQ-312).

    A teammate gets a session per (this agent, that sender) — the same key the
    human surface is handed for a person, derived by the same owner
    (COMP-007). A constant here would be a second identity for the
    conversation: every teammate on the team would land in one shared session,
    so alice's conversation became bob's context, and it would skip the
    rotation the owner folds in.
    """
    return build_session_key(identity.did if identity is not None else "", sender_did)


def _should_activate(msg: Any, identity: Any) -> bool:
    """Whether *this* agent's run should wake for ``msg`` at all (SPEC-055).

    Every channel member currently spins a full LLM run on every pushed
    message, even one that @mentions someone else -- Anthropic's ~15x
    multi-agent token fan-out anti-pattern. Critical traffic and messages
    with no mentions (DM/broadcast, where this agent is the only or an
    unaddressed recipient) always wake the run; a channel message that
    names other agents wakes only the ones it names.
    """
    if str(msg.priority) == "critical":
        return True
    if not msg.mentions:
        return True
    return identity is not None and identity.did in list(msg.mentions)


def _is_channel_broadcast(msg: Any) -> bool:
    """Whether ``msg`` is an un-addressed post to a shared channel (SPEC-055).

    A channel target with no @mentions and non-critical priority: the case where
    every member currently wakes a full run. @mentions and critical bypass the
    relevance gate (they are explicitly for this agent / are kill-switch traffic).
    """
    if str(msg.priority) == "critical" or msg.mentions:
        return False
    return any(str(t).startswith("channel://") for t in (msg.to or []))


async def _passes_channel_triage(msg: Any, st: Any) -> bool:
    """Cheap per-agent relevance decision for a channel broadcast.

    One bounded yes/no LLM call via the bound ``classify_fn``: should THIS agent,
    given its role, answer this channel message? Returns True to wake the full
    run. Fail-open — a missing classifier or any error wakes the run, so a
    relevant message is never silently dropped (only the token-saving is lost).
    """
    if not st.config.channel_triage or st.classify_fn is None:
        return True
    entity_name = sanitize_text(st.config.entity_name or st.agent_name, max_length=200)
    system = (
        f"You are {entity_name}, one member of a team. A message was posted to a "
        "shared team channel (not addressed to anyone specific). Decide whether "
        "answering it falls within YOUR role and responsibilities. Reply with "
        "exactly one word: YES or NO."
    )
    user = sanitize_text(str(msg.body), max_length=2000)
    try:
        verdict: str = await st.classify_fn(system=system, user=user)
    except Exception:  # reason: fail-open — a triage failure must not drop a message
        _logger.warning("channel triage failed; waking run (fail-open)", exc_info=True)
        return True
    cleaned = verdict.strip().upper()
    # Skip only on an explicit NO; an empty/garbled verdict is not a decision, so
    # fail-open and wake (a relevant message is never dropped over a bad reply).
    return not cleaned.startswith("N")


def _interrupt_for(msg: Any, identity: Any) -> bool:
    """Whether ``msg`` asks for mid-turn steering (REQ-041).

    Critical priority always does; an ``action_required`` message only when it
    @mentions this agent. Everything else queues as a follow_up at turn end.
    This is the sender's request, read off the message itself, not a decision:
    ``deliver_fn`` still refuses it unless the policy pipeline permits a steer.
    """
    if str(msg.priority) == "critical":
        return True
    if msg.action_required and identity is not None and identity.did in list(msg.mentions):
        return True
    return False


def _origin_reply_target(msg: Any) -> tuple[str | None, str | None]:
    """The arcteam channel a reply to ``msg`` should return to, or (None, None).

    A message posted to a channel (e.g. an operator's group post from the arcui
    dashboard) is answered IN that channel, so the reply lands where the human
    wrote it rather than on whatever gateway platform the agent was last reached
    on. A direct message threads no reply target: ``notify_user`` then reaches the
    human on their own known channel, never the teammate who sent the DM.
    """
    channels = [str(t) for t in (msg.to or []) if str(t).startswith("channel://")]
    if channels:
        name = channels[0][len("channel://") :]
        return channels[0], f"Channel — {name}"
    return None, None


def _format_delivery(msg: Any) -> str:
    """Render one incoming message as a sanitised delivery prompt (LLM01)."""
    sender = sanitize_text(str(msg.sender), max_length=200)
    body = sanitize_text(str(msg.body), max_length=4000)
    msg_type = sanitize_text(str(msg.msg_type), max_length=50)
    priority = sanitize_text(str(msg.priority), max_length=50)
    flag = " [ACTION REQUIRED]" if msg.action_required else ""
    lines = [
        f"Message from {sender} ({msg_type}, {priority} priority){flag}:",
        f"> {body}",
        "Reply with messaging_send if a response is warranted.",
    ]
    return "\n".join(lines)


async def _handle_incoming(message: Any) -> None:
    """Deliver one bus-pushed message into the agent's run via the delivery gate.

    ``MessagingService.subscribe`` has already Ed25519-verified + replay-checked
    the message and will ack it once this returns (REQ-021/030). A teammate
    message travels the same path as a message from a human surface (REQ-312):
    whether it joins the live turn, steers it or opens a new one is decided by
    ``deliver_fn`` (arcagent core). A critical teammate message may *request* a
    steer, which the policy pipeline still has to permit. Before ``agent:ready``
    binds ``deliver_fn`` the message routes through ``agent_run_fn`` so nothing
    is dropped in the startup window.
    """
    st = _runtime.state()
    if not _should_activate(message, st.identity):
        # Ack-and-ignore (SPEC-055): a channel message that names OTHER agents
        # skips the run entirely -- avoids the ~15x-token fan-out of waking every
        # member. The channel stream itself remains the record; nothing to retry
        # or steer, so no follow_up.
        return
    if _is_channel_broadcast(message) and not await _passes_channel_triage(message, st):
        # An un-addressed channel post this agent's role does not concern: the
        # cheap triage said no, so skip the full run (the expensive fan-out this
        # gate exists to prevent). Still ack-and-ignore -- nothing to steer.
        return
    async with st.processing_lock:
        caller_did = message.signer_did or message.sender
        session_key = _inbox_session(caller_did, st.identity)
        reply_target, reply_label = _origin_reply_target(message)
        if st.deliver_fn is not None:
            try:
                await st.deliver_fn(
                    caller_did=caller_did,
                    message=_format_delivery(message),
                    session_key=session_key,
                    interrupt=_interrupt_for(message, st.identity),
                    reply_target=reply_target,
                    reply_label=reply_label,
                )
            except asyncio.QueueFull as exc:
                from arcteam.messenger import RetryableDeliveryError

                # The agent's steering queue is full: defer redelivery (do not
                # let subscribe ack this) rather than silently dropping a teammate.
                raise RetryableDeliveryError(message.id) from exc
        elif st.agent_run_fn is not None:
            await st.agent_run_fn(
                _format_delivery(message),
                session_key=session_key,
                reply_target=reply_target,
                reply_label=reply_label,
            )


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
async def messaging_bind_run_fn(ctx: Any) -> None:
    """Bind the agent's run + steering callbacks for inbox delivery.

    ``run_fn`` starts an idle-agent run; ``deliver_fn`` injects a teammate
    message into the agent's current run under policy control (REQ-040/041).
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    st = _runtime.state()
    run_fn = data.get("run_fn")
    if run_fn is not None:
        st.agent_run_fn = run_fn
    deliver_fn = data.get("deliver_fn")
    if deliver_fn is not None:
        st.deliver_fn = deliver_fn
    st.classify_fn = data.get("classify_fn")
    st.channel_deliver_fn = data.get("channel_deliver_fn")
    _logger.info("Bound agent run/deliver callbacks for message processing")


@hook(event="agent:shutdown", priority=100)
async def messaging_shutdown(ctx: Any) -> None:
    """Log module stop. Background poll task is cancelled by the loader."""
    del ctx  # event payload unused
    _logger.info("Messaging module stopped")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _notify_target(st: Any) -> str | None:
    """Where a proactive user notification should go.

    Prefers the channel the current turn arrived on (reply in place); else the
    agent's most-recently-seen channel. None when the agent has never been
    reached on any channel (nothing to notify on).
    """
    current = turn_context.inbound_channel()
    if current:
        return current
    known = known_channels.list_channels(st.workspace)
    return known[0]["target"] if known else None


async def _send_to_team(st: Any, target: str, message: str) -> None:
    """Post a human-facing notification back onto the arcteam bus.

    Used when the current turn arrived from an arcteam channel (``channel://…``):
    the reply returns to that channel — where the operator is watching in the
    dashboard — instead of a gateway platform. Classification is stamped from the
    sender's clearance, same as :func:`messaging_send`.
    """
    from arcteam.types import Message

    sender_floor = st.identity.clearance.name if st.identity is not None else "UNCLASSIFIED"
    await st.svc.send(
        Message(
            sender=st.config.entity_id,
            to=[target],
            body=message,
            classification=sender_floor,
        )
    )


@tool(
    name="notify_user",
    description=(
        "Send a proactive message to the human operator on their channel. Use "
        "ONLY when you have a meaningful update, result, question, or need "
        "direction — never for routine status. For agents/channels use "
        "messaging_send instead."
    ),
    classification="state_modifying",
    # SPEC-038 REQ-030 — notifying the human is an external_comms leg producer.
    capability_tags=["network_egress"],
)
async def notify_user(message: str = "") -> str:
    """Deliver a proactive notification to the human via the gateway channel.

    Channel-agnostic: routes through the embedded gateway's channel delivery
    (the same seam scheduled deliveries use), so it works on whatever platform
    the operator reached the agent on — no per-platform bot in the agent.
    """
    if not message.strip():
        return json.dumps({"error": "message is required"})
    st = _runtime.state()
    target = _notify_target(st)
    if not target:
        return json.dumps({"error": "no known channel to notify the user on"})
    try:
        if turn_context.is_team_target(target):
            # The turn came from an arcteam channel — answer in that channel so the
            # reply lands where the operator posted, not on a gateway platform.
            await _send_to_team(st, target, message)
        elif st.channel_deliver_fn is not None:
            await st.channel_deliver_fn(target, message)
        else:
            return json.dumps({"error": "no delivery channel is wired (standalone agent)"})
    except Exception as exc:  # reason: surface a tool error, don't crash the turn
        _logger.warning("notify_user delivery to %s failed: %s", target, exc)
        return json.dumps({"error": f"delivery failed: {exc}"})
    _logger.info("Agent notified user on %s (%d chars)", target, len(message))
    return json.dumps({"status": "sent", "target": target})


@tool(
    name="messaging_send",
    description=(
        "Send a message to another agent, user, channel, or role. "
        "Use agent://name for direct messages, channel://name for channels, "
        "role://name for role-based broadcast."
    ),
    classification="state_modifying",
    # SPEC-038 REQ-030 — an outbound comms tool is an external_comms leg
    # producer, so the SPEC-035 lethal-trifecta gate fires on a real
    # read-private -> comms sequence.
    capability_tags=["network_egress"],
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

        # SPEC-038 F4/REQ-024 — stamp the message classification at the tool
        # boundary from the SENDER's clearance (bound to identity, not a
        # free-form sender claim). No-read-up guarantees nothing the sender read
        # this session exceeds its clearance, so the clearance is the honest
        # floor; the messenger's no-write-down gate then refuses any recipient
        # who cannot receive it. Default UNCLASSIFIED clearance = no change.
        sender_floor = st.identity.clearance.name if st.identity is not None else "UNCLASSIFIED"
        msg = Message(
            sender=st.config.entity_id,
            to=targets,
            body=body,
            msg_type=MsgType(msg_type),
            priority=Priority(priority),
            thread_id=thread_id,
            action_required=action_required,
            classification=sender_floor,
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
    # SPEC-038 REQ-030 — inbound messages are untrusted content (peer/user
    # authored); tag the untrusted_input trifecta leg.
    capability_tags=["extract"],
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
    # SPEC-038 REQ-030 — thread content is peer-authored untrusted input.
    capability_tags=["extract"],
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


@tool(
    name="store_team_file",
    description=(
        "Store a file in the team's shared directory so other agents "
        "can access it. Use the file path from a received attachment."
    ),
    classification="state_modifying",
    when_to_use="Share a downloaded attachment or artifact with teammates.",
)
async def store_team_file(file_path: str) -> str:
    """Store a file in the team's shared directory."""
    from arcteam.files import TeamFileStore

    st = _runtime.state()
    entity_name = st.config.entity_name or st.config.entity_id
    try:
        store = TeamFileStore(st.team_root)
        result = await store.store(
            source_path=Path(file_path),
            agent_name=entity_name,
        )
        return json.dumps({"status": "stored", **result})
    except (FileNotFoundError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="list_team_files",
    description=("List files in the team's shared directory. Optionally filter by agent name."),
    classification="read_only",
    when_to_use="Discover files teammates have shared in the team directory.",
)
async def list_team_files(agent_name: str = "") -> str:
    """List files in the team's shared directory."""
    from arcteam.files import TeamFileStore

    st = _runtime.state()
    try:
        store = TeamFileStore(st.team_root)
        files = await store.list_files(agent_name=agent_name or None)
        return json.dumps({"files": files, "count": len(files)})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


@background_task(
    name="messaging_inbox_loop",
    interval=_POLL_TICK,
)
async def messaging_inbox_loop(_ctx: Any) -> None:
    """Background inbox loop — durable PUSH consume + policy-gated delivery.

    Joins the live NATS bus when configured (REQ-020), then subscribes over the
    entity's durable consumers (REQ-021): a running agent is delivered to live
    and a restarted one resumes from its last ack. ``MessagingService.subscribe``
    Ed25519-verifies + replay-checks each message (REQ-030) and hands valid ones
    to ``_handle_incoming``, which routes them through the steering gate
    (REQ-040/041). The loop blocks on the subscription until the task is
    cancelled by the capability loader, then stops the consumers cleanly.
    """
    # Give services one second to initialise before subscribing.
    await asyncio.sleep(1.0)

    # Upgrade to the live NATS backend once, if a url is configured.
    try:
        await _runtime.ensure_live_backend()
    except Exception:  # reason: fail-open — stay on in-memory backend
        _logger.exception("Live backend upgrade failed; staying on in-memory backend")

    st = _runtime.state()
    subscription = await st.svc.subscribe(st.config.entity_id, _handle_incoming)
    try:
        await subscription.wait()
    except asyncio.CancelledError:
        await subscription.stop()
        raise


__all__ = [
    "inject_messaging_sections",
    "list_team_files",
    "messaging_bind_run_fn",
    "messaging_check_inbox",
    "messaging_inbox_loop",
    "messaging_list_channels",
    "messaging_list_entities",
    "messaging_read_thread",
    "messaging_send",
    "messaging_shutdown",
    "notify_user",
    "store_team_file",
]
