"""`arc agent skills` — list discovered skills across all scan roots, with verdicts."""

from __future__ import annotations

import argparse
import asyncio
import sys

from arccli.commands._shared import print_table as _print_table
from arccli.commands.agent._common import _resolve_agent_dir

_DESCRIPTION_MAX = 50


def _skills(args: argparse.Namespace) -> None:
    """List every skill the agent would actually load, with its verdict.

    Task #39 fold-in: previously used `_iter_skill_folders` (agent/global/
    workspace roots ONLY — explicitly excludes package builtins, and looks
    directly under each root rather than its `skills/` subdir where
    `create_skill` actually writes) plus a bare `validate_skill_folder()`
    call with no status surfaced. That's why `arc agent skills` showed 1
    skill with no status column while arcui's capability view — backed by
    the same seam this now uses — correctly showed the real count with
    verdicts.

    `collect_agent_capability_inventory` is the single read-only inventory
    seam arcui already uses (SPEC arcui-reality-mirror COMP-007) and the
    one `arc agent tools`/`arc ext inspect` share via
    `build_capability_registry` (task #29) — reusing it here means the
    CLI, arcui, and a real agent load all agree on what skills exist and
    why each one did or didn't load.
    """
    from arcagent.capabilities.inventory import collect_agent_capability_inventory

    agent_dir = _resolve_agent_dir(args.path)
    config_path = agent_dir / "arcagent.toml"
    if not config_path.is_file():
        sys.stderr.write(f"arc agent: no arcagent.toml in {agent_dir}\n")
        sys.exit(1)

    try:
        inventory = asyncio.run(collect_agent_capability_inventory(config_path))
    except Exception as exc:  # reason: a listing command must degrade, not crash
        sys.stderr.write(f"arc agent skills: could not enumerate skills: {exc}\n")
        sys.exit(1)

    skills = [item for item in inventory.items if item.kind == "skill"]
    if not skills:
        sys.stdout.write("No skills found.\n")
        return

    rows: list[list[str]] = []
    for item in sorted(skills, key=lambda s: s.name):
        description = item.description
        if len(description) > _DESCRIPTION_MAX:
            description = description[: _DESCRIPTION_MAX - 3] + "..."
        rows.append([item.name, item.version, item.source_root, item.status, description])

    _print_table(["Name", "Version", "Source", "Status", "Description"], rows)
