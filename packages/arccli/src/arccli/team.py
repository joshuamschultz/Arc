"""Team messaging CLI — `arc team` commands.

Wraps the arcteam messaging subsystem as Click commands under the
unified ``arc`` CLI. All arcteam imports are lazy so the ``arc``
command still loads even if arcteam is not installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import click

from arccli.formatting import click_echo, print_json, print_table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_root(ctx: click.Context) -> Path:
    """Get root path from context, defaulting to TeamConfig."""
    root = ctx.obj.get("root")
    if root:
        return Path(root)
    from arcteam.config import TeamConfig

    return TeamConfig().root


def _validate_sender(ctx: click.Context) -> str:
    """Get and validate sender URI from context."""
    sender = ctx.obj.get("sender", "")
    if not sender:
        raise click.ClickException("--as is required (e.g., --as user://josh)")
    from arcteam.types import parse_uri

    try:
        parse_uri(sender)
    except ValueError as exc:
        raise click.ClickException(f"Invalid --as URI: {exc}") from exc
    return sender


async def _build_service(root: Path) -> tuple[Any, Any, Any, Any]:
    """Bootstrap arcteam services from a root directory."""
    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry
    from arcteam.storage import FileBackend

    backend = FileBackend(root)
    hmac_key = AuditLogger.load_hmac_key()
    audit = AuditLogger(backend, hmac_key=hmac_key)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)
    return svc, registry, audit, backend


def _print_message(msg: Any) -> None:
    """Format and print a single message with full metadata."""
    d = msg.model_dump() if hasattr(msg, "model_dump") else msg
    flag = "!" if d.get("action_required") else " "
    sender = d.get("sender", "?")
    subject = d.get("subject", "")
    body = d.get("body", "")
    seq = d.get("seq", 0)
    ts = d.get("ts", "")[:19]
    msg_type = d.get("msg_type", "info")
    priority = d.get("priority", "normal")
    msg_id = d.get("id", "")
    thread_id = d.get("thread_id", "")
    reply_to = d.get("reply_to")
    to = d.get("to", [])
    refs = d.get("refs", [])
    status = d.get("status", "")
    meta = d.get("meta", {})

    click_echo(f"  [{seq:>4}] {flag} {msg_type:<8} {priority:<8} {ts}")
    click_echo(f"         From: {sender}")
    click_echo(f"         To:   {', '.join(to) if isinstance(to, list) else to}")
    if subject:
        click_echo(f"         Subj: {subject}")
    click_echo(f"         Body: {body[:120]}")
    if len(body) > 120:
        click_echo(f"               {body[120:240]}")
    click_echo(f"         ID:   {msg_id}")
    if thread_id:
        click_echo(f"         Thread: {thread_id}")
    if reply_to:
        click_echo(f"         Reply-To: {reply_to}")
    if refs:
        click_echo(f"         Refs: {', '.join(refs)}")
    if meta:
        click_echo(f"         Meta: {meta}")
    click_echo(f"         Status: {status}")
    click_echo("")


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("team")
@click.option("--root", type=click.Path(), default=None, help="Team data root directory.")
@click.option("--as", "sender", default="", help="Your entity URI (e.g., user://josh).")
@click.option("--json", "use_json", is_flag=True, help="JSON output mode.")
@click.pass_context
def team(ctx: click.Context, root: str | None, sender: str, use_json: bool) -> None:
    """Team messaging — Slack for agents."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root
    ctx.obj["sender"] = sender
    ctx.obj["use_json"] = use_json


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


@team.command()
@click.argument("entity_id")
@click.option("--name", required=True, help="Display name.")
@click.option(
    "--type",
    "entity_type",
    required=True,
    type=click.Choice(["agent", "user"]),
    help="Entity type.",
)
@click.option("--roles", default="", help="Comma-separated roles.")
@click.pass_context
def register(
    ctx: click.Context,
    entity_id: str,
    name: str,
    entity_type: str,
    roles: str,
) -> None:
    """Register an agent or user entity."""
    from arcteam.types import Entity, EntityType

    root = _resolve_root(ctx)
    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    entity = Entity(id=entity_id, name=name, type=EntityType(entity_type), roles=role_list)

    async def _run() -> None:
        _, registry, _, _ = await _build_service(root)
        await registry.register(entity)

    asyncio.run(_run())
    click_echo(f"Registered {entity_type}: {entity_id}")


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------


@team.command()
@click.option("--role", default=None, help="Filter by role.")
@click.pass_context
def entities(ctx: click.Context, role: str | None) -> None:
    """List registered entities."""
    root = _resolve_root(ctx)
    use_json = ctx.obj.get("use_json", False)

    async def _run() -> list[Any]:
        _, registry, _, _ = await _build_service(root)
        return await registry.list_entities(role=role)

    result = asyncio.run(_run())

    if use_json:
        print_json([e.model_dump() for e in result])
    elif not result:
        click_echo("No entities registered.")
    else:
        rows = []
        for e in result:
            rows.append([e.id, e.name, e.type.value, ", ".join(e.roles), e.status])
        print_table(["ID", "Name", "Type", "Roles", "Status"], rows)


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


