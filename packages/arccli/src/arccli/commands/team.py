"""Plain CommandDef handlers for the `arc team` subcommand group.

T1.1.5 migration: replaces the legacy Click-based dispatch in registry.py.
Each function is a direct translation of the corresponding Click command body
in arccli.team, with Click-specific calls replaced with stdlib equivalents.

Layer contract: this module may import from arcteam.
It MUST NOT import click or arccli.main_legacy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(msg: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(msg + "\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a table with headers."""
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


def _print_kv(pairs: list[tuple[str, str]]) -> None:
    """Print key-value pairs in aligned format."""
    try:
        from arccli.formatting import print_kv

        print_kv(pairs)
    except ImportError:
        width = max(len(k) for k, _ in pairs) if pairs else 0
        for k, v in pairs:
            sys.stdout.write(f"  {k:<{width}}  {v}\n")


def _print_json(data: Any) -> None:
    """Print data as indented JSON."""
    import json

    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _get_root(args: argparse.Namespace) -> Path:
    """Resolve team data root from args or TeamConfig default."""
    root: str | None = getattr(args, "root", None)
    if root:
        return Path(root)
    from arcteam.config import TeamConfig

    return TeamConfig().root


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

    _write(f"  [{seq:>4}] {flag} {msg_type:<8} {priority:<8} {ts}")
    _write(f"         From: {sender}")
    _write(f"         To:   {', '.join(to) if isinstance(to, list) else to}")
    if subject:
        _write(f"         Subj: {subject}")
    _write(f"         Body: {body[:120]}")
    if len(body) > 120:
        _write(f"               {body[120:240]}")
    _write(f"         ID:   {msg_id}")
    if thread_id:
        _write(f"         Thread: {thread_id}")
    if reply_to:
        _write(f"         Reply-To: {reply_to}")
    if refs:
        _write(f"         Refs: {', '.join(refs)}")
    if meta:
        _write(f"         Meta: {meta}")
    _write(f"         Status: {status}")
    _write()


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _status(args: argparse.Namespace) -> None:
    """Show team overview — entity count, channels, messages, audit chain.

    Paths must mirror the on-disk layout written by ``arcteam`` collections:
    ``messages/registry`` (entities), ``messages/channels`` (channels),
    ``messages/streams`` (message streams as ``.log`` files), and
    ``audit/audit`` (audit chain as ``.log`` files).
    """
    root = _get_root(args)
    use_json: bool = getattr(args, "use_json", False)

    entities_dir = root / "messages" / "registry"
    channels_dir = root / "messages" / "channels"

    entity_count = len(list(entities_dir.glob("*.json"))) if entities_dir.is_dir() else 0
    channel_count = len(list(channels_dir.glob("*.json"))) if channels_dir.is_dir() else 0

    message_count = 0
    streams_dir = root / "messages" / "streams"
    if streams_dir.is_dir():
        for stream_file in streams_dir.rglob("*.log"):
            with open(stream_file, encoding="utf-8") as f:
                message_count += sum(1 for _ in f)

    audit_dir = root / "audit" / "audit"
    audit_count = 0
    if audit_dir.is_dir():
        for audit_file in audit_dir.glob("*.log"):
            with open(audit_file, encoding="utf-8") as f:
                audit_count += sum(1 for _ in f)

    hmac_exists = (root / ".hmac_key").exists()

    data = {
        "root": str(root),
        "entities": entity_count,
        "channels": channel_count,
        "messages": message_count,
        "audit_entries": audit_count,
        "hmac_key": hmac_exists,
    }

    if use_json:
        _print_json(data)
    else:
        _write("Team Status:")
        _print_kv(
            [
                ("Root", str(root)),
                ("Entities", str(entity_count)),
                ("Channels", str(channel_count)),
                ("Messages", str(message_count)),
                ("Audit entries", str(audit_count)),
                ("HMAC key", "present" if hmac_exists else "MISSING"),
            ]
        )


def _config_cmd(args: argparse.Namespace) -> None:
    """Show team configuration."""
    from arcteam.config import TeamConfig

    as_json: bool = getattr(args, "as_json", False)
    cfg = TeamConfig()
    data = cfg.model_dump()
    data["root"] = str(data["root"])

    if as_json:
        _print_json(data)
    else:
        _write("Team Configuration:")
        for key, val in data.items():
            _write(f"  {key} = {val}")


