"""`arc agent create` — scaffold a new agent directory."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from arccli.commands.agent._common import (
    _CALCULATOR_TOOL,
    _DEFAULT_CONFIG,
    _print_scaffold_summary,
    _scaffold_workspace,
)


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new agent directory with example tools."""
    name: str = args.name
    parent_dir: str = getattr(args, "parent_dir", ".")
    model: str = getattr(args, "model", "anthropic/claude-sonnet-4-5-20250929")
    no_register: bool = getattr(args, "no_register", False)

    parent = Path(parent_dir).expanduser().resolve()
    agent_dir = parent / name

    if agent_dir.exists():
        sys.stderr.write(f"Error: Directory already exists: {agent_dir}\n")
        sys.exit(1)

    agent_dir.mkdir(parents=True)

    config_content = _DEFAULT_CONFIG.format(name=name)
    if model != "anthropic/claude-sonnet-4-5-20250929":
        config_content = config_content.replace(
            'model = "anthropic/claude-sonnet-4-5-20250929"',
            f'model = "{model}"',
        )
    (agent_dir / "arcagent.toml").write_text(config_content)

    _scaffold_workspace(agent_dir, name)

    calc_path = agent_dir / "capabilities" / "calculator.py"
    calc_path.write_text(_CALCULATOR_TOOL)

    sys.stdout.write(f"Created agent: {agent_dir}\n")
    _print_scaffold_summary(name, agent_dir)

    # FIX-1: Auto-register with arcteam. Without this, the agent serves and
    # emits traces to disk correctly but stays invisible to arcui's trace
    # dashboard. Workspace_path = agent_dir/workspace (the SUBDIRECTORY where
    # JSONLTraceStore expects to find traces/). Best-effort: a registration
    # failure logs a warning but does not fail the create.
    if not no_register:
        _try_auto_register(name, agent_dir)


def _try_auto_register(name: str, agent_dir: Path) -> None:
    """Best-effort arcteam registration after scaffold. Idempotent."""
    try:
        from arcteam.config import TeamConfig
        from arcteam.types import Entity, EntityType

        from arccli.commands.team import _build_service

        async def _do() -> None:
            root = TeamConfig().root
            _, registry, _, _ = await _build_service(root)
            entity = Entity(
                id=name,
                name=name,
                type=EntityType("agent"),
                roles=["executor"],
                workspace_path=str(agent_dir / "workspace"),
            )
            try:
                await registry.register(entity)
            except ValueError as exc:
                if "already registered" not in str(exc).lower():
                    raise
                sys.stdout.write(f"  arcteam: {name} already registered (ok)\n")
                return
            sys.stdout.write(
                f"Registered with arcteam: {name}\n  Workspace: {agent_dir / 'workspace'}\n"
            )

        asyncio.run(_do())
    except Exception as exc:  # reason: fail-open — continue
        sys.stdout.write(
            f"Warning: arcteam auto-register failed: {exc}\n"
            f"  Run manually: arc team register {name} --type agent "
            f"--roles executor --workspace {agent_dir}/workspace\n"
        )
