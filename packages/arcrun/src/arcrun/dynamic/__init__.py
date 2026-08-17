"""Ad-hoc orchestration: the model writes a script, the engine runs it safely.

For a task with genuinely independent parts, one model call authors a short
script in a restricted Python subset; this package parses it against a
whitelisted grammar, dry-runs it against a stub host for zero tokens, then walks
it deterministically while its ``agent()`` and ``parallel()`` calls become
bounded child runs.

This is the **disposable** counterpart to ArcFlow. ArcFlow
(``arcteam.workflow``) is for permanent, reusable, operator-signed multi-agent
graphs authored ahead of time. A dynamic script is invented for one request and
thrown away. Neither replaces the other.

The modules split by concern:

* :mod:`~arcrun.dynamic.host` — the host boundary, and the whole list of effects
  a script can have.
* :mod:`~arcrun.dynamic.grammar` — what may be written. A security boundary.
* :mod:`~arcrun.dynamic.interpreter` — what happens when it runs.
* :mod:`~arcrun.dynamic.journal` — the append-only record that makes a run
  resumable by replay.
* :mod:`~arcrun.dynamic.validate` — the dry run that proves a script before it
  costs anything.
* :mod:`~arcrun.dynamic.binding` — the real host, over child ReAct runs.
"""

from __future__ import annotations

from arcrun.dynamic.binding import RunHost
from arcrun.dynamic.grammar import ScriptSyntaxError, parse_script
from arcrun.dynamic.host import (
    AgentOutcome,
    AgentSpec,
    BudgetState,
    ScriptHost,
    ScriptOutcome,
)
from arcrun.dynamic.interpreter import ScriptError, execute_script
from arcrun.dynamic.journal import Journal, JournalDivergence, JournalError
from arcrun.dynamic.validate import ValidationReport, dry_run

__all__ = [
    "AgentOutcome",
    "AgentSpec",
    "BudgetState",
    "Journal",
    "JournalDivergence",
    "JournalError",
    "RunHost",
    "ScriptError",
    "ScriptHost",
    "ScriptOutcome",
    "ScriptSyntaxError",
    "ValidationReport",
    "dry_run",
    "execute_script",
    "parse_script",
]