def _init_cmd(args: argparse.Namespace) -> None:
    """Initialize team data directory."""
    import secrets

    from arcteam.config import TeamConfig

    root_path: str | None = getattr(args, "root_path", None)
    root = Path(root_path) if root_path else TeamConfig().root

    dirs = [
        root,
        root / "messages" / "registry",
        root / "messages" / "channels",
        root / "messages" / "cursors",
        root / "messages" / "streams",
        root / "audit" / "audit",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    hmac_path = root / ".hmac_key"
    if not hmac_path.exists():
        hmac_path.write_bytes(secrets.token_bytes(32))
        hmac_path.chmod(0o600)
        _write(f"Generated HMAC key: {hmac_path}")
    else:
        _write(f"HMAC key already exists: {hmac_path}")

    _write(f"Team initialized at: {root}")
    for d in dirs:
        if d == root:
            continue
        _write(f"  {d.relative_to(root)}/")


def _register(args: argparse.Namespace) -> None:
    """Register an agent or user entity."""
    from arcteam.types import Entity, EntityType

    root = _get_root(args)
    entity_id: str = args.entity_id
    name: str = args.name
    entity_type: str = args.entity_type
    roles_str: str = getattr(args, "roles", "")
    role_list = [r.strip() for r in roles_str.split(",") if r.strip()]
    entity = Entity(id=entity_id, name=name, type=EntityType(entity_type), roles=role_list)

    async def _run() -> None:
        _, registry, _, _ = await _build_service(root)
        await registry.register(entity)

    asyncio.run(_run())
    _write(f"Registered {entity_type}: {entity_id}")


def _entities(args: argparse.Namespace) -> None:
    """List registered entities."""
    root = _get_root(args)
    role_filter: str | None = getattr(args, "role", None)
    use_json: bool = getattr(args, "use_json", False)

    async def _run() -> list[Any]:
        _, registry, _, _ = await _build_service(root)
        entities: list[Any] = await registry.list_entities(role=role_filter)
        return entities

    result: list[Any] = asyncio.run(_run())

    if use_json:
        _print_json([e.model_dump() for e in result])
    elif not result:
        _write("No entities registered.")
    else:
        rows = []
        for e in result:
            rows.append([e.id, e.name, e.type.value, ", ".join(e.roles), e.status])
        _print_table(["ID", "Name", "Type", "Roles", "Status"], rows)


def _channels(args: argparse.Namespace) -> None:
    """List available channels."""
    root = _get_root(args)
    use_json: bool = getattr(args, "use_json", False)

    async def _run() -> list[Any]:
        svc, _, _, _ = await _build_service(root)
        channels: list[Any] = await svc.list_channels()
        return channels

    result: list[Any] = asyncio.run(_run())

    if use_json:
        _print_json([c.model_dump() for c in result])
    elif not result:
        _write("No channels.")
    else:
        rows = []
        for c in result:
            members = ", ".join(c.members[:3])
            if len(c.members) > 3:
                members += f" +{len(c.members) - 3}"
            rows.append([c.name, c.description[:40], members])
        _print_table(["Name", "Description", "Members"], rows)


def _memory_status(args: argparse.Namespace) -> None:
    """Show entity count, index health, and memory config."""
    root = _get_root(args)
    use_json: bool = getattr(args, "use_json", False)

    from arcteam.memory.config import TeamMemoryConfig
    from arcteam.memory.index_manager import IndexManager
    from arcteam.memory.storage import MemoryStorage

    async def _run() -> dict[str, Any]:
        config = TeamMemoryConfig(root=root)
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        index = await index_mgr.get_index()
        dirty = index_mgr._is_dirty()
        files = await storage.list_entity_files()
        return {
            "enabled": config.enabled,
            "entity_count": len(index),
            "file_count": len(files),
            "index_dirty": dirty,
            "entities_dir": str(config.entities_dir),
            "tier": config.tier,
        }

    data = asyncio.run(_run())

    if use_json:
        _print_json(data)
    else:
        _write("Team Memory Status:")
        _print_kv(
            [
                ("Enabled", str(data["enabled"])),
                ("Entities indexed", str(data["entity_count"])),
                ("Entity files", str(data["file_count"])),
                ("Index dirty", str(data["index_dirty"])),
                ("Entities dir", data["entities_dir"]),
                ("Tier", data["tier"]),
            ]
        )


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc team <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc team",
        description="Team messaging — Slack for agents.",
        add_help=True,
    )
    # Global options shared by most subcommands
    parser.add_argument("--root", dest="root", default=None, help="Team data root directory.")
    parser.add_argument("--json", dest="use_json", action="store_true", help="JSON output mode.")

    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # status
    subs.add_parser("status", help="Show team overview.")

    # config
    p = subs.add_parser("config", help="Show team configuration.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # init
    p = subs.add_parser("init", help="Initialize team data directory.")
    p.add_argument("--root", dest="root_path", default=None, help="Team data root.")

    # register
    p = subs.add_parser("register", help="Register an agent or user entity.")
    p.add_argument("entity_id", help="Entity ID.")
    p.add_argument("--name", required=True, help="Display name.")
    p.add_argument(
        "--type", dest="entity_type", required=True, choices=["agent", "user"],
        help="Entity type."
    )
    p.add_argument("--roles", default="", help="Comma-separated roles.")

    # entities
    p = subs.add_parser("entities", help="List registered entities.")
    p.add_argument("--role", default=None, help="Filter by role.")

    # channels
    subs.add_parser("channels", help="List available channels.")

    # memory status
    subs.add_parser("memory-status", help="Show team memory status.")

    return parser


_SUBCOMMAND_MAP = {
    "status": _status,
    "config": _config_cmd,
    "init": _init_cmd,
    "register": _register,
    "entities": _entities,
    "channels": _channels,
    "memory-status": _memory_status,
}


def team_handler(args: list[str]) -> None:
    """Top-level handler for `arc team <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc team ...`.
    """
    parser = _build_parser()

    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = _SUBCOMMAND_MAP.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"arc team: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
