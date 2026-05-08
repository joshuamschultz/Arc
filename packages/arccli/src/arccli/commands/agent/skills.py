"""`arc agent skills` — list discovered skill folders across scan roots."""

from __future__ import annotations

import argparse

from arccli.commands.agent._common import (
    _capability_scan_roots,
    _iter_skill_folders,
    _print_table,
    _resolve_agent_dir,
)


def _skills(args: argparse.Namespace) -> None:
    """List discovered skill folders across all four scan roots."""
    import sys

    agent_dir = _resolve_agent_dir(args.path)
    folders = _iter_skill_folders(agent_dir)

    if not folders:
        sys.stdout.write("No skills found.\n")
        for root_name, root in _capability_scan_roots(agent_dir):
            sys.stdout.write(f"  {root_name}: {root}\n")
        return

    try:
        from arcagent.core.skill_validator import validate_skill_folder
    except ImportError:
        validate_skill_folder = None  # type: ignore[assignment]  # reason: optional import — when arcagent isn't on the path the function falls back to None and the caller skips validation

    rows: list[list[str]] = []
    for root_name, folder in folders:
        name = folder.name
        version = ""
        description = ""
        if validate_skill_folder is not None:
            result = validate_skill_folder(folder, root_name)
            if result.entry is not None:
                name = result.entry.name
                version = result.entry.version
                description = result.entry.description
        if len(description) > 50:
            description = description[:47] + "..."
        rows.append([name, version, root_name, description])

    _print_table(["Name", "Version", "Source", "Description"], rows)
