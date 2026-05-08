"""`arc agent serve` — long-running agent daemon."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from arccli.commands.agent._common import (
    _load_arcagent,
    _load_env,
    _resolve_agent_dir,
    _scaffold_workspace,
)


async def _serve_daemon(
    agent_dir: Path,
    shutdown_event: asyncio.Event,
    verbose: bool,
) -> None:
    """Async serve coroutine — startup, wait for shutdown, cleanup."""
    import logging

    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    # Route logs to stderr so systemd/supervisord captures them.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
    )
    logging.getLogger("arcagent").setLevel(logging.INFO)
    logging.getLogger("arcagent.audit").setLevel(logging.WARNING)
    logging.getLogger("arcagent.tool_registry").setLevel(logging.WARNING)
    if verbose:
        logging.getLogger("arcagent").setLevel(logging.DEBUG)
        logging.getLogger("arcagent.audit").setLevel(logging.INFO)
        logging.getLogger("arcagent.tool_registry").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.INFO)

    await arc_agent.startup()
    agent_name = config.agent.name
    sys.stdout.write(f"Serving agent: {agent_name}\n")
    sys.stdout.write("Scheduler active. Press Ctrl+C to stop.\n")
    sys.stdout.write("-" * 40 + "\n")
    sys.stdout.flush()

    try:
        await shutdown_event.wait()
    finally:
        sys.stdout.write("\nShutting down...\n")
        sys.stdout.flush()
        await arc_agent.shutdown()
        sys.stdout.write("Done.\n")
        sys.stdout.flush()


def _serve(args: argparse.Namespace) -> None:
    """Start a long-running agent daemon."""
    import signal

    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    verbose = getattr(args, "verbose", False)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    try:
        loop.run_until_complete(_serve_daemon(agent_dir, shutdown_event, verbose))
    finally:
        loop.close()
