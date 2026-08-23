"""Fill an existing agent's TOML with settings the current scaffold declares.

A module gains a setting; ``render_agent_config`` gains a line; every agent
built before that day keeps running the default with nothing in its file to
find or change. Agents built on different days then disagree about what is even
configurable. This merges the canonical scaffold INTO an existing config,
additively: a key already in the file — operator value, comment and all — is
never touched, so the merge is idempotent and safe to run on a live fleet.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from arccli.commands.agent._common import render_agent_config

# Identity is minted per agent and signed; a scaffold value here would hand two
# agents the same name or DID. The sync adds settings, never identity.
_NEVER_SYNCED = frozenset({"agent", "identity"})


@dataclass(frozen=True)
class ConfigSyncResult:
    """What one agent's file was missing, and whether the fix was written."""

    path: Path
    added: tuple[str, ...]
    written: bool

    @property
    def changed(self) -> bool:
        return bool(self.added)


def _is_table(value: Any) -> bool:
    return isinstance(value, dict | Table)


def _merge(source: Any, target: Any, prefix: str, added: list[str]) -> None:
    """Copy every key of ``source`` missing from ``target``, recursing into tables."""
    for key, value in source.items():
        path = f"{prefix}{key}"
        if key not in target:
            target[key] = value
            added.append(path)
            continue
        if _is_table(value) and _is_table(target[key]):
            _merge(value, target[key], f"{path}.", added)


def plan_config_sync(agent_dir: Path) -> tuple[TOMLDocument, list[str]]:
    """The agent's document with scaffold gaps filled, plus the keys that filled them."""
    config_path = agent_dir / "arcagent.toml"
    existing = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    agent_table = existing.get("agent", {})
    identity_table = existing.get("identity", {})
    scaffold = tomlkit.parse(
        render_agent_config(
            name=str(agent_table.get("name", agent_dir.name)),
            tier=str(agent_table.get("tier", "personal")),
            did=str(identity_table.get("did", "")),
        )
    )

    added: list[str] = []
    for section, value in scaffold.items():
        if section in _NEVER_SYNCED:
            continue
        if section not in existing:
            existing[section] = value
            added.append(section)
        elif _is_table(value) and _is_table(existing[section]):
            _merge(value, existing[section], f"{section}.", added)
    return existing, added


def sync_agent_config(agent_dir: Path, *, dry_run: bool = False) -> ConfigSyncResult:
    """Add every scaffold setting this agent's TOML lacks. Existing values stand."""
    merged, added = plan_config_sync(agent_dir)
    config_path = agent_dir / "arcagent.toml"
    write = bool(added) and not dry_run
    if write:
        config_path.write_text(tomlkit.dumps(merged), encoding="utf-8")
    return ConfigSyncResult(path=config_path, added=tuple(added), written=write)


def discover_agent_dirs(team_root: Path) -> list[Path]:
    """Every immediate subdirectory of ``team_root`` holding an arcagent.toml."""
    if not team_root.is_dir():
        return []
    return sorted(toml.parent for toml in team_root.glob("*/arcagent.toml"))


__all__ = [
    "ConfigSyncResult",
    "discover_agent_dirs",
    "plan_config_sync",
    "sync_agent_config",
]
