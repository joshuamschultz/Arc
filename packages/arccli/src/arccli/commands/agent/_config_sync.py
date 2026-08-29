"""Fill an existing agent's TOML with settings the current scaffold declares,
and (separately) refresh values still at whatever default the scaffold wrote.

A module gains a setting; ``render_agent_config`` gains a line; every agent
built before that day keeps running the default with nothing in its file to
find or change. Agents built on different days then disagree about what is even
configurable. ``sync_agent_config`` merges the canonical scaffold INTO an
existing config, additively: a key already in the file — operator value,
comment and all — is never touched, so the merge is idempotent and safe to
run on a live fleet.

H-039 materializes every field at its default (rather than a hand-picked
subset), which raises the tradeoff a purely additive sync cannot answer:
"the model's default for a field changed — should an agent that never touched
it pick that up?" ``refresh_agent_config`` answers it via a small snapshot
(``.arc-config-defaults.json``, written alongside the TOML at create time and
after every refresh) recording the value the generator last wrote at each
key. On refresh: a key whose CURRENT file value still equals its snapshotted
value has not been touched since — it is safe to advance to today's default.
A key that differs was deliberately changed by an operator (or coincidentally
re-typed to something else) and is left alone. This is a best-effort
heuristic, not a guarantee: an operator who re-enters the exact default value
by hand is indistinguishable from one who never touched it, and a value the
scaffold does not yet snapshot (an agent built before this mechanism existed)
is left untouched rather than guessed at.
"""

from __future__ import annotations

import json
import tomllib
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

# Per-agent record of the value config_render last wrote at each dotted key —
# the baseline `refresh_agent_config` diffs an existing file's values against
# to tell "operator changed this" from "still whatever we generated".
_SNAPSHOT_FILENAME = ".arc-config-defaults.json"


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


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted-key -> leaf value, treating any list (including a table array
    parsed by ``tomllib``) as one opaque leaf rather than exploding it —
    comparing an append-only array like ``security.validators.approved``
    whole avoids a partial, order-sensitive refresh of it."""
    flat: dict[str, Any] = {}
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _plain(document: TOMLDocument) -> dict[str, Any]:
    """Plain-Python view of a tomlkit document — round-trips through
    ``tomllib`` so no tomlkit item type ever leaks into a JSON/equality
    comparison."""
    return tomllib.loads(tomlkit.dumps(document))


def _snapshot_path(agent_dir: Path) -> Path:
    return agent_dir / _SNAPSHOT_FILENAME


def _load_snapshot(agent_dir: Path) -> dict[str, Any]:
    path = _snapshot_path(agent_dir)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_config_snapshot(agent_dir: Path) -> None:
    """Record this agent's CURRENT arcagent.toml values as the refresh baseline.

    Called right after the file is written — at scaffold creation, and again
    after every ``refresh_agent_config`` — so the NEXT refresh can tell
    "operator changed this since" from "still whatever we last wrote here".
    An agent with no snapshot yet (built before this mechanism existed) is
    simply never refreshed until one is written; it keeps working exactly as
    ``sync_agent_config`` alone left it.
    """
    config_path = agent_dir / "arcagent.toml"
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    flat = _flatten(_plain(document))
    _snapshot_path(agent_dir).write_text(
        json.dumps(flat, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _set_dotted(document: TOMLDocument, dotted_key: str, value: Any) -> None:
    """Set ``value`` at a dotted path whose parent tables already exist."""
    *parents, leaf = dotted_key.split(".")
    node: Any = document
    for part in parents:
        node = node[part]
    node[leaf] = value


@dataclass(frozen=True)
class ConfigRefreshResult:
    """What one agent's file gained (new keys) or advanced (stale defaults)."""

    path: Path
    added: tuple[str, ...]
    refreshed: tuple[str, ...]
    skipped_no_baseline: tuple[str, ...]
    written: bool

    @property
    def changed(self) -> bool:
        return bool(self.added) or bool(self.refreshed)


def plan_config_refresh(agent_dir: Path) -> tuple[TOMLDocument, ConfigRefreshResult]:
    """The agent's document with new scaffold keys added AND stale, untouched
    defaults advanced to what the scaffold renders today.

    Two independent passes over the SAME fresh render: first the existing,
    already-correct additive merge (``_merge`` — a whole missing section or
    leaf is copied in wholesale, so nested tables are created properly);
    then a snapshot-diffed leaf refresh that only ever touches a key already
    known to exist before this call (a key the additive pass just added is
    already "fresh" and is skipped here).
    """
    config_path = agent_dir / "arcagent.toml"
    existing = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    existing_before_flat = _flatten(_plain(existing))
    snapshot = _load_snapshot(agent_dir)

    agent_table = existing.get("agent", {})
    identity_table = existing.get("identity", {})
    fresh_text = render_agent_config(
        name=str(agent_table.get("name", agent_dir.name)),
        tier=str(agent_table.get("tier", "personal")),
        did=str(identity_table.get("did", "")),
    )
    fresh_document = tomlkit.parse(fresh_text)

    added: list[str] = []
    for section, value in fresh_document.items():
        if section in _NEVER_SYNCED:
            continue
        if section not in existing:
            existing[section] = value
            added.append(section)
        elif _is_table(value) and _is_table(existing[section]):
            _merge(value, existing[section], f"{section}.", added)

    fresh_flat = _flatten(tomllib.loads(fresh_text))
    refreshed: list[str] = []
    skipped: list[str] = []
    for key, fresh_value in fresh_flat.items():
        top = key.split(".", 1)[0]
        if top in _NEVER_SYNCED:
            continue
        if any(key == entry or key.startswith(f"{entry}.") for entry in added):
            continue  # just added above — already today's value
        if key not in existing_before_flat:
            continue  # guarded by the additive pass; nothing to refresh
        if key not in snapshot:
            skipped.append(key)
            continue
        if existing_before_flat[key] == snapshot[key] and existing_before_flat[key] != fresh_value:
            _set_dotted(existing, key, fresh_value)
            refreshed.append(key)

    result = ConfigRefreshResult(
        path=config_path,
        added=tuple(added),
        refreshed=tuple(refreshed),
        skipped_no_baseline=tuple(skipped),
        written=False,
    )
    return existing, result


def refresh_agent_config(agent_dir: Path, *, dry_run: bool = False) -> ConfigRefreshResult:
    """Add missing scaffold settings AND advance untouched defaults.

    Writes a fresh snapshot after a real write so the NEXT refresh's baseline
    reflects whatever is now on disk — including a value an operator set that
    happens (this time) to equal the scaffold's default; it stays theirs.
    """
    merged, result = plan_config_refresh(agent_dir)
    write = result.changed and not dry_run
    if write:
        config_path = agent_dir / "arcagent.toml"
        config_path.write_text(tomlkit.dumps(merged), encoding="utf-8")
        write_config_snapshot(agent_dir)
    return ConfigRefreshResult(
        path=result.path,
        added=result.added,
        refreshed=result.refreshed,
        skipped_no_baseline=result.skipped_no_baseline,
        written=write,
    )


__all__ = [
    "ConfigRefreshResult",
    "ConfigSyncResult",
    "discover_agent_dirs",
    "plan_config_refresh",
    "plan_config_sync",
    "refresh_agent_config",
    "sync_agent_config",
    "write_config_snapshot",
]
