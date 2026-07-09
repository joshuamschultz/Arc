"""`arc agent chat` — interactive REPL chat session."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from arcrun import collect

from arccli.commands.agent._common import (
    _iter_capability_files,
    _load_arcagent,
    _load_env,
    _resolve_agent_dir,
    _scaffold_workspace,
)
from arccli.commands.agent.run import _agent_run_once, _default_session_id


async def _chat_interactive(
    agent_dir: Path,
    model_override: str | None,
    max_turns: int,
    verbose: bool,
    session_id: str | None,
) -> None:
    """Interactive REPL chat coroutine via ArcAgent."""
    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    if model_override:
        config.llm.model = model_override

    await arc_agent.startup()

    agent_name = config.agent.name
    model_id = config.llm.model

    sys.stdout.write(f"Agent: {agent_name}  |  Model: {model_id}\n")
    sys.stdout.write(f"Skills: {len(arc_agent.skills)}\n")
    sys.stdout.write("\n")
    sys.stdout.write("Commands:\n")
    sys.stdout.write("  /quit              Exit\n")
    sys.stdout.write("  /tools             List tools\n")
    sys.stdout.write("  /model             Show model\n")
    sys.stdout.write("  /cost              Session cost\n")
    sys.stdout.write("  /reload            Hot-reload extensions and skills\n")
    sys.stdout.write("  /skills            List available skills\n")
    sys.stdout.write("  /extensions        List loaded extensions\n")
    sys.stdout.write("  /session           Show current session info\n")
    sys.stdout.write("  /sessions          List all sessions\n")
    sys.stdout.write("  /identity          Show agent DID and identity\n")
    sys.stdout.write("  /status            Show agent status summary\n")
    sys.stdout.write("-" * 60 + "\n")
    sys.stdout.flush()

    total_cost = 0.0
    total_turns = 0
    total_tool_calls = 0
    # A deterministic default key so the REPL has a stable session to resume.
    current_session_id = session_id or "cli:chat"

    try:
        while True:
            try:
                sys.stdout.write("\nyou> ")
                sys.stdout.flush()
                user_input = input().strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                break

            if not user_input:
                continue

            if user_input == "/quit":
                break

            if user_input == "/tools":
                if arc_agent._tool_registry is not None:
                    tools = arc_agent._tool_registry.to_arcrun_tools()
                    for t in tools:
                        sys.stdout.write(f"  {t.name}: {t.description}\n")
                else:
                    sys.stdout.write("  Tool registry not initialized.\n")
                continue

            if user_input == "/model":
                sys.stdout.write(f"  {config.llm.model}\n")
                continue

            if user_input == "/cost":
                sys.stdout.write(
                    f"  Session: ${total_cost:.4f} "
                    f"({total_turns} turns, {total_tool_calls} tool calls)\n"
                )
                continue

            if user_input == "/reload":
                await arc_agent.reload()
                sys.stdout.write(f"  Reloaded. Skills: {len(arc_agent.skills)}\n")
                continue

            if user_input == "/skills":
                skills = arc_agent.skills
                if not skills:
                    sys.stdout.write("  No skills loaded.\n")
                else:
                    for s in skills:
                        sys.stdout.write(f"  {s.name}: {s.description}\n")
                continue

            if user_input == "/extensions":
                files = _iter_capability_files(agent_dir)
                if not files:
                    sys.stdout.write("  No capability files found.\n")
                else:
                    for source, py_file in files:
                        sys.stdout.write(f"  {py_file.stem} ({source})\n")
                continue

            if user_input == "/session":
                sm = arc_agent._sessions.get(current_session_id)
                if sm is not None:
                    sys.stdout.write(f"  Session ID: {sm.session_id}\n")
                    sys.stdout.write(f"  Messages:   {sm.message_count}\n")
                else:
                    sys.stdout.write(f"  Session '{current_session_id}' not started yet.\n")
                continue

            if user_input == "/sessions":
                sessions_dir = agent_dir / "workspace" / "sessions"
                if not sessions_dir.is_dir():
                    sys.stdout.write("  No sessions directory.\n")
                else:
                    session_files = sorted(
                        sessions_dir.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    if not session_files:
                        sys.stdout.write("  No sessions.\n")
                    else:
                        for sf in session_files[:10]:
                            mtime = datetime.fromtimestamp(sf.stat().st_mtime, tz=UTC)
                            line_count = sum(1 for _ in open(sf))
                            sys.stdout.write(
                                f"  {sf.stem}  "
                                f"({mtime.strftime('%Y-%m-%d %H:%M')}, "
                                f"{line_count} msgs)\n"
                            )
                continue

            if user_input.startswith("/switch"):
                arg = user_input[len("/switch") :].strip()
                if arg:
                    current_session_id = arg
                    sys.stdout.write(f"  Switched to session: {arg}\n")
                else:
                    sys.stdout.write("  Usage: /switch <session-id>\n")
                continue

            if user_input == "/identity":
                if arc_agent._identity is not None:
                    sys.stdout.write(f"  DID: {arc_agent._identity.did}\n")
                    sys.stdout.write(f"  Can sign: {arc_agent._identity.can_sign}\n")
                else:
                    sys.stdout.write("  Identity not initialized.\n")
                continue

            if user_input == "/status":
                sys.stdout.write(f"  Agent:      {arc_agent._config.agent.name}\n")
                sys.stdout.write(f"  Model:      {arc_agent._config.llm.model}\n")
                if arc_agent._identity:
                    sys.stdout.write(f"  DID:        {arc_agent._identity.did}\n")
                sys.stdout.write(f"  Skills:     {len(arc_agent.skills)}\n")
                sys.stdout.write(f"  Cost:       ${total_cost:.4f}\n")
                sys.stdout.write(f"  Turns:      {total_turns}\n")
                sys.stdout.write(f"  Tool calls: {total_tool_calls}\n")
                continue

            if user_input.startswith("/"):
                sys.stdout.write(f"  Unknown command: {user_input}\n")
                continue

            # Execute task via the one streaming entry, collected to a result.
            try:
                session = await arc_agent.session(current_session_id)
                result = await collect(arc_agent.run(user_input, session=session))

                total_cost += result.cost_usd
                total_turns += result.turns
                total_tool_calls += result.tool_calls_made

                sys.stdout.write("\n")
                if result.content:
                    sys.stdout.write(result.content + "\n")

                if verbose:
                    sys.stdout.write(
                        f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                        f"${result.cost_usd:.4f}]\n"
                    )
                sys.stdout.flush()
            except Exception as e:  # reason: fail-open — continue
                sys.stdout.write(f"\nError: {e}\n")

    finally:
        await arc_agent.shutdown()

    sys.stdout.write(
        f"\nSession: ${total_cost:.4f} total "
        f"({total_turns} turns, {total_tool_calls} tool calls)\n"
    )


def _chat(args: argparse.Namespace) -> None:
    """Start interactive chat session with an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    task = getattr(args, "task", None)
    verbose = getattr(args, "verbose", False)
    session_id = getattr(args, "session_id", None)

    if task:
        # One-shot mode: reuse _agent_run_once
        asyncio.run(
            _agent_run_once(
                agent_dir=agent_dir,
                task=task,
                model_override=getattr(args, "model", None),
                context=getattr(args, "context", None),
                verbose=verbose,
                as_json=False,
                session_id=session_id or _default_session_id(),
            )
        )
    else:
        # Interactive REPL mode
        asyncio.run(
            _chat_interactive(
                agent_dir=agent_dir,
                model_override=getattr(args, "model", None),
                max_turns=getattr(args, "max_turns", 10),
                verbose=verbose,
                session_id=session_id,
            )
        )
