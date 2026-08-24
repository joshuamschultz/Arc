"""`arc agent reload` — hot-reload extensions and skills."""

from __future__ import annotations

import argparse
import asyncio
import sys

from arccli.commands.agent._common import _load_env, _resolve_agent_dir


def _reload(args: argparse.Namespace) -> None:
    """Hot-reload extensions and skills for an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)

    import arcagent

    config_path = agent_dir / "arcagent.toml"
    config = arcagent.load_config(config_path)
    # An agent addressed from the CLI is still part of whatever fleet it belongs
    # to: without this it starts with no directory and no inbox, and every
    # fleet-facing tool reports itself unavailable on that path alone.
    from arcteam.agent_fleet import ArcTeamFleet

    arc_agent = arcagent.ArcAgent(
        config, config_path=config_path, fleet=ArcTeamFleet()
    )

    async def _do_reload() -> None:
        await arc_agent.startup()
        try:
            await arc_agent.reload()
            sys.stdout.write("Reload complete.\n")
            sys.stdout.write(f"  Skills:     {len(arc_agent.skills)}\n")
        finally:
            await arc_agent.shutdown()

    asyncio.run(_do_reload())
