"""`arc agent config` — show, or sync, agent configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arccli.commands.agent._common import _load_agent_config, _resolve_agent_dir
from arccli.commands.agent._config_sync import (
    ConfigRefreshResult,
    ConfigSyncResult,
    discover_agent_dirs,
    refresh_agent_config,
    sync_agent_config,
)


def _config(args: argparse.Namespace) -> None:
    """Show agent configuration, or sync/refresh it against the current scaffold."""
    if getattr(args, "refresh_defaults", False):
        _refresh(args)
        return
    if getattr(args, "sync", False):
        _sync(args)
        return

    agent_dir = _resolve_agent_dir(args.path)
    config = _load_agent_config(agent_dir)

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(config, indent=2) + "\n")
        return

    for section, values in config.items():
        sys.stdout.write(f"[{section}]\n")
        if isinstance(values, dict):
            for key, val in values.items():
                sys.stdout.write(f"  {key} = {val}\n")
        else:
            sys.stdout.write(f"  {values}\n")
        sys.stdout.write("\n")


def _sync(args: argparse.Namespace) -> None:
    """Add every setting the current scaffold declares and the file lacks."""
    dry_run = bool(getattr(args, "dry_run", False))
    agent_dirs = _sync_targets(args)
    if not agent_dirs:
        sys.stderr.write("error: no agent directory with an arcagent.toml found\n")
        raise SystemExit(1)

    results = [sync_agent_config(agent_dir, dry_run=dry_run) for agent_dir in agent_dirs]
    for result in results:
        _report(result, dry_run=dry_run)

    added = [key for result in results for key in result.added]
    # A whole new module block turns on behavior this agent did not have before;
    # a new leaf only exposes a default it was already running. Count them apart
    # so an operator reading the summary can tell which happened.
    new_modules = [key for key in added if key.count(".") == 1 and key.startswith("modules.")]
    verb = "would add" if dry_run else "added"
    sys.stdout.write(
        f"{verb} {len(new_modules)} module(s) and "
        f"{len(added) - len(new_modules)} setting(s) across {len(results)} agent(s)\n"
    )


def _refresh(args: argparse.Namespace) -> None:
    """Add missing scaffold settings AND advance untouched defaults (H-039)."""
    dry_run = bool(getattr(args, "dry_run", False))
    agent_dirs = _sync_targets(args)
    if not agent_dirs:
        sys.stderr.write("error: no agent directory with an arcagent.toml found\n")
        raise SystemExit(1)

    results = [refresh_agent_config(agent_dir, dry_run=dry_run) for agent_dir in agent_dirs]
    for result in results:
        _report_refresh(result, dry_run=dry_run)

    added = sum(len(r.added) for r in results)
    refreshed = sum(len(r.refreshed) for r in results)
    verb = "would advance" if dry_run else "advanced"
    sys.stdout.write(
        f"added {added} new setting(s) and {verb} {refreshed} stale default(s) "
        f"across {len(results)} agent(s)\n"
    )


def _report_refresh(result: ConfigRefreshResult, *, dry_run: bool) -> None:
    name = result.path.parent.name
    if not result.changed:
        sys.stdout.write(f"  = {name}: already current\n")
        return
    marker = "?" if dry_run else "+"
    sys.stdout.write(
        f"  {marker} {name}: {len(result.added)} new, {len(result.refreshed)} refreshed\n"
    )
    for key in result.added:
        sys.stdout.write(f"      + {key}\n")
    for key in result.refreshed:
        sys.stdout.write(f"      ~ {key}\n")


def _sync_targets(args: argparse.Namespace) -> list[Path]:
    team_root = getattr(args, "team_root", None)
    if team_root:
        return discover_agent_dirs(Path(team_root).expanduser().resolve())
    agent_dir = _resolve_agent_dir(args.path)
    return [agent_dir] if (agent_dir / "arcagent.toml").is_file() else []


def _report(result: ConfigSyncResult, *, dry_run: bool) -> None:
    name = result.path.parent.name
    if not result.changed:
        sys.stdout.write(f"  = {name}: already complete\n")
        return
    marker = "?" if dry_run else "+"
    sys.stdout.write(f"  {marker} {name}: {len(result.added)} setting(s)\n")
    for key in result.added:
        sys.stdout.write(f"      {key}\n")
