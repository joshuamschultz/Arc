"""The environment an extension's child process is given, and the names it may not carry.

Three places start a process on an extension's behalf — the launcher behind an MCP
server, the CLI attachment behind a declared binary, and the sign-in check behind
``verify_command`` — and each hands the child an environment that a manifest can
contribute to. That contribution is the whole point of ``[secrets.placement]``: a
bundle says which variable its own tool reads a credential from, and Arc puts the
value there.

Which is exactly why the refusal list lives here rather than beside any one caller.
A variable can do two very different things to a child:

* **carry a value** — what a placement is for;
* **steer the process** — ``PATH`` chooses which binary runs at all, and the loader
  (``LD_*``, ``DYLD_*``) and interpreter-startup families (``PYTHONSTARTUP``,
  ``NODE_OPTIONS``) execute attacker-chosen code inside it before the extension's own
  entry point.

:func:`scrubbed_environment` drops the second family from whatever it is handed, and
drops it *after* the merge so a bundle cannot smuggle one back through its own table.
But dropping it silently is not enough for a placement: a manifest naming ``LD_PRELOAD``
would parse, deliver nothing, and let every surface report a credential as placed.
:func:`refuses_placement` is the same knowledge read at parse time, so the manifest is
refused instead — one list, two readings, and no way for them to drift apart.

``PATH`` is refused for placement and deliberately NOT scrubbed: a child that inherits
no ``PATH`` cannot find anything, and the risk is a *manifest-chosen* value, not the
operator's own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Dynamic-loader families. Matched by prefix rather than by name: the loader reads a
#: whole family, and a fixed list goes stale the moment a new member is added.
LOADER_PREFIXES = ("LD_", "DYLD_")

#: Interpreter-startup hooks — each one executes attacker-chosen code before the
#: extension's own entry point runs.
INTERPRETER_VARIABLES = frozenset({"PYTHONSTARTUP", "NODE_OPTIONS"})

#: Variables that decide which program runs. Inherited from the operator's own
#: environment, never accepted from a manifest.
PROCESS_STEERING_VARIABLES = frozenset({"PATH"})


def scrubbed_environment(declared: Mapping[str, str]) -> dict[str, str]:
    """Build the child's environment: inherit, merge the declared entries, then scrub.

    The scrub runs last on purpose (REQ-273). Scrubbing the inherited environment and
    then merging a manifest's own table would let a bundle reintroduce exactly the
    variable that was just removed.

    Args:
        declared: The variables the manifest asked for, including any credential a
            ``[secrets.placement]`` puts there.

    Returns:
        The environment the child is actually given — never inherited implicitly, so a
        variable that survives here is one this function chose to keep.
    """
    merged = {**os.environ, **declared}
    return {name: value for name, value in merged.items() if not is_scrubbed(name)}


def is_scrubbed(name: str) -> bool:
    """True for a loader or interpreter-startup variable."""
    return name.startswith(LOADER_PREFIXES) or name in INTERPRETER_VARIABLES


def refuses_placement(name: str) -> bool:
    """True when a manifest must not be allowed to place a credential under ``name``.

    Either the variable would be scrubbed on the way to the child — a placement that
    reports success and delivers nothing — or it decides which program runs.
    """
    return is_scrubbed(name) or name in PROCESS_STEERING_VARIABLES


__all__ = [
    "INTERPRETER_VARIABLES",
    "LOADER_PREFIXES",
    "PROCESS_STEERING_VARIABLES",
    "is_scrubbed",
    "refuses_placement",
    "scrubbed_environment",
]
