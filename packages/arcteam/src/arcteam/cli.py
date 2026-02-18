"""CLI for ArcTeam messaging: arc-team command."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from arcteam.audit import AuditLogger
from arcteam.config import TeamConfig
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import FileBackend
from arcteam.types import Channel, Entity, EntityType, Message, MsgType, Priority, parse_uri


async def _build_service(
    root: Path,
) -> tuple[MessagingService, EntityRegistry, AuditLogger, FileBackend]:
    """Bootstrap all components from a root directory."""
    backend = FileBackend(root)
    hmac_key = AuditLogger.load_hmac_key()
    audit = AuditLogger(backend, hmac_key=hmac_key)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)
    return svc, registry, audit, backend


def _validate_sender(sender: str) -> str:
    """Validate sender URI format. Raises SystemExit on invalid URI."""
    if not sender:
        print("Error: --as is required (e.g., --as agent://a1)", file=sys.stderr)
        sys.exit(1)
    try:
        parse_uri(sender)
    except ValueError as e:
        print(f"Error: invalid --as URI: {e}", file=sys.stderr)
        sys.exit(1)
    return sender


def _print_json(data: Any) -> None:
    """Print data as formatted JSON."""
    print(json.dumps(data, indent=2, default=str))


def _format_message(msg: dict[str, Any]) -> str:
    """Format a message for human-readable display."""
    flag = "!" if msg.get("action_required") else " "
    sender = msg.get("sender", "?")
    body = msg.get("body", "")[:80]
    seq = msg.get("seq", 0)
    ts = msg.get("ts", "")[:19]
    return f"  [{seq:>4}] {flag} {ts} {sender:<25} {body}"


def _format_entity(entity: dict[str, Any]) -> str:
    """Format an entity for display."""
    eid = entity.get("id", "?")
    name = entity.get("name", "?")
    etype = entity.get("type", "?")
    roles = ", ".join(entity.get("roles", []))
    status = entity.get("status", "?")
    return f"  {eid:<30} {name:<20} {etype:<6} [{roles}] ({status})"


async def cmd_register(args: argparse.Namespace) -> None:
    _, registry, _, _ = await _build_service(args.root)
    entity_type = EntityType(args.type)
    roles = [r.strip() for r in args.roles.split(",")] if args.roles else []
    entity = Entity(
        id=args.entity_id,
        name=args.name,
        type=entity_type,
        roles=roles,
    )
    await registry.register(entity)
    print(f"Registered {entity_type.value}: {args.entity_id}")


async def cmd_entities(args: argparse.Namespace) -> None:
    _, registry, _, _ = await _build_service(args.root)
    entities = await registry.list_entities(role=args.role)
    if getattr(args, "json", False):
        _print_json([e.model_dump() for e in entities])
    else:
        print(f"Entities ({len(entities)}):")
        for e in entities:
            print(_format_entity(e.model_dump()))


async def cmd_channel(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    members = [m.strip() for m in args.members.split(",")] if args.members else []
    channel = Channel(
        name=args.name,
        description=args.description or "",
        members=members,
    )
    await svc.create_channel(channel)
    print(f"Channel created: {args.name}")


async def cmd_join(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    await svc.join_channel(args.channel, args.entity_id)
    print(f"Joined channel: {args.channel}")


async def cmd_channels(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    channels = await svc.list_channels()
    if getattr(args, "json", False):
        _print_json([c.model_dump() for c in channels])
    else:
        print(f"Channels ({len(channels)}):")
        for c in channels:
            members = ", ".join(c.members[:3])
            if len(c.members) > 3:
                members += f" +{len(c.members) - 3}"
            print(f"  {c.name:<25} {c.description[:40]:<40} [{members}]")


async def cmd_send(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    sender = _validate_sender(args.sender)
    targets = [t.strip() for t in args.to.split(",")]
    refs = [r.strip() for r in args.refs.split(",")] if args.refs else []
    msg = Message(
        sender=sender,
        to=targets,
        body=args.body,
        msg_type=MsgType(args.type) if args.type else MsgType.INFO,
        priority=Priority(args.priority) if args.priority else Priority.NORMAL,
        action_required=args.action or False,
        refs=refs,
        thread_id=args.thread_id,
    )
    sent = await svc.send(msg)
    print(f"Sent: {sent.id} (seq={sent.seq})")


async def cmd_inbox(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    sender = _validate_sender(args.sender)
    result = await svc.poll_all(sender, max_per_stream=args.limit)
    if getattr(args, "json", False):
        out: dict[str, list[dict[str, Any]]] = {}
        for stream, msgs in result.items():
            out[stream] = [m.model_dump() for m in msgs]
        _print_json(out)
    else:
        if not result:
            print("No new messages.")
            return
        for stream, msgs in result.items():
            print(f"\n{stream} ({len(msgs)} unread):")
            for msg in msgs:
                print(_format_message(msg.model_dump()))


async def cmd_read(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    sender = _validate_sender(args.sender)
    if args.channel:
        stream = f"arc.channel.{args.channel}"
    elif args.dm:
        stream = f"arc.agent.{args.dm}"
    else:
        print("Error: specify --channel or --dm", file=sys.stderr)
        sys.exit(1)
    messages = await svc.poll(stream, sender, max_messages=args.limit)
    if getattr(args, "json", False):
        _print_json([m.model_dump() for m in messages])
    else:
        print(f"\n{stream} ({len(messages)} messages):")
        for msg in messages:
            print(_format_message(msg.model_dump()))


async def cmd_thread(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    messages = await svc.get_thread(args.stream, args.thread_id)
    if getattr(args, "json", False):
        _print_json([m.model_dump() for m in messages])
    else:
        print(f"\nThread {args.thread_id} ({len(messages)} messages):")
        for msg in messages:
            print(_format_message(msg.model_dump()))


async def cmd_dlq(args: argparse.Namespace) -> None:
    svc, _, _, _ = await _build_service(args.root)
    entries = await svc.dlq_list(limit=args.limit)
    if getattr(args, "json", False):
        _print_json(entries)
    else:
        print(f"DLQ ({len(entries)} entries):")
        for e in entries:
            reason = e.get("meta", {}).get("dlq_reason", "unknown")
            sender = e.get("sender", "?")
            print(f"  [{e.get('seq', 0):>4}] {reason:<25} from {sender}")


async def cmd_audit(args: argparse.Namespace) -> None:
    _, _, audit, backend = await _build_service(args.root)
    if args.verify:
        valid, last_seq = await audit.verify_chain()
        status = "VALID" if valid else "INVALID"
        print(f"Audit chain: {status} (verified through seq {last_seq})")
        if not valid:
            sys.exit(1)
    else:
        records = await backend.read_stream("audit", "audit", after_seq=0, limit=args.limit)
        if getattr(args, "json", False):
            _print_json(records)
        else:
            print(f"Audit log ({len(records)} entries):")
            for r in records:
                ts = r.get("timestamp_utc", "")[:19]
                event = r.get("event_type", "?")
                actor = r.get("actor_id", "?")
                detail = r.get("detail", "")[:60]
                print(f"  [{r.get('audit_seq', 0):>4}] {ts} {event:<25} {actor:<25} {detail}")


async def cmd_cleanup(args: argparse.Namespace) -> None:
    """Remove stale cursors (FR-11)."""
    svc, _, _, _ = await _build_service(args.root)
    removed = await svc.cleanup_stale_cursors(max_age_hours=args.max_age)
    print(f"Removed {removed} stale cursor(s) older than {args.max_age}h")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="arc-team",
        description="ArcTeam messaging CLI — Slack for agents",
    )
    parser.add_argument("--root", type=Path, default=TeamConfig().root, help="Data directory")
    parser.add_argument("--as", dest="sender", default="", help="Entity URI (e.g., agent://a1)")
    parser.add_argument("--json", action="store_true", help="JSON output mode")

    sub = parser.add_subparsers(dest="command", required=True)

    # register
    p = sub.add_parser("register", help="Register an entity")
    p.add_argument("entity_id", help="Entity URI (e.g., agent://a1)")
    p.add_argument("--name", required=True, help="Display name")
    p.add_argument("--type", required=True, choices=["agent", "user"], help="Entity type")
    p.add_argument("--roles", default="", help="Comma-separated roles")

    # entities
    p = sub.add_parser("entities", help="List entities")
    p.add_argument("--role", default=None, help="Filter by role")

    # channel
    p = sub.add_parser("channel", help="Create a channel")
    p.add_argument("name", help="Channel name")
    p.add_argument("--members", default="", help="Comma-separated member URIs")
    p.add_argument("--description", default="", help="Channel description")

    # join
    p = sub.add_parser("join", help="Join a channel")
    p.add_argument("channel", help="Channel name")
    p.add_argument("entity_id", help="Entity URI")

    # channels
    sub.add_parser("channels", help="List channels")

    # send
    p = sub.add_parser("send", help="Send a message")
    p.add_argument("--to", required=True, help="Target URIs (comma-separated)")
    p.add_argument("--body", required=True, help="Message body")
    p.add_argument(
        "--type",
        default=None,
        choices=[t.value for t in MsgType],
        help="Message type",
    )
    p.add_argument(
        "--priority",
        default=None,
        choices=[p.value for p in Priority],
        help="Priority",
    )
    p.add_argument("--action", action="store_true", help="Action required")
    p.add_argument("--refs", default=None, help="Comma-separated references")
    p.add_argument("--thread-id", default=None, help="Thread ID (for replies)")

    # inbox
    p = sub.add_parser("inbox", help="Check inbox (all subscribed streams)")
    p.add_argument("--limit", type=int, default=10, help="Max messages per stream")

    # read
    p = sub.add_parser("read", help="Read channel or DM history")
    p.add_argument("--channel", default=None, help="Channel name")
    p.add_argument("--dm", default=None, help="DM entity name")
    p.add_argument("--limit", type=int, default=20, help="Max messages")

    # thread
    p = sub.add_parser("thread", help="View message thread")
    p.add_argument("thread_id", help="Thread ID")
    p.add_argument("--stream", required=True, help="Stream name")

    # dlq
    p = sub.add_parser("dlq", help="List Dead Letter Queue")
    p.add_argument("--limit", type=int, default=50, help="Max entries")

    # audit
    p = sub.add_parser("audit", help="View audit log")
    p.add_argument("--limit", type=int, default=50, help="Max entries")
    p.add_argument("--verify", action="store_true", help="Verify chain integrity")

    # cleanup (FR-11)
    p = sub.add_parser("cleanup", help="Remove stale cursors")
    p.add_argument("--max-age", type=int, default=24, help="Max cursor age in hours")

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for arc-team CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "register": cmd_register,
        "entities": cmd_entities,
        "channel": cmd_channel,
        "join": cmd_join,
        "channels": cmd_channels,
        "send": cmd_send,
        "inbox": cmd_inbox,
        "read": cmd_read,
        "thread": cmd_thread,
        "dlq": cmd_dlq,
        "audit": cmd_audit,
        "cleanup": cmd_cleanup,
    }

    handler = commands.get(args.command)
    if handler:
        asyncio.run(handler(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
