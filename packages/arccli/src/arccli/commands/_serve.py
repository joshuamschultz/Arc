"""Shared team-serve bootstrap for ``arc team serve`` and ``arc ui start``.

Brings the messaging infra up out of the box so an operator never hand-starts a
broker:

  1. Start a managed NATS JetStream server (or reuse one already listening).
  2. Discover every agent directory under the team root and register it with
     arcteam so it can message and shows a DID in the registry.

Registration is best-effort — a broker or operator-custody problem degrades to
a printed warning, never a crash, because the dashboard's fleet roster is
folder-scanned (arcgateway.team_roster) and therefore surfaces the agents
regardless of registry state.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from arccli.commands._shared import write as _write

_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"


def nats_url() -> str:
    """Resolve the broker URL — same source as the ``arc team`` CLI path."""
    return os.environ.get("ARCTEAM_NATS_URL", _DEFAULT_NATS_URL)


def _jetstream_store_dir() -> Path:
    """Persistent JetStream store so registrations survive a broker restart."""
    return Path("~/.arc/nats/jetstream").expanduser()


def discover_agent_dirs(team_root: Path) -> list[Path]:
    """Immediate subdirectories of ``team_root`` that contain an arcagent.toml."""
    if not team_root.is_dir():
        return []
    return sorted(
        toml.parent for toml in team_root.glob("*/arcagent.toml") if toml.parent.is_dir()
    )


def _entity_for(agent_dir: Path) -> Any:
    """Build an arcteam ``Entity`` from an agent dir's config + minted identity.

    The identity is minted from the agent's own ``arcagent.toml`` (persisted on
    first mint), so the DID registered here is the SAME key the running agent
    signs with — the signed bus can verify its messages (REQ-030).
    """
    from arcagent.core.config import load_config
    from arcteam.types import Entity, EntityType
    from arctrust import AgentIdentity

    config_path = agent_dir / "arcagent.toml"
    config = load_config(config_path)
    identity = AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=config_path,
    )
    name = config.agent.name
    return Entity(
        did=identity.did,
        handle=name,
        id=f"agent://{name}",
        name=name,
        type=EntityType("agent"),
        public_key=identity.public_key.hex(),
        roles=["executor"],
        workspace_path=str(agent_dir / "workspace"),
    )


async def register_folder_agents(root: Path, agent_dirs: list[Path]) -> int:
    """Register each discovered agent through one service. Returns the count present.

    Idempotent — an "already registered" entity still counts as present. A
    single agent's failure is warned and skipped so one bad config never blocks
    the rest.
    """
    from arccli.commands.team import _build_service, _shutdown

    _, registry, _, backend = await _build_service(root)
    present = 0
    try:
        for agent_dir in agent_dirs:
            try:
                await registry.register(_entity_for(agent_dir))
                present += 1
            except ValueError as exc:
                if "already registered" in str(exc).lower():
                    present += 1
                else:
                    _write(f"  warn: could not register {agent_dir.name}: {exc}")
    finally:
        await _shutdown(backend)
    return present


async def bootstrap_infra(team_root: Path) -> Any:
    """Start (or reuse) NATS and auto-register the folder's agents.

    Returns the :class:`~arcteam.nats_server.ManagedNatsServer` handle this call
    started (the caller must ``await handle.stop()`` on shutdown), or ``None``
    when a broker was reused or none could be started. Prints one status line
    describing what came up. Fail-open throughout — the dashboard still serves
    the folder-scanned roster even if messaging infra is unavailable.
    """
    from arcteam.config import TeamConfig
    from arcteam.nats_server import NatsServerUnavailableError, ensure_nats_server

    url = nats_url()
    agent_dirs = discover_agent_dirs(team_root)

    handle = None
    try:
        handle = await ensure_nats_server(url=url, store_dir=_jetstream_store_dir())
    except NatsServerUnavailableError as exc:
        _write(f"  Messaging: {exc}")
        _write(
            "  Messaging: agents still appear in the roster (folder scan); "
            "team status/send are unavailable until a broker is running."
        )
        return None

    if handle is None:
        _write(f"  Messaging: reusing NATS broker already running at {url}")
    else:
        _write(f"  Messaging: started NATS JetStream at {url} (pid {handle.process.pid})")

    if agent_dirs:
        try:
            count = await register_folder_agents(TeamConfig().root, agent_dirs)
            _write(f"  Registered {count}/{len(agent_dirs)} agent(s) with arcteam.")
        except Exception as exc:  # reason: fail-open — roster is folder-scanned
            _write(f"  Warning: agent auto-registration degraded: {exc}")

    return handle


__all__ = [
    "bootstrap_infra",
    "discover_agent_dirs",
    "nats_url",
    "register_folder_agents",
]