@team.command()
@click.option("--to", required=True, help="Target URIs (comma-separated).")
@click.option("--body", required=True, help="Message body.")
@click.option("--subject", default=None, help="Message subject.")
@click.option(
    "--type",
    "msg_type",
    default=None,
    type=click.Choice(["info", "request", "task", "result", "alert"]),
    help="Message type.",
)
@click.option(
    "--priority",
    default=None,
    type=click.Choice(["low", "normal", "high", "critical"]),
    help="Priority level.",
)
@click.option("--action", is_flag=True, help="Mark as action required.")
@click.option("--refs", default=None, help="Comma-separated references.")
@click.option("--reply-to", default=None, help="Message ID to reply to.")
@click.pass_context
def send(
    ctx: click.Context,
    to: str,
    body: str,
    subject: str | None,
    msg_type: str | None,
    priority: str | None,
    action: bool,
    refs: str | None,
    reply_to: str | None,
) -> None:
    """Send a message to agents, users, or channels."""
    from arcteam.types import Message, MsgType, Priority

    root = _resolve_root(ctx)
    sender = _validate_sender(ctx)
    targets = [t.strip() for t in to.split(",")]
    ref_list = [r.strip() for r in refs.split(",") if r.strip()] if refs else []

    msg = Message(
        sender=sender,
        to=targets,
        body=body,
        subject=subject or "",
        msg_type=MsgType(msg_type) if msg_type else MsgType.INFO,
        priority=Priority(priority) if priority else Priority.NORMAL,
        action_required=action,
        refs=ref_list,
        reply_to=reply_to,
    )

    async def _run() -> Any:
        svc, _, _, _ = await _build_service(root)
        return await svc.send(msg)

    sent = asyncio.run(_run())
    click_echo(f"Sent: {sent.id} (seq={sent.seq})")


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


@team.command()
@click.option("--limit", default=10, help="Max messages per stream.")
@click.pass_context
def inbox(ctx: click.Context, limit: int) -> None:
    """Check inbox — all subscribed streams."""
    root = _resolve_root(ctx)
    sender = _validate_sender(ctx)
    use_json = ctx.obj.get("use_json", False)

    async def _run() -> dict[str, list[Any]]:
        svc, _, _, _ = await _build_service(root)
        return await svc.poll_all(sender, max_per_stream=limit)

    result = asyncio.run(_run())

    if use_json:
        out = {s: [m.model_dump() for m in msgs] for s, msgs in result.items()}
        print_json(out)
    elif not result:
        click_echo("No new messages.")
    else:
        for stream, msgs in result.items():
            click_echo(f"\n{stream} ({len(msgs)} unread):")
            for msg in msgs:
                _print_message(msg)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@team.command()
@click.option("--channel", default=None, help="Channel name.")
@click.option("--dm", default=None, help="DM entity name.")
@click.option("--limit", default=20, help="Max messages.")
@click.pass_context
def read(ctx: click.Context, channel: str | None, dm: str | None, limit: int) -> None:
    """Read channel or DM history."""
    if not channel and not dm:
        raise click.ClickException("Specify --channel or --dm")

    root = _resolve_root(ctx)
    sender = _validate_sender(ctx)
    use_json = ctx.obj.get("use_json", False)
    stream = f"arc.channel.{channel}" if channel else f"arc.agent.{dm}"

    async def _run() -> list[Any]:
        svc, _, _, _ = await _build_service(root)
        return await svc.poll(stream, sender, max_messages=limit)

    messages = asyncio.run(_run())

    if use_json:
        print_json([m.model_dump() for m in messages])
    else:
        click_echo(f"\n{stream} ({len(messages)} messages):")
        for msg in messages:
            _print_message(msg)


# ---------------------------------------------------------------------------
# thread
# ---------------------------------------------------------------------------


@team.command()
@click.argument("thread_id")
@click.option("--stream", required=True, help="Stream name.")
@click.pass_context
def thread(ctx: click.Context, thread_id: str, stream: str) -> None:
    """View a message thread."""
    root = _resolve_root(ctx)
    use_json = ctx.obj.get("use_json", False)

    async def _run() -> list[Any]:
        svc, _, _, _ = await _build_service(root)
        return await svc.get_thread(stream, thread_id)

    messages = asyncio.run(_run())

    if use_json:
        print_json([m.model_dump() for m in messages])
    else:
        click_echo(f"\nThread {thread_id} ({len(messages)} messages):")
        for msg in messages:
            _print_message(msg)


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


