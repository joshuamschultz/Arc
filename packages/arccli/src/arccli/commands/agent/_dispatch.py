"""Argparse dispatch for the `arc agent` subcommand group.

Subcommand dispatch is argparse-based so the top-level
`arc agent <sub> [args]` contract is preserved exactly.

Module CLIs (bio-memory, memory, policy, browser) are Click groups
delegated via ``_run_module_cli`` before argparse parsing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arccli.commands.agent.build import _build
from arccli.commands.agent.chat import _chat
from arccli.commands.agent.config import _config
from arccli.commands.agent.create import _create
from arccli.commands.agent.events import _events
from arccli.commands.agent.extensions import _extensions
from arccli.commands.agent.reload import _reload
from arccli.commands.agent.run import _run
from arccli.commands.agent.serve import _serve
from arccli.commands.agent.sessions import _sessions
from arccli.commands.agent.skills import _skills
from arccli.commands.agent.status import _status
from arccli.commands.agent.strategies import _strategies
from arccli.commands.agent.tools import _tools


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc agent <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc agent",
        description="Agent management — create, build, chat with agents.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # create
    p = subs.add_parser("create", help="Scaffold a new agent directory.")
    p.add_argument("name", help="Agent name.")
    p.add_argument("--dir", dest="parent_dir", default=".", help="Parent directory (default: .)")
    p.add_argument(
        "--model",
        default="anthropic/claude-sonnet-4-5-20250929",
        help="LLM model.",
    )
    p.add_argument("--with-code-exec", dest="with_code_exec", action="store_true")
    p.add_argument(
        "--no-register",
        dest="no_register",
        action="store_true",
        help="Skip auto-registration with arcteam (agent will not appear in arcui).",
    )

    # status
    p = subs.add_parser("status", help="Show agent status summary.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # skills
    p = subs.add_parser("skills", help="List agent skills.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # extensions
    p = subs.add_parser("extensions", help="List agent extensions.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # sessions
    p = subs.add_parser("sessions", help="List agent sessions.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # build
    p = subs.add_parser("build", help="Interactive build / validate agent setup.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--check", action="store_true", help="Validate only; skip interactive setup.")

    # tools
    p = subs.add_parser("tools", help="List agent tools.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--json", dest="json", action="store_true", help="Output as JSON.")
    p.add_argument("--with-code-exec", dest="with_code_exec", action="store_true")

    # config
    p = subs.add_parser("config", help="Show agent configuration.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--json", dest="json", action="store_true", help="Output as JSON.")

    # reload
    p = subs.add_parser("reload", help="Hot-reload extensions and skills.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")

    # strategies
    subs.add_parser("strategies", help="List available execution strategies.")

    # events
    subs.add_parser("events", help="List all event types.")

    # run
    p = subs.add_parser("run", help="Run a task against an agent.")
    p.add_argument("path", help="Agent directory.")
    p.add_argument("task", help="Task to run.")
    p.add_argument("--model", default=None, help="Override model (provider/model).")
    p.add_argument(
        "--context",
        default=None,
        help="Stage context: literal text or path to a file. "
        "Written to workspace/context.md before the agent starts.",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Output full result as JSON (includes completion_payload).",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--max-turns", dest="max_turns", type=int, default=None)

    # serve
    p = subs.add_parser("serve", help="Start agent daemon.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--verbose", "-v", action="store_true")

    # chat
    p = subs.add_parser("chat", help="Interactive chat session.")
    p.add_argument("path", nargs="?", default=".", help="Agent directory (default: .)")
    p.add_argument("--task", "-t", default=None, help="One-shot task instead of REPL.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--model", default=None, help="Override model (provider/model).")
    p.add_argument("--max-turns", dest="max_turns", type=int, default=10)
    p.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Resume a specific session.",
    )

    return parser


_SUBCOMMAND_MAP = {
    "create": _create,
    "status": _status,
    "skills": _skills,
    "extensions": _extensions,
    "sessions": _sessions,
    "build": _build,
    "tools": _tools,
    "config": _config,
    "reload": _reload,
    "strategies": _strategies,
    "events": _events,
    "run": _run,
    "serve": _serve,
    "chat": _chat,
}


_MODULE_CLI_MAP = {
    "bio-memory": "arcagent.modules.bio_memory.cli",
    "memory": "arcagent.modules.memory.cli",
    "policy": "arcagent.modules.policy.cli",
    "browser": "arcagent.modules.browser.cli",
}


def _run_module_cli(module_import: str, args: list[str]) -> None:
    """Delegate to a Click-based module CLI group.

    Usage: ``arc agent <module-name> <path> [subcommands...]``
    The first positional arg is the agent directory; remaining args
    are forwarded to the Click group.
    """
    import importlib

    if not args:
        sys.stderr.write(f"Usage: arc agent {module_import.split('.')[-2]} <path> [subcommand]\n")
        sys.exit(1)

    agent_dir = Path(args[0]).resolve()
    workspace = agent_dir / "workspace"
    remaining = args[1:]

    mod = importlib.import_module(module_import)
    group = mod.cli_group(workspace)
    group(remaining, standalone_mode=True)


def agent_handler(args: list[str]) -> None:
    """Top-level handler for `arc agent <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc agent ...`.
    Module CLIs (bio-memory, memory, policy, browser) are delegated to
    their Click groups before argparse parsing.
    """
    if args and args[0] in _MODULE_CLI_MAP:
        _run_module_cli(_MODULE_CLI_MAP[args[0]], args[1:])
        return

    parser = _build_parser()

    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = _SUBCOMMAND_MAP.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"arc agent: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
