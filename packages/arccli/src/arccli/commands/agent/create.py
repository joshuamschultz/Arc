"""`arc agent create` — scaffold a new agent directory.

The agent itself is built by :func:`arcagent.scaffold.create_agent`, the same
function the dashboard's "New agent" uses, so the terminal and the browser make
identical agents. This module only resolves the CLI's operator custody, prints,
and registers the agent with the fleet.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import arcagent
import arcllm

from arccli.commands.agent._common import _print_scaffold_summary, cli_operator_signing


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new agent directory with example tools."""
    name: str = args.name
    tier: str = getattr(args, "tier", "personal")
    operator = cli_operator_signing(bootstrap=True)
    if operator is None:
        # Fail open, as before: the scaffold still succeeds; its identity.md and
        # calculator stay unsigned (refused at run start) until signed later.
        sys.stdout.write(
            "Warning: operator key unavailable; identity.md and capabilities are unsigned.\n"
        )
    try:
        created = arcagent.scaffold.create_agent(
            Path(getattr(args, "parent_dir", ".")),
            name,
            tier=tier,
            model=getattr(args, "model", arcagent.scaffold.DEFAULT_MODEL),
            llm_module_surface=arcllm.commented_module_surface(prefix="llm."),
            operator=operator,
        )
    except (arcagent.scaffold.AgentNameError, arcagent.scaffold.AgentExistsError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)

    sys.stdout.write(f"Created agent: {created.agent_dir}\n")
    _print_scaffold_summary(name, created.agent_dir, tier)

    # FIX-1: Auto-register with arcteam. Without this, the agent serves and
    # emits traces to disk correctly but stays invisible to arcui's trace
    # dashboard. Best-effort: a registration failure warns but does not fail.
    if not getattr(args, "no_register", False):
        _try_auto_register(created)


def _try_auto_register(created: arcagent.scaffold.CreatedAgent) -> None:
    """Best-effort arcteam registration after scaffold. Idempotent."""
    try:
        from arcteam.config import TeamConfig
        from arcteam.registry import register_native_agent

        from arccli.commands.team import _build_service, _shutdown

        workspace = created.agent_dir / "workspace"

        async def _do() -> bool:
            _, registry, _, backend = await _build_service(TeamConfig().root)
            try:
                return await register_native_agent(
                    registry,
                    name=created.name,
                    did=created.did,
                    public_key_hex=created.public_key_hex,
                    workspace_path=str(workspace),
                )
            finally:
                await _shutdown(backend)

        if asyncio.run(_do()):
            sys.stdout.write(
                f"Registered with arcteam: {created.name} ({created.did})\n"
                f"  Workspace: {workspace}\n"
            )
        else:
            sys.stdout.write(f"  arcteam: {created.name} already registered (ok)\n")
    except Exception as exc:  # reason: fail-open — the agent exists; registration can retry
        sys.stdout.write(
            f"Warning: arcteam auto-register failed: {exc}\n"
            f"  Run manually: arc team register {created.name} --type agent "
            f"--roles executor --workspace {created.agent_dir}/workspace\n"
        )
