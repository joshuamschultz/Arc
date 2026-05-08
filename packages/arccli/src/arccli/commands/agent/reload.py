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

    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = agent_dir / "arcagent.toml"
    config = load_config(config_path)
    arc_agent = ArcAgent(config, config_path=config_path)

    async def _do_reload() -> None:
        await arc_agent.startup()
        try:
            await arc_agent.reload()
            sys.stdout.write("Reload complete.\n")
            sys.stdout.write(f"  Skills:     {len(arc_agent.skills)}\n")
        finally:
            await arc_agent.shutdown()

    asyncio.run(_do_reload())
