"""`arc agent create` — scaffold a new agent directory."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

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

    # SPEC-026 FR-6 (AC-6.1): the agent ships with an [arcstore] block; create
    # the operational store data dir + spool idempotently so the call-now-see-later
    # guarantee holds from the very first run.
    _ensure_arcstore_dirs(agent_dir)

    sys.stdout.write(f"Created agent: {agent_dir}\n")
    _print_scaffold_summary(name, agent_dir)

    # FIX-1: Auto-register with arcteam. Without this, the agent serves and
    # emits traces to disk correctly but stays invisible to arcui's trace
    # dashboard. Workspace_path = agent_dir/workspace (the SUBDIRECTORY where
    # JSONLTraceStore expects to find traces/). Best-effort: a registration
    # failure logs a warning but does not fail the create.
    if not no_register:
        _try_auto_register(name, agent_dir)


def _ensure_arcstore_dirs(agent_dir: Path) -> None:
    """Create the resolved arcstore data dir + spool (idempotent, fail-open)."""
    try:
        from arccli.commands.agent._store_lifecycle import load_arcstore_config

        data_dir = load_arcstore_config(agent_dir).resolve_data_dir()
        (data_dir / "spool").mkdir(parents=True, exist_ok=True)
    except Exception:  # reason: fail-open — store setup must never fail create
        sys.stdout.write("Warning: could not pre-create arcstore data dir (non-fatal)\n")


def _mint_agent_identity(agent_dir: Path) -> Any:
    """Materialize the agent's real identity from its scaffolded config.

    ``AgentIdentity`` lives in arctrust; arccli reads it here and passes both
    the DID and Ed25519 verify key into arcteam registration (arcteam never
    fetches identity itself). ``from_config`` mints + persists the keypair,
    so the identity registered here is the SAME one the agent signs with at
    startup — the signed bus can therefore verify its messages.
    """
    from arcagent.core.config import load_config
    from arctrust import AgentIdentity

    config_path = agent_dir / "arcagent.toml"
    config = load_config(config_path)
    return AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=config_path,
    )


def _try_auto_register(name: str, agent_dir: Path) -> None:
    """Best-effort arcteam registration after scaffold. Idempotent."""
    try:
        from arcteam.config import TeamConfig
        from arcteam.types import Entity, EntityType

        from arccli.commands.team import _build_service

        identity = _mint_agent_identity(agent_dir)
        did = identity.did

        async def _do() -> None:
            root = TeamConfig().root
            _, registry, _, _ = await _build_service(root)
            entity = Entity(
                did=did,
                handle=name,
                id=f"agent://{name}",
                name=name,
                type=EntityType("agent"),
                public_key=identity.public_key.hex(),
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
                f"Registered with arcteam: {name} ({did})\n"
                f"  Workspace: {agent_dir / 'workspace'}\n"
            )

        asyncio.run(_do())
    except Exception as exc:  # reason: fail-open — continue
        sys.stdout.write(
            f"Warning: arcteam auto-register failed: {exc}\n"
            f"  Run manually: arc team register {name} --type agent "
            f"--roles executor --workspace {agent_dir}/workspace\n"
        )
