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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcgateway.agent_config import load_ui_section

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RosterEntry:
    """One agent row in the fleet roster.

    ``harness`` (H-040) is the runtime kind: ``"arcagent"`` for a native
    disk-defined agent, or a foreign harness name (``"hermes"``, …) for an
    enrolled foreign member sourced from the registry. It drives the roster's
    type badge and, later, the capability-gated detail tabs.
    """

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
    harness: str = "arcagent"


def list_team(*, team_root: Path, online_ids: set[str]) -> list[RosterEntry]:
    """Enumerate all agents under ``team_root`` and overlay online status.

    An agent is any immediate subdirectory of ``team_root`` that contains an
    ``arcagent.toml`` — the presence of that file is the sole discovery signal,
    so both ``arc agent create <name>`` (bare ``<name>/``) and the legacy
    ``<name>_agent/`` layout are surfaced. Other directories are skipped
    silently. Each TOML parse error is logged at WARN and that agent is omitted
    (we never fail the whole roster on one bad file — the fleet stays observable).

    Args:
        team_root: Directory containing per-agent subdirs.
        online_ids: Set of agent ids the caller knows are currently connected.

    Returns:
        List of :class:`RosterEntry`, sorted by ``agent_id``.
    """
    entries: list[RosterEntry] = []
    if not team_root.exists():
        return entries

    for toml_path in sorted(team_root.glob("*/arcagent.toml")):
        agent_dir = toml_path.parent
        if not agent_dir.is_dir():
            continue
        entry = _load_agent(agent_dir, toml_path, online_ids)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda r: r.agent_id)
    return entries


def _load_llm_section(agent_dir: Path) -> dict[str, Any]:
    """Read `[llm]` from the agent's arcllm.toml — the file that owns LLM-wire.

    A missing or unparseable arcllm.toml yields an empty section (the agent
    still shows up, just without a model) — one bad file never drops an agent
    off the roster.
    """
    llm_path = agent_dir / "arcllm.toml"
    try:
        cfg = tomllib.loads(llm_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    llm = cfg.get("llm")
    return llm if isinstance(llm, dict) else {}


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
    llm = _load_llm_section(agent_dir)
    ui = load_ui_section(cfg)

    name = agent.get("name") or _strip_agent_suffix(agent_dir.name)
    agent_id = name
    model = llm.get("model") if isinstance(llm.get("model"), str) else None
    agent_type = agent.get("type") if isinstance(agent.get("type"), str) else None
    harness = agent.get("harness") if isinstance(agent.get("harness"), str) else "arcagent"

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
        harness=harness or "arcagent",
    )


def foreign_member_entry(entity: Any, *, online: bool) -> RosterEntry:
    """Build a roster row for a foreign (non-arcagent) registry member (H-040 §4).

    A foreign member has no ``arcagent.toml`` on disk, so its roster row comes
    from the ``EntityRegistry`` record instead: DID, handle, capabilities, and
    the harness badge. It declares no model/provider/workspace of arc's — the
    capability-gated detail tabs (Slice 2) show only what it actually supports.
    """
    handle = getattr(entity, "handle", "") or getattr(entity, "id", "")
    name = getattr(entity, "name", None) or handle
    return RosterEntry(
        agent_id=handle,
        name=name,
        did=getattr(entity, "did", ""),
        org=None,
        type=getattr(getattr(entity, "type", None), "value", None),
        workspace_path="",
        model=None,
        provider=None,
        online=online,
        display_name=name,
        color=_deterministic_color(handle),
        role_label=getattr(entity, "harness", "arcagent"),
        hidden=False,
        harness=getattr(entity, "harness", "arcagent") or "arcagent",
    )


def merge_foreign_members(
    base: list[RosterEntry],
    entities: Iterable[Any],
    online_ids: set[str],
) -> list[RosterEntry]:
    """Append eligible foreign members to a disk-scanned roster (H-040 §4).

    Only ``active`` members whose harness is NOT ``arcagent`` are added (native
    agents already come from the disk scan), and only if their DID is not already
    present — so a member that happens to have both a disk config and a registry
    row is not double-listed. Sorted by ``agent_id`` for a stable roster.
    """
    seen_dids = {e.did for e in base if e.did}
    merged = list(base)
    for entity in entities:
        harness = getattr(entity, "harness", "arcagent")
        status = getattr(getattr(entity, "status", None), "value", "active")
        did = getattr(entity, "did", "")
        if harness == "arcagent" or status != "active" or did in seen_dids:
            continue
        handle = getattr(entity, "handle", "")
        merged.append(foreign_member_entry(entity, online=handle in online_ids))
        seen_dids.add(did)
    merged.sort(key=lambda r: r.agent_id)
    return merged


def _strip_agent_suffix(dir_name: str) -> str:
    return dir_name[:-6] if dir_name.endswith("_agent") else dir_name


def _provider_from_model(model: str | None) -> str | None:
    if model and "/" in model:
        return model.split("/", 1)[0]
    return None


def _deterministic_color(agent_id: str) -> str:
    digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()
    return f"#{digest[:6]}"
