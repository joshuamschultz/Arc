"""Plain CommandDef handlers for the `arc team` subcommand group."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arccli.commands._shared import dispatch
from arccli.commands._shared import print_json as _print_json
from arccli.commands._shared import print_kv as _print_kv
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _write

_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
_PREFLIGHT_TIMEOUT = 0.5
_CONNECT_TIMEOUT = 3.0

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_root(args: argparse.Namespace) -> Path:
    """Resolve team data root from args or TeamConfig default."""
    root: str | None = getattr(args, "root", None)
    if root:
        return Path(root)
    from arcteam.config import TeamConfig

    return TeamConfig().root


async def _preflight(url: str) -> None:
    """Fail fast if the NATS port is not accepting connections.

    ``nats.connect`` retries internally on a refused port, so a quick TCP probe
    keeps ``arc`` responsive (and best-effort auto-registration snappy) when no
    server is running.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4222
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_PREFLIGHT_TIMEOUT
        )
    except (TimeoutError, OSError) as exc:
        raise ConnectionError(f"NATS not reachable at {url}") from exc
    writer.close()
    await writer.wait_closed()


async def _connect_backend() -> Any:
    """Connect the live NATS JetStream backend from the configured URL.

    The URL is read from ``ARCTEAM_NATS_URL`` (default: the local dev server).
    Tests replace this factory with an in-memory backend via monkeypatch, so
    no NATS server is required off the live path.
    """
    import nats
    from arcteam.backends.nats import NatsBackend

    async def _swallow(_exc: Exception) -> None:
        return None

    url = os.environ.get("ARCTEAM_NATS_URL", _DEFAULT_NATS_URL)
    await _preflight(url)
    nc = await asyncio.wait_for(
        nats.connect(url, connect_timeout=2, allow_reconnect=False, error_cb=_swallow),
        timeout=_CONNECT_TIMEOUT,
    )
    return NatsBackend(nc.jetstream(), nc)


async def _build_service(
    root: Path,
    backend: Any | None = None,
    signer: Any | None = None,
) -> tuple[Any, Any, Any, Any]:
    """Bootstrap arcteam services over a storage backend.

    Live callers pass ``backend=None`` and get a NATS-backed service; tests
    inject a :class:`~arcteam.storage.MemoryBackend`. A ``signer`` promotes the
    messenger to signed mode (REQ-030) — required on the send path.
    """
    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry

    from arccli.commands.operator import resolve_operator_signer

    if backend is None:
        backend = await _connect_backend()
    # The audit chain is signed by the OPERATOR key (audit authority), never a
    # team member's DID (SPEC-053/037) — asymmetric, non-repudiable — at the
    # config-resolved custody + algorithm (F3), not a bare Ed25519 default.
    audit = AuditLogger(backend, resolve_operator_signer())
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit, signer=signer)
    return svc, registry, audit, backend


