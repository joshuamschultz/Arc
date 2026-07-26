"""Agent roster enumeration + selection for arctui (SPEC-058 COMP-015, REQ-143).

arctui drives an agent selected from the arc roster — the same discovery arcui
uses (``arcgateway.team_roster``: any ``team_root/*/arcagent.toml`` is an agent) —
instead of the CWD-only single load. This module is the pure core; the
interactive picker screen is a thin layer over :func:`select_agent`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentRef:
    """A selectable agent: enough to identify it and load its config."""

    agent_id: str
    display_name: str
    root: Path
    """Directory containing ``arcagent.toml`` (the agent root, not the workspace subdir)."""

    config_path: Path
    model: str


def default_team_root() -> Path:
    """The global team root (``~/.arc/team``) — arc's fleet discovery location."""
    from arctrust.paths import arc_home

    return arc_home() / "team"


def list_agents(team_root: Path | None = None) -> list[AgentRef]:
    """Enumerate agents under ``team_root`` (default ``~/.arc/team``), sorted by id.

    Returns an empty list when the root does not exist — the caller offers
    ``arc agent create`` rather than entering no-agent mode silently (REQ-143).
    """
    from arcgateway.team_roster import list_team

    root = team_root if team_root is not None else default_team_root()
    if not root.is_dir():
        return []
    refs: list[AgentRef] = []
    for entry in list_team(team_root=root, online_ids=set()):
        agent_root = Path(entry.workspace_path)
        refs.append(
            AgentRef(
                agent_id=entry.agent_id,
                display_name=entry.display_name or entry.agent_id,
                root=agent_root,
                config_path=agent_root / "arcagent.toml",
                model=entry.model or "",
            )
        )
    return sorted(refs, key=lambda r: r.agent_id)


@dataclass(frozen=True)
class ResolvedAgent:
    """The outcome of resolving which agent to drive at launch.

    ``reason`` tells the caller how to proceed: ``named``/``single`` → run
    ``selected``; ``ambiguous`` → present the picker over ``candidates``;
    ``empty`` → offer ``arc agent create``; ``unknown`` → the given name matched
    nothing.
    """

    selected: AgentRef | None
    candidates: list[AgentRef]
    reason: str  # "named" | "single" | "ambiguous" | "empty" | "unknown"


def resolve_agent(name: str | None = None, team_root: Path | None = None) -> ResolvedAgent:
    """Resolve the agent to drive from the roster + an optional ``--agent`` name."""
    candidates = list_agents(team_root)
    if not candidates:
        return ResolvedAgent(None, [], "empty")
    if name:
        chosen = select_agent(candidates, name)
        return ResolvedAgent(chosen, candidates, "named" if chosen else "unknown")
    if len(candidates) == 1:
        return ResolvedAgent(candidates[0], candidates, "single")
    return ResolvedAgent(None, candidates, "ambiguous")


def select_agent(agents: list[AgentRef], name: str | None) -> AgentRef | None:
    """Resolve which agent to drive.

    A given ``name`` matches an ``agent_id`` or ``display_name``. With no name,
    a single agent auto-selects; multiple agents return ``None`` so the caller
    presents the interactive picker.
    """
    if name:
        for agent in agents:
            if name in (agent.agent_id, agent.display_name):
                return agent
        return None
    if len(agents) == 1:
        return agents[0]
    return None


__all__ = [
    "AgentRef",
    "ResolvedAgent",
    "default_team_root",
    "list_agents",
    "resolve_agent",
    "select_agent",
]
