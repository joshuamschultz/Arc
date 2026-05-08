"""`arc agent` subcommand subpackage.

The original 1546-LOC ``arccli.commands.agent`` module was decomposed
into focused subcommand modules + a shared ``_common`` helper module +
the ``_dispatch`` argparse wiring. This package preserves the public
import surface so callers continue to do
``from arccli.commands.agent import agent_handler`` without modification.

Subcommand modules
------------------
- ``create``     — scaffold a new agent directory (+ best-effort
  arcteam registration).
- ``status``     — config / workspace / tools / capabilities / sessions
  summary.
- ``skills``     — discovered skill folders across scan roots.
- ``extensions`` — capability `.py` files across scan roots.
- ``sessions``   — session transcripts.
- ``build``      — interactive wizard (TTY) or ``--check`` validation.
- ``tools``      — list tools available to an agent.
- ``config``     — show TOML config (text or JSON).
- ``reload``     — hot-reload extensions + skills.
- ``strategies`` — list arcrun strategies.
- ``events``     — list event types.
- ``run``        — one-shot non-interactive task execution.
- ``serve``      — long-running agent daemon.
- ``chat``       — interactive REPL (or one-shot via ``--task``).

Internal modules
----------------
- ``_common``    — shared helpers (env / config / scaffold / output
  formatting / capability scan roots / ArcAgent loader).
- ``_dispatch``  — ``_build_parser``, ``_SUBCOMMAND_MAP``, and the
  public ``agent_handler`` entry point.
"""

from __future__ import annotations

# Re-export selected internal helpers used by other arccli modules
# (e.g. ``arccli.telegram_setup`` imports ``_resolve_agent_dir``).
from arccli.commands.agent._common import (
    _CALCULATOR_TOOL,
    _DEFAULT_CONFIG,
    _GLOBAL_CAP_DIR,
    _capability_scan_roots,
    _discover_tools,
    _iter_capability_files,
    _iter_skill_folders,
    _load_agent_config,
    _load_arcagent,
    _load_env,
    _print_kv,
    _print_result_json,
    _print_scaffold_summary,
    _print_table,
    _resolve_agent_dir,
    _scaffold_workspace,
)

# Public dispatch entry — only callers outside this subpackage use this.
from arccli.commands.agent._dispatch import agent_handler

__all__ = [
    "_CALCULATOR_TOOL",
    "_DEFAULT_CONFIG",
    "_GLOBAL_CAP_DIR",
    "_capability_scan_roots",
    "_discover_tools",
    "_iter_capability_files",
    "_iter_skill_folders",
    "_load_agent_config",
    "_load_arcagent",
    "_load_env",
    "_print_kv",
    "_print_result_json",
    "_print_scaffold_summary",
    "_print_table",
    "_resolve_agent_dir",
    "_scaffold_workspace",
    "agent_handler",
]