async def _shutdown(backend: Any) -> None:
    """Drain a live backend connection; a no-op for the in-memory test backend."""
    close = getattr(backend, "close", None)
    if close is not None:
        await close()


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
    """Show team overview — entities, channels, and teams counted from the store."""
    from arcteam.team import TeamStore

    root = _get_root(args)
    use_json: bool = getattr(args, "use_json", False)

    async def _run() -> tuple[int, int, int]:
        svc, registry, audit, backend = await _build_service(root)
        try:
            entities = await registry.list_entities()
            channels = await svc.list_channels()
            teams = await TeamStore(backend, audit).list_teams()
        finally:
            await _shutdown(backend)
        return len(entities), len(channels), len(teams)

    from arccli.commands.operator import operator_key_path

    entity_count, channel_count, team_count = asyncio.run(_run())
    operator_key_exists = operator_key_path(Path("~/.arc")).exists()

    data = {
        "root": str(root),
        "entities": entity_count,
        "channels": channel_count,
        "teams": team_count,
        "operator_key": operator_key_exists,
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
                ("Teams", str(team_count)),
                ("Operator key", "present" if operator_key_exists else "MISSING"),
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
    from arcteam.config import TeamConfig

    from arccli.commands.operator import load_operator_key, operator_key_path

    root_path: str | None = getattr(args, "root_path", None)
    root = Path(root_path) if root_path else TeamConfig().root

    # The message/audit store is NATS JetStream — init only needs the root and
    # the OPERATOR key that signs the audit chain asymmetrically (SPEC-037).
    root.mkdir(parents=True, exist_ok=True)

    load_operator_key()  # bootstrap the audit authority (idempotent)
    _write(f"Operator audit key: {operator_key_path(Path('~/.arc'))}")
    _write(f"Team initialized at: {root}")


def _backfill_workspaces(args: argparse.Namespace) -> None:
    """Scan team/*/arcagent.toml and update entities with `workspace_path`.

    SPEC-019 T1.4 / FR-3. Idempotent. Dry-run is the default; `--apply`
    persists changes. Composes two single-purpose helpers: TOML
    discovery (pure I/O, no registry contact) and registry application
    (the async update loop). Wave 2 simplification — the previous
    monolith mixed both jobs in one ~55-line function.
    """
    root = _get_root(args)
    apply = bool(getattr(args, "apply", False))
    team_dir = Path(getattr(args, "team_dir", "team")).resolve()

    matches = _discover_agent_tomls(team_dir)
    if not matches:
        return

    asyncio.run(_apply_workspace_updates(root, matches, apply))


def _discover_agent_tomls(team_dir: Path) -> list[tuple[str, Path]]:
    """Read every team/*/arcagent.toml; return (agent_name, abs_workspace).

    Skips missing or malformed TOML and missing `[agent].name` — emits
    one warning line per skip. An empty result writes a "no toml found"
    notice and returns []. No registry contact happens here.
    """
    import tomllib

    if not team_dir.exists() or not team_dir.is_dir():
        _write(f"  team-dir not found: {team_dir}")
        return []

    matches: list[tuple[str, Path]] = []
    for toml_path in team_dir.glob("*/arcagent.toml"):
        try:
            cfg = tomllib.loads(toml_path.read_text())
        except (tomllib.TOMLDecodeError, OSError) as exc:
            _write(f"  skip {toml_path}: {exc}")
            continue
        agent_section = cfg.get("agent", {})
        agent_name = agent_section.get("name")
        if not agent_name:
            _write(f"  skip {toml_path}: no [agent].name")
            continue
        workspace_raw = agent_section.get("workspace", "./workspace")
        matches.append((agent_name, (toml_path.parent / workspace_raw).resolve()))

    if not matches:
        _write("  no arcagent.toml files found in team-dir")
    return matches


async def _apply_workspace_updates(
    root: Path, matches: list[tuple[str, Path]], apply: bool
) -> None:
    """Update each entity's `workspace_path` field; idempotent."""
    _, registry, _, backend = await _build_service(root)
    try:
        for entity_id, workspace in matches:
            entity = await registry.get(entity_id)
            if entity is None:
                _write(f"  skip {entity_id}: not in registry")
                continue
            if entity.workspace_path == str(workspace):
                _write(f"  unchanged {entity_id}")
                continue
            if apply:
                entity.workspace_path = str(workspace)
                await registry.update(entity)
                _write(f"  updated {entity_id} -> {workspace}")
            else:
                _write(f"  would update {entity_id} -> {workspace}")
    finally:
        await _shutdown(backend)


def _resolve_workspace(raw: str | None) -> str | None:
    """Resolve `--workspace` value to an absolute path.

    SPEC-019 SR-6: reject `~` and env-var shorthand in stored form to prevent
    late-binding ambiguity. Resolves explicit paths against `Path.cwd()` and
    requires the path to exist as a directory.

    Returns None when raw is None AND the entity is not an agent (caller decides).
    Otherwise returns absolute path string. Raises ValueError on validation
    failure.
    """
    candidate = raw if raw is not None else str(Path.cwd())

    # SR-6: no late-binding shorthand persisted
    if "~" in candidate or "$" in candidate:
        msg = (
            f"workspace must be an absolute or relative path, "
            f"not shorthand like ~ or $VAR: {candidate!r}"
        )
        raise ValueError(msg)

    resolved = Path(candidate).resolve()
    if not resolved.exists():
        raise ValueError(f"workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"workspace is not a directory: {resolved}")
    return str(resolved)


def _registration_identity(entity_type: str, workspace_path: str | None, root: Path) -> Any:
    """Resolve the persisted identity whose key the entity signs with.

    An agent's identity lives in its own ``arcagent.toml`` (next to its
    workspace) — the same file the running agent loads at startup — so the
    registered DID and verify key match what it signs with (REQ-030). When
    there is no such config (a user, or a bare workspace) a fresh identity is
    minted and persisted under the team root, so the same key can sign later.
    """
    from arctrust import AgentIdentity

    if entity_type == "agent" and workspace_path:
        config_path = Path(workspace_path).parent / "arcagent.toml"
        if config_path.exists():
            from arcagent.core.config import load_config

            config = load_config(config_path)
            return AgentIdentity.from_config(
                config.identity,
                org=config.agent.org,
                agent_type=config.agent.type,
                config_path=config_path,
            )

    identity = AgentIdentity.generate(org="local", agent_type=entity_type)
    identity.save_keys(root / "keys")
    return identity


def _register(args: argparse.Namespace) -> None:
    """Register an agent or user entity on DID-keyed identity."""
    from arcteam.types import Entity, EntityType

    root = _get_root(args)
    entity_id: str = args.entity_id
    name: str = args.name
    entity_type: str = args.entity_type
    roles_str: str = getattr(args, "roles", "")
    role_list = [r.strip() for r in roles_str.split(",") if r.strip()]

    # Workspace path applies to agents only; users have no workspace concept.
    workspace_path: str | None = None
    if entity_type == "agent":
        raw_workspace: str | None = getattr(args, "workspace", None)
        try:
            workspace_path = _resolve_workspace(raw_workspace)
        except ValueError as exc:
            sys.stderr.write(f"arc team register: {exc}\n")
            sys.exit(2)

    handle = entity_id.split("://")[-1]
    uri = entity_id if "://" in entity_id else f"{entity_type}://{handle}"
    identity = _registration_identity(entity_type, workspace_path, root)
    entity = Entity(
        did=identity.did,
        handle=handle,
        id=uri,
        name=name,
        type=EntityType(entity_type),
        public_key=identity.public_key.hex(),
        roles=role_list,
        workspace_path=workspace_path,
    )

    async def _run() -> None:
        _, registry, _, backend = await _build_service(root)
        try:
            await registry.register(entity)
        finally:
            await _shutdown(backend)

    asyncio.run(_run())
    _write(f"Registered {entity_type}: {entity_id}")
    if workspace_path:
        _write(f"  Workspace: {workspace_path}")


def _entities(args: argparse.Namespace) -> None:
    """List registered entities."""
    root = _get_root(args)
    role_filter: str | None = getattr(args, "role", None)
    use_json: bool = getattr(args, "use_json", False)

    async def _run() -> list[Any]:
        _, registry, _, backend = await _build_service(root)
        try:
            entities: list[Any] = await registry.list_entities(role=role_filter)
        finally:
            await _shutdown(backend)
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
        svc, _, _, backend = await _build_service(root)
        try:
            channels: list[Any] = await svc.list_channels()
        finally:
            await _shutdown(backend)
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
# Team lifecycle verbs (C4) — create / add-member / remove-member
# ---------------------------------------------------------------------------


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated argument into trimmed, non-empty tokens."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _create_team(args: argparse.Namespace) -> None:
    """Create a team on the new ``TeamStore`` (REQ-010, REQ-012)."""
    from arcteam.registry import resolve
    from arcteam.team import Team, TeamStore

    root = _get_root(args)
    team_id: str = args.team_id
    name: str = getattr(args, "name", None) or team_id
    channel: str = args.channel
    members_raw: str = getattr(args, "members", "") or ""
    goal: str | None = getattr(args, "goal", None)

    async def _run() -> None:
        _, registry, audit, backend = await _build_service(root)
        try:
            member_dids = [await resolve(registry, ref) for ref in _split_csv(members_raw)]
            store = TeamStore(backend, audit)
            await store.create(
                Team(
                    id=team_id,
                    name=name,
                    members=member_dids,
                    default_channel=channel,
                    goal_ref=goal,
                )
            )
        finally:
            await _shutdown(backend)

    asyncio.run(_run())
    _write(f"Created team: {team_id}")


def _add_member(args: argparse.Namespace) -> None:
    """Add a member to a team (REQ-012)."""
    _mutate_member(args, add=True)


def _remove_member(args: argparse.Namespace) -> None:
    """Remove a member from a team (REQ-012)."""
    _mutate_member(args, add=False)


def _mutate_member(args: argparse.Namespace, *, add: bool) -> None:
    """Resolve a member ref to a DID and add/remove it from a team."""
    from arcteam.registry import resolve
    from arcteam.team import TeamStore

    root = _get_root(args)
    team_id: str = args.team_id
    member: str = args.member

    async def _run() -> None:
        _, registry, audit, backend = await _build_service(root)
        try:
            did = await resolve(registry, member)
            store = TeamStore(backend, audit)
            if add:
                await store.add_member(team_id, did)
            else:
                await store.remove_member(team_id, did)
        finally:
            await _shutdown(backend)

    asyncio.run(_run())
    verb, prep = ("Added", "to") if add else ("Removed", "from")
    _write(f"{verb} {member} {prep} team {team_id}")


# ---------------------------------------------------------------------------
# Messaging verbs (C4) — send / inbox / read / thread
# ---------------------------------------------------------------------------


async def _signer_for(registry: Any, sender_ref: str) -> Any:
    """Build a ``MessageSigner`` from the sender's arctrust identity (REQ-030).

    The signing key lives with the agent, not arcteam: resolve the entity,
    locate its ``arcagent.toml`` (the parent of the registered workspace), load
    the persisted identity, and hand its Ed25519 seed to the messenger so every
    outgoing envelope is signed.
    """
    from arcagent.core.config import load_config
    from arcteam.crypto import MessageSigner
    from arcteam.registry import UnknownHandle
    from arctrust import AgentIdentity

    entity = await registry.get(sender_ref)
    if entity is None:
        raise UnknownHandle(f"Unknown sender: {sender_ref}")
    if not entity.workspace_path:
        raise ValueError(f"Sender {sender_ref!r} has no workspace; cannot locate signing identity")

    config_path = Path(entity.workspace_path).parent / "arcagent.toml"
    config = load_config(config_path)
    identity = AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=config_path,
    )
    return MessageSigner.from_identity(identity)


def _send(args: argparse.Namespace) -> None:
    """Send a signed message to one or more targets (REQ-012, REQ-030)."""
    from arcteam.messenger import MessagingService
    from arcteam.types import Message, MsgType, Priority

    root = _get_root(args)
    sender: str = args.sender
    targets = _split_csv(args.to)
    refs = _split_csv(args.refs) if getattr(args, "refs", None) else []
    msg_type: str | None = getattr(args, "type", None)
    priority: str | None = getattr(args, "priority", None)

    async def _run() -> Any:
        _, registry, audit, backend = await _build_service(root)
        try:
            signer = await _signer_for(registry, sender)
            svc = MessagingService(backend, registry, audit, signer=signer)
            message = Message(
                sender=sender,
                to=targets,
                body=args.body,
                msg_type=MsgType(msg_type) if msg_type else MsgType.INFO,
                priority=Priority(priority) if priority else Priority.NORMAL,
                action_required=bool(getattr(args, "action", False)),
                refs=refs,
                thread_id=getattr(args, "thread_id", None),
            )
            return await svc.send(message)
        finally:
            await _shutdown(backend)

    sent = asyncio.run(_run())
    _write(f"Sent: {sent.id} (seq={sent.seq})")


def _inbox(args: argparse.Namespace) -> None:
    """Poll every stream the sender subscribes to (REQ-012)."""
    root = _get_root(args)
    sender: str = args.sender
    limit: int = getattr(args, "limit", 10)
    use_json: bool = getattr(args, "use_json", False)

    async def _run() -> Any:
        svc, _, _, backend = await _build_service(root)
        try:
            return await svc.poll_all(sender, max_per_stream=limit)
        finally:
            await _shutdown(backend)

    result = asyncio.run(_run())
    if use_json:
        _print_json({stream: [m.model_dump() for m in msgs] for stream, msgs in result.items()})
    elif not result:
        _write("No new messages.")
    else:
        for stream, msgs in result.items():
            _write(f"{stream} ({len(msgs)} unread):")
            for msg in msgs:
                _print_message(msg)


def _read(args: argparse.Namespace) -> None:
    """Read channel or DM history without advancing any cursor (REQ-012)."""
    root = _get_root(args)
    sender: str = args.sender
    limit: int = getattr(args, "limit", 20)
    use_json: bool = getattr(args, "use_json", False)
    channel: str | None = getattr(args, "channel", None)
    dm: str | None = getattr(args, "dm", None)

    if channel:
        stream = f"arc.channel.{channel}"
    elif dm:
        stream = f"arc.agent.{dm}"
    else:
        sys.stderr.write("arc team read: specify --channel or --dm\n")
        sys.exit(2)

    async def _run() -> Any:
        svc, _, _, backend = await _build_service(root)
        try:
            return await svc.poll(stream, sender, max_messages=limit)
        finally:
            await _shutdown(backend)

    messages = asyncio.run(_run())
    if use_json:
        _print_json([m.model_dump() for m in messages])
    else:
        _write(f"{stream} ({len(messages)} messages):")
        for msg in messages:
            _print_message(msg)


def _thread(args: argparse.Namespace) -> None:
    """Show all messages in a thread, chronologically (REQ-012)."""
    root = _get_root(args)
    thread_id: str = args.thread_id
    stream: str = args.stream
    use_json: bool = getattr(args, "use_json", False)

    async def _run() -> Any:
        svc, _, _, backend = await _build_service(root)
        try:
            return await svc.get_thread(stream, thread_id)
        finally:
            await _shutdown(backend)

    messages = asyncio.run(_run())
    if use_json:
        _print_json([m.model_dump() for m in messages])
    else:
        _write(f"Thread {thread_id} ({len(messages)} messages):")
        for msg in messages:
            _print_message(msg)


# ---------------------------------------------------------------------------
# Supervised daemon orchestrator (E2) — arc team up / down
# ---------------------------------------------------------------------------


class TeamSupervisor:
    """Supervise one ``arc agent serve`` daemon per team member (REQ-051).

    Mirrors the arcgateway runner supervision shape: one task per member inside
    a single ``asyncio.TaskGroup`` so a crash never kills siblings, each task
    restarts its daemon after a backoff delay, and a stop gate terminates every
    child on graceful shutdown.
    """

    def __init__(
        self,
        targets: dict[str, str],
        spawn: Any,
        backoff: float = 1.0,
    ) -> None:
        self.targets = targets
        self._spawn = spawn
        self._backoff = backoff
        self._stop = asyncio.Event()
        self._procs: dict[str, Any] = {}

    def stop(self) -> None:
        """Signal all supervised members to shut down."""
        self._stop.set()

    async def run(self) -> None:
        """Supervise every member until :meth:`stop` (or a signal) fires."""
        async with asyncio.TaskGroup() as tg:
            for name, agent_dir in self.targets.items():
                tg.create_task(self._supervise(name, agent_dir), name=f"member:{name}")
            tg.create_task(self._stopper(), name="stop_gate")

    async def _supervise(self, name: str, agent_dir: str) -> None:
        while not self._stop.is_set():
            proc = await self._spawn(name, agent_dir)
            self._procs[name] = proc
            if self._stop.is_set():
                proc.terminate()
                return
            await proc.wait()
            if self._stop.is_set():
                return
            await asyncio.sleep(self._backoff)

    async def _stopper(self) -> None:
        await self._stop.wait()
        for proc in self._procs.values():
            proc.terminate()


def _arc_executable() -> str:
    """Resolve the ``arc`` CLI entry point for spawning member daemons."""
    import shutil

    found = shutil.which("arc")
    return found if found else sys.argv[0]


async def _spawn_agent_serve(name: str, agent_dir: str) -> Any:
    """Spawn ``arc agent serve <agent_dir>`` as a supervised child (REQ-051)."""
    # Args are internal: the resolved arc binary and a registered agent dir.
    return await asyncio.create_subprocess_exec(
        _arc_executable(),
        "agent",
        "serve",
        agent_dir,
    )


async def _build_supervisor(root: Path, team_id: str, spawn: Any | None = None) -> TeamSupervisor:
    """Map a team's members to ``(handle -> agent_dir)`` and build a supervisor."""
    from arcteam.team import TeamStore

    _, registry, audit, backend = await _build_service(root)
    try:
        team = await TeamStore(backend, audit).get(team_id)
        if team is None:
            raise ValueError(f"Team not found: {team_id}")
        targets: dict[str, str] = {}
        for did in team.members:
            entity = await registry.get(did)
            if entity is None or not entity.workspace_path:
                continue
            targets[entity.handle] = str(Path(entity.workspace_path).parent)
    finally:
        await _shutdown(backend)
    return TeamSupervisor(targets, spawn=spawn or _spawn_agent_serve)


def _team_pid_path(root: Path, team_id: str) -> Path:
    """Return the PID file path for a running team supervisor."""
    safe = team_id.replace(":", "_").replace("/", "_")
    return root / "run" / f"team-{safe}.pid"


async def _run_supervisor(supervisor: TeamSupervisor) -> None:
    """Install signal handlers and run the supervisor until shutdown."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, supervisor.stop)
        except (NotImplementedError, OSError):
            pass
    await supervisor.run()


def _up(args: argparse.Namespace) -> None:
    """Boot each team member as a supervised ``arc agent serve`` daemon (REQ-051)."""
    root = _get_root(args)
    team_id: str = args.team_id

    supervisor = asyncio.run(_build_supervisor(root, team_id))
    if not supervisor.targets:
        _write(f"Team {team_id} has no runnable members.")
        return

    pid_path = _team_pid_path(root, team_id)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n")
    _write(f"Starting {len(supervisor.targets)} member daemon(s) for team {team_id}...")
    try:
        asyncio.run(_run_supervisor(supervisor))
    finally:
        pid_path.unlink(missing_ok=True)


def _stop_pid(pid_path: Path, kill: Callable[[int, int], object] = os.kill) -> None:
    """Send SIGTERM to the PID recorded in ``pid_path`` and remove the file."""
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    pid_path.unlink(missing_ok=True)


def _down(args: argparse.Namespace) -> None:
    """Stop a running team's member daemons (REQ-051)."""
    root = _get_root(args)
    team_id: str = args.team_id
    pid_path = _team_pid_path(root, team_id)
    if not pid_path.exists():
        _write(f"Team {team_id} is not running.")
        return
    _stop_pid(pid_path)
    _write(f"Stopped team {team_id}.")


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
        "--type", dest="entity_type", required=True, choices=["agent", "user"], help="Entity type."
    )
    p.add_argument("--roles", default="", help="Comma-separated roles.")
    p.add_argument(
        "--workspace",
        default=None,
        help="Agent workspace path (default: cwd). Resolved to absolute. "
        "Must exist as a directory; ~ and $VAR forms rejected (SR-6).",
    )

    # entities
    p = subs.add_parser("entities", help="List registered entities.")
    p.add_argument("--role", default=None, help="Filter by role.")

    # channels
    subs.add_parser("channels", help="List available channels.")

    # memory status
    subs.add_parser("memory-status", help="Show team memory status.")

    # backfill-workspaces (SPEC-019 T1.4)
    p = subs.add_parser(
        "backfill-workspaces",
        help="Backfill workspace_path on registered agents from team/*/arcagent.toml.",
    )
    p.add_argument(
        "--team-dir",
        dest="team_dir",
        default="team",
        help="Directory containing per-agent subdirectories (default: ./team).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Persist changes. Without this flag, the command is dry-run.",
    )

    # create (team)
    p = subs.add_parser("create", help="Create a team.")
    p.add_argument("team_id", help="Team id.")
    p.add_argument("--name", default=None, help="Display name (default: team id).")
    p.add_argument("--channel", required=True, help="Default channel for team traffic.")
    p.add_argument("--members", default="", help="Comma-separated member refs (handles/DIDs).")
    p.add_argument("--goal", default=None, help="Optional goal artifact ref.")

    # add-member
    p = subs.add_parser("add-member", help="Add a member to a team.")
    p.add_argument("team_id", help="Team id.")
    p.add_argument("member", help="Member ref (handle or DID).")

    # remove-member
    p = subs.add_parser("remove-member", help="Remove a member from a team.")
    p.add_argument("team_id", help="Team id.")
    p.add_argument("member", help="Member ref (handle or DID).")

    # send
    p = subs.add_parser("send", help="Send a signed message.")
    p.add_argument("--sender", required=True, help="Sender ref (agent://handle).")
    p.add_argument("--to", required=True, help="Target refs (comma-separated).")
    p.add_argument("--body", required=True, help="Message body.")
    p.add_argument("--type", dest="type", default=None, help="Message type.")
    p.add_argument("--priority", default=None, help="Priority.")
    p.add_argument("--action", action="store_true", help="Mark action required.")
    p.add_argument("--refs", default=None, help="Comma-separated references.")
    p.add_argument("--thread-id", dest="thread_id", default=None, help="Thread id (for replies).")

    # inbox
    p = subs.add_parser("inbox", help="Check inbox across subscribed streams.")
    p.add_argument("--sender", required=True, help="Whose inbox to poll.")
    p.add_argument("--limit", type=int, default=10, help="Max messages per stream.")

    # read
    p = subs.add_parser("read", help="Read channel or DM history.")
    p.add_argument("--sender", required=True, help="Reader ref.")
    p.add_argument("--channel", default=None, help="Channel name.")
    p.add_argument("--dm", default=None, help="DM entity handle.")
    p.add_argument("--limit", type=int, default=20, help="Max messages.")

    # thread
    p = subs.add_parser("thread", help="View a message thread.")
    p.add_argument("thread_id", help="Thread id.")
    p.add_argument("--stream", required=True, help="Stream name.")

    # up
    p = subs.add_parser("up", help="Boot each team member as a supervised daemon.")
    p.add_argument("team_id", help="Team id.")

    # down
    p = subs.add_parser("down", help="Stop a running team's member daemons.")
    p.add_argument("team_id", help="Team id.")

    return parser


_SUBCOMMAND_MAP = {
    "status": _status,
    "config": _config_cmd,
    "init": _init_cmd,
    "register": _register,
    "entities": _entities,
    "channels": _channels,
    "memory-status": _memory_status,
    "backfill-workspaces": _backfill_workspaces,
    "create": _create_team,
    "add-member": _add_member,
    "remove-member": _remove_member,
    "send": _send,
    "inbox": _inbox,
    "read": _read,
    "thread": _thread,
    "up": _up,
    "down": _down,
}


def team_handler(args: list[str]) -> None:
    """Top-level handler for `arc team <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc team ...`.
    """
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)
