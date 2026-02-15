"""Basic Agent — minimal example using ArcAgent.

Demonstrates:
- Loading config from arcagent.toml
- ArcAgent lifecycle (startup → run/chat → shutdown)
- Single-shot task execution
- Multi-turn chat with session persistence

Usage:
    # Single task
    python run.py "List all files in the workspace"

    # Interactive chat
    python run.py --chat
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from arcagent.core.agent import ArcAgent
from arcagent.core.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
_logger = logging.getLogger("basic_agent")


async def run_task(agent: ArcAgent, task: str) -> None:
    """Execute a single task and print the result."""
    result = await agent.run(task)
    content = getattr(result, "content", None) or str(result)
    print(f"\n{content}")


async def run_chat(agent: ArcAgent) -> None:
    """Interactive multi-turn chat loop."""
    print("Basic Agent (type 'exit' to quit)")
    print("-" * 40)

    session_id: str | None = None

    while True:
        try:
            message = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not message:
            continue
        if message.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break

        result = await agent.chat(message, session_id=session_id)
        content = getattr(result, "content", None) or str(result)
        print(f"\n{content}")

        # Capture session_id from first response for continuity
        if session_id is None and agent._session is not None:
            session_id = agent._session.session_id


async def main() -> None:
    parser = argparse.ArgumentParser(description="Basic ArcAgent example")
    parser.add_argument("task", nargs="?", help="Task to execute (single-shot mode)")
    parser.add_argument("--chat", action="store_true", help="Interactive chat mode")
    args = parser.parse_args()

    if not args.task and not args.chat:
        parser.print_help()
        sys.exit(1)

    # Load config relative to this file
    config_path = Path(__file__).parent / "arcagent.toml"
    config = load_config(config_path)

    agent = ArcAgent(config, config_path=config_path)
    await agent.startup()

    try:
        if args.chat:
            await run_chat(agent)
        else:
            await run_task(agent, args.task)
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