@team.command()
@click.pass_context
def channels(ctx: click.Context) -> None:
    """List available channels."""
    root = _resolve_root(ctx)
    use_json = ctx.obj.get("use_json", False)

    async def _run() -> list[Any]:
        svc, _, _, _ = await _build_service(root)
        return await svc.list_channels()

    result = asyncio.run(_run())

    if use_json:
        print_json([c.model_dump() for c in result])
    elif not result:
        click_echo("No channels.")
    else:
        rows = []
        for c in result:
            members = ", ".join(c.members[:3])
            if len(c.members) > 3:
                members += f" +{len(c.members) - 3}"
            rows.append([c.name, c.description[:40], members])
        print_table(["Name", "Description", "Members"], rows)


# ---------------------------------------------------------------------------
# create-channel
# ---------------------------------------------------------------------------


@team.command("create-channel")
@click.argument("name")
@click.option("--members", default="", help="Comma-separated member URIs.")
@click.option("--description", default="", help="Channel description.")
@click.pass_context
def create_channel(ctx: click.Context, name: str, members: str, description: str) -> None:
    """Create a new channel."""
    from arcteam.types import Channel

    root = _resolve_root(ctx)
    member_list = [m.strip() for m in members.split(",") if m.strip()]
    channel = Channel(name=name, description=description, members=member_list)

    async def _run() -> None:
        svc, _, _, _ = await _build_service(root)
        await svc.create_channel(channel)

    asyncio.run(_run())
    click_echo(f"Channel created: {name}")


# ---------------------------------------------------------------------------
# join-channel
# ---------------------------------------------------------------------------


@team.command("join-channel")
@click.argument("channel_name")
@click.argument("entity_id")
@click.pass_context
def join_channel(ctx: click.Context, channel_name: str, entity_id: str) -> None:
    """Join a channel."""
    root = _resolve_root(ctx)

    async def _run() -> None:
        svc, _, _, _ = await _build_service(root)
        await svc.join_channel(channel_name, entity_id)

    asyncio.run(_run())
    click_echo(f"Joined channel: {channel_name}")


# ---------------------------------------------------------------------------
# dlq
# ---------------------------------------------------------------------------


@team.command()
@click.option("--limit", default=50, help="Max entries.")
@click.pass_context
def dlq(ctx: click.Context, limit: int) -> None:
    """List Dead Letter Queue entries."""
    root = _resolve_root(ctx)
    use_json = ctx.obj.get("use_json", False)

    async def _run() -> list[dict[str, Any]]:
        svc, _, _, _ = await _build_service(root)
        return await svc.dlq_list(limit=limit)

    entries = asyncio.run(_run())

    if use_json:
        print_json(entries)
    elif not entries:
        click_echo("DLQ is empty.")
    else:
        click_echo(f"DLQ ({len(entries)} entries):")
        for e in entries:
            reason = e.get("meta", {}).get("dlq_reason", "unknown")
            sender = e.get("sender", "?")
            click_echo(f"  [{e.get('seq', 0):>4}] {reason:<25} from {sender}")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@team.command()
@click.option("--limit", default=50, help="Max entries.")
@click.option("--verify", is_flag=True, help="Verify chain integrity.")
@click.pass_context
def audit(ctx: click.Context, limit: int, verify: bool) -> None:
    """View or verify the audit log."""
    root = _resolve_root(ctx)
    use_json = ctx.obj.get("use_json", False)

    async def _run() -> tuple[str, Any, ...]:
        _, _, audit_svc, backend = await _build_service(root)
        if verify:
            valid, last_seq = await audit_svc.verify_chain()
            return ("verify", valid, last_seq)
        records = await backend.read_stream("audit", "audit", after_seq=0, limit=limit)
        return ("list", records)

    result = asyncio.run(_run())

    if result[0] == "verify":
        _, valid, last_seq = result
        status = "VALID" if valid else "INVALID"
        click_echo(f"Audit chain: {status} (verified through seq {last_seq})")
        if not valid:
            raise SystemExit(1)
    elif use_json:
        print_json(result[1])
    elif not result[1]:
        click_echo("No audit entries.")
    else:
        records = result[1]
        click_echo(f"Audit log ({len(records)} entries):")
        for r in records:
            ts = r.get("timestamp_utc", "")[:19]
            event = r.get("event_type", "?")
            actor = r.get("actor_id", "?")
            detail = r.get("detail", "")[:60]
            click_echo(f"  [{r.get('audit_seq', 0):>4}] {ts} {event:<25} {actor:<25} {detail}")


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


@team.command()
@click.option("--max-age", default=24, help="Max cursor age in hours.")
@click.pass_context
def cleanup(ctx: click.Context, max_age: int) -> None:
    """Remove stale cursors."""
    root = _resolve_root(ctx)

    async def _run() -> int:
        svc, _, _, _ = await _build_service(root)
        return await svc.cleanup_stale_cursors(max_age_hours=max_age)

    removed = asyncio.run(_run())
    click_echo(f"Removed {removed} stale cursor(s) older than {max_age}h")
