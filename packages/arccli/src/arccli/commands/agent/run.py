"""`arc agent run` — one-shot non-interactive task execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from arccli.commands.agent._common import (
    _load_arcagent,
    _load_env,
    _print_result_json,
    _resolve_agent_dir,
    _scaffold_workspace,
)


async def _agent_run_once(
    agent_dir: Path,
    task: str,
    model_override: str | None,
    context: str | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """One-shot task execution coroutine."""
    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    if model_override:
        config.llm.model = model_override

    if context is not None:
        context_path = agent_dir / "workspace" / "context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        source = Path(context)
        if source.is_file():
            context_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            context_path.write_text(context, encoding="utf-8")

    await arc_agent.startup()
    try:
        result = await arc_agent.run(task)

        if as_json:
            _print_result_json(result)
        else:
            if result.completion_payload:
                sys.stdout.write(json.dumps(result.completion_payload, indent=2) + "\n")
            elif result.content:
                sys.stdout.write(result.content + "\n")
            if verbose:
                sys.stdout.write(
                    f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                    f"${result.cost_usd:.4f}, strategy={result.strategy_used}]\n"
                )
    finally:
        await arc_agent.shutdown()


def _run(args: argparse.Namespace) -> None:
    """Run a task against an agent (non-interactive one-shot)."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    asyncio.run(
        _agent_run_once(
            agent_dir=agent_dir,
            task=args.task,
            model_override=getattr(args, "model", None),
            context=getattr(args, "context", None),
            verbose=getattr(args, "verbose", False),
            as_json=getattr(args, "as_json", False),
        )
    )
