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

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from arccli.commands._shared import write as _write

_logger = logging.getLogger("arccli.serve")


def nats_url() -> str:
    """Resolve the broker URL through arcteam's single source of truth."""
    from arcteam.config import default_nats_url

    return default_nats_url()


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
    import arcagent
    from arcteam.types import Entity, EntityType
    from arctrust import AgentIdentity

    config_path = agent_dir / "arcagent.toml"
    config = arcagent.load_config(config_path)
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
    started (the caller must reap it on shutdown), or ``None`` when a broker was
    reused or none could be started. Prints one status line describing what came
    up. Fail-open throughout — the dashboard still serves the folder-scanned
    roster even if messaging infra is unavailable.

    Delegates to :func:`arcgateway.broker_bootstrap.start_broker` rather than
    calling ``ensure_nats_server`` directly: COMP-008 exists so every launch
    path shares one broker lifecycle, and a second hand-rolled one here is the
    reaping bug that only shows up on whichever path nobody fixed.
    """
    from arcgateway.broker_bootstrap import start_broker
    from arcteam.config import TeamConfig

    agent_dirs = discover_agent_dirs(team_root)

    broker = await start_broker()
    url = broker.url
    if not broker.available:
        _write(f"  Messaging: {broker.reason}")
        _write(
            "  Messaging: agents still appear in the roster (folder scan); "
            "team status/send are unavailable until a broker is running."
        )
        return None

    handle = broker.managed
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


def _did_getter(agent: Any) -> Callable[[], str]:
    """Read ``agent.did`` on demand, captured per agent (not per loop variable)."""
    return lambda: str(agent.did)


def _promotable_document_types() -> frozenset[str] | None:
    """Operator allowlist of promotable document types, or None for no restriction.

    ``ARC_SHARED_KNOWLEDGE_TYPES`` is a comma-separated list (e.g. ``procedure,policy``)
    that lets the operator control WHAT gets shared to the fleet rather than every
    personal note becoming fleet-wide. Unset/empty → any type may be promoted
    (personal zero-config).
    """
    raw = os.environ.get("ARC_SHARED_KNOWLEDGE_TYPES", "").strip()
    if not raw:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


async def install_fleet_shared_knowledge(
    team_root: Path, started: list[tuple[Any, Path, str]]
) -> int:
    """Attach the signed fleet shared-knowledge tools to every started team agent (H-027).

    Makes ``shared_knowledge_promote`` (and retrieve/search/revoke) LIVE: an agent
    can promote one of its OWN curated personal documents into the operator-scoped,
    signed fleet collection under ``<team_root>/shared/knowledge``. The service owns
    every gate (membership, personal scope, owner==caller, no-write-down, Ed25519
    signature, TOFU pin) plus the operator promotion-type filter; each promotion is
    audited through the promoting agent's own telemetry sink (never a silent side
    channel).

    Best-effort: arcmemory absent, or one agent's wiring failing, degrades to a
    warning and never blocks the fleet.
    """
    if not started:
        return 0
    try:
        from arcmemory.adapters import PersonalKnowledgeAdapter
    except ImportError:
        return 0
    import arcagent
    from arcteam.shared_knowledge import (
        ComposedSharedKnowledgeAgent,
        FleetSharedKnowledgeComposition,
        FleetSharedKnowledgeService,
    )
    from arcteam.team import Team

    dids = [agent.did for agent, _workspace, _clearance in started]
    team = Team(
        id="team:fleet",
        name="fleet",
        members=dids,
        default_channel="channel://fleet",
    )
    service = FleetSharedKnowledgeService.for_team_root(
        team_root, promotable_document_types=_promotable_document_types()
    )
    composition = FleetSharedKnowledgeComposition(team, service)
    members = [
        ComposedSharedKnowledgeAgent(
            agent,
            PersonalKnowledgeAdapter(workspace, agent.did),
            arcagent.KnowledgeAccess(agent.did, clearance),
            agent.extension_signer,
            agent.audit_sink,
        )
        for agent, workspace, clearance in started
    ]
    await composition.start(members)
    return len(members)


async def serve_fleet_agents(
    team_root: Path,
    fleet: Any,
    *,
    warm: Callable[[str, Any], Awaitable[Any]] | None = None,
    deliver_for: Callable[[Callable[[], str]], Any] | None = None,
) -> int:
    """Start every discovered team agent so its messaging inbox loop runs (MSG4).

    For each agent under ``team_root`` this loads the agent, calls
    ``startup()`` — which spawns the ``messaging_inbox_loop`` durable PUSH
    consumer when ``[modules.messaging]`` is enabled, so the agent WAKES on a
    DM / @mention / channel post and can reply — and registers the started
    instance in ``fleet`` (an :class:`arcgateway.fleet.FleetRegistry`). The
    embedded gateway's agent factory then reuses that SAME instance for web
    chat, guaranteeing one durable consumer per agent (no double subscription).

    ``warm(did)`` is an optional callback that touches the gateway factory for a
    started agent so it also shows LIVE in the fleet roster; a warm failure is
    logged and never blocks consuming. Best-effort per agent: one bad config is
    warned and skipped so a single failure never leaves the rest of the fleet
    dark. Returns the number of agents started.

    Must be awaited inside the serving process's event loop (the started agents'
    inbox-loop tasks live there for the process lifetime); ``arc ui start`` runs
    it from a lifespan startup hook, after the broker and gateway are up.
    """
    from arccli.commands.agent._common import _load_arcagent

    agent_dirs = discover_agent_dirs(team_root)
    started = 0
    knowledge_members: list[tuple[Any, Path, str]] = []
    for agent_dir in agent_dirs:
        try:
            agent, _config, _config_path = _load_arcagent(agent_dir)
            # Wire channel delivery BEFORE startup so agent:ready carries it and
            # the scheduler can deliver a fired schedule's output — bound to THIS
            # agent's DID so it goes out through this agent's bot, not another's.
            # Late-bound: startup() is what materialises the identity, so
            # ``agent.did`` is still "" here and must be read at send time.
            if deliver_for is not None:
                deliver_fn = deliver_for(_did_getter(agent))
                if deliver_fn is not None:
                    agent.set_channel_deliver_fn(deliver_fn)
            await agent.startup()
            fleet.add(agent.did, agent)
            started += 1
        except Exception as exc:  # reason: best-effort — one bad agent never blocks the fleet
            _write(f"  warn: could not start agent {agent_dir.name}: {exc}")
            continue
        # Collect the shared-knowledge inputs separately so a missing attribute
        # never un-starts an already-started agent — shared knowledge is additive.
        try:
            knowledge_members.append((agent, agent.workspace, _config.security.clearance))
        except Exception:  # reason: additive — an ineligible agent just skips promotion
            _logger.debug("fleet: %s not eligible for shared knowledge", agent.did, exc_info=True)
        if warm is not None:
            try:
                await warm(agent.did, agent)
            except Exception:  # reason: LIVE-status is cosmetic; consuming already works
                _logger.warning("fleet: could not warm %s for LIVE", agent.did, exc_info=True)

    # Wire the signed fleet shared-knowledge surface across the started members so
    # shared_knowledge_promote is actually LIVE. Best-effort — a wiring failure
    # must never leave the messaging fleet dark (the tools it just started).
    try:
        shared = await install_fleet_shared_knowledge(team_root, knowledge_members)
        if shared:
            _write(f"  Fleet: shared-knowledge promotion active for {shared} agent(s).")
    except Exception:  # reason: fail-open — shared knowledge is additive to messaging
        _logger.warning("fleet: could not install shared knowledge", exc_info=True)
    return started


__all__ = [
    "bootstrap_infra",
    "discover_agent_dirs",
    "install_fleet_shared_knowledge",
    "nats_url",
    "register_folder_agents",
    "serve_fleet_agents",
]
