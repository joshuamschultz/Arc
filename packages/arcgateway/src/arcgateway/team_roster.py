"""Discover agents declared on disk and overlay live status.

The roster is the canonical fleet view consumed by arcui's Agent Fleet page.
Each entry merges three inputs:

1. ``team/<dir>_agent/arcagent.toml`` — agent's own self-description (identity,
   model, optional ``[ui]`` block).
2. The set of currently-connected agent ids (``online_ids``) supplied by the
   caller (typically arcui's :class:`AgentRegistry`).
3. Sane derivations for missing fields — deterministic hash color, provider
   inference from model slug.

Single source of truth: the agent's own ``arcagent.toml``. No sidecar files,
no cross-process sync (D-003).
"""

from __future__ import annotations

import hashlib
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from arcgateway.agent_config import load_ui_section

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RosterEntry:
    """One agent row in the fleet roster."""

    agent_id: str
    name: str
    did: str
    org: str | None
    type: str | None
    workspace_path: str  # absolute path to agent dir (team/<dir>_agent/)
    model: str | None
    provider: str | None  # inferred from "<provider>/<model>"
    online: bool
    display_name: str
    color: str
    role_label: str
    hidden: bool


def list_team(*, team_root: Path, online_ids: set[str]) -> list[RosterEntry]:
    """Enumerate all agents under ``team_root`` and overlay online status.

    Walks ``team_root`` for directories matching ``*_agent`` that contain an
    ``arcagent.toml``. Skips other directories silently. Each TOML parse error
    is logged at WARN and that agent is omitted (we never fail the whole
    roster on one bad file — the fleet stays observable).

    Args:
        team_root: Directory containing ``<name>_agent/`` subdirs.
        online_ids: Set of agent ids the caller knows are currently connected.

    Returns:
        List of :class:`RosterEntry`, sorted by ``agent_id``.
    """
    entries: list[RosterEntry] = []
    if not team_root.exists():
        return entries

    for agent_dir in sorted(team_root.glob("*_agent")):
        if not agent_dir.is_dir():
            continue
        toml_path = agent_dir / "arcagent.toml"
        if not toml_path.exists():
            continue
        entry = _load_agent(agent_dir, toml_path, online_ids)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda r: r.agent_id)
    return entries


def _load_agent(
    agent_dir: Path,
    toml_path: Path,
    online_ids: set[str],
) -> RosterEntry | None:
    try:
        cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("team_roster: failed to load %s: %s", toml_path, exc)
        return None

    agent = cfg.get("agent", {}) if isinstance(cfg.get("agent"), dict) else {}
    identity = cfg.get("identity", {}) if isinstance(cfg.get("identity"), dict) else {}
    llm = cfg.get("llm", {}) if isinstance(cfg.get("llm"), dict) else {}
    ui = load_ui_section(cfg)

    name = agent.get("name") or _strip_agent_suffix(agent_dir.name)
    agent_id = name
    model = llm.get("model") if isinstance(llm.get("model"), str) else None
    agent_type = agent.get("type") if isinstance(agent.get("type"), str) else None

    return RosterEntry(
        agent_id=agent_id,
        name=name,
        did=identity.get("did", "") if isinstance(identity.get("did"), str) else "",
        org=agent.get("org") if isinstance(agent.get("org"), str) else None,
        type=agent_type,
        workspace_path=str(agent_dir),
        model=model,
        provider=_provider_from_model(model),
        online=agent_id in online_ids,
        display_name=ui.display_name or name,
        color=ui.color or _deterministic_color(agent_id),
        role_label=ui.role_label or agent_type or "",
        hidden=ui.hidden,
    )


def _strip_agent_suffix(dir_name: str) -> str:
    return dir_name[:-6] if dir_name.endswith("_agent") else dir_name


def _provider_from_model(model: str | None) -> str | None:
    if model and "/" in model:
        return model.split("/", 1)[0]
    return None


def _deterministic_color(agent_id: str) -> str:
    digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()
    return f"#{digest[:6]}"
