"""The one rule for every operator-chosen name that addresses a connector.

A connection name is not a label. It becomes a TOML key in the deployment's
``connections.toml`` (``[connections.<name>]``), a segment of the env-var key the
local secret backend writes (``ARC_SECRET_<connection>_<field>``), and a vault
path segment. An agent name granted a connection is matched against the agent's
directory name. One rule for all of them, living here rather than restated at
each: a second copy is a rule that drifts, and the destination it drifts away
from is the one that breaks.

**Why this is stricter than TOML.** A bare TOML key permits far more than this —
``personal-mail`` parses perfectly well, and connections carrying hyphenated
names exist and work. The narrower rule is set by the env-var key, not by TOML: a
POSIX shell variable name is ``[A-Za-z_][A-Za-z0-9_]*``, so a hyphen produces an
entry in ``connections.env`` that cannot be exported and that several dotenv
parsers reject outright. The strictest destination sets the rule, because a name
is checked once and then used in all three. Loosen this to match TOML and you
move the failure from a refusal an operator can read into a credential that
silently will not load.

Lowercase is load-bearing for the same reason. The local backend upper-cases a
coordinate into that env key, so permitting ``Work`` beside ``work`` would fold
two connections onto one cell and let one account read the other's credential.

**One check, on every path.** It is applied in
:func:`~arcagent.modules.connectors.install.plan_connector`, which the CLI, the
web, the TUI and every read verb behind them all come through — so there is no
route by which an unusable name survives anywhere, and no second copy to drift.

The one thing that must still work on a name this refuses is DELETING it, or the
strict rule would create something an operator cannot get rid of. It does:
``remove`` reads a connection's credential fields through the planner, treats a
refusal as "no fields to delete", and drops the connection and its record
regardless. That is not an exception to the rule — it is sound because the
rule is the same everywhere: ``SecretRef`` applies it too, so no credential can
exist under a name this refuses, and there is nothing left behind to strand.

:func:`refusal` is written for the person who typed the name — an operator naming
a connected mailbox ``blackarc industrial email``, which is a perfectly
reasonable thing to type and which took a whole agent off a live fleet. It says
what is wrong and hands back the name they meant.
"""

from __future__ import annotations

import re

#: 1-64 characters, lowercase letters, digits and underscores, opening on a
#: letter or digit. Narrow enough to survive every destination above unquoted.
COORDINATE = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")

#: What a name is described as, once, so every refusal reads the same.
RULE = (
    "lowercase letters, digits and underscores only, 1-64 characters, "
    "starting with a letter or digit"
)


def is_coordinate(value: str) -> bool:
    """True when ``value`` can be used as a name without quoting or folding."""
    return COORDINATE.fullmatch(value) is not None


def slug(value: str) -> str:
    """The nearest legal name to what the operator typed, or empty if there is none.

    Offered rather than applied: silently renaming an operator's account is how
    they end up unable to find it again.
    """
    lowered = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
    return lowered[:64].rstrip("_")


def refusal(label: str, value: str) -> str:
    """The refusal an operator reads, naming the fix rather than only the rule."""
    suggestion = slug(value)
    example = f" Try {suggestion!r}." if suggestion else ""
    return f"{label} {value!r} cannot be used: {RULE}.{example}"


__all__ = ["COORDINATE", "RULE", "is_coordinate", "refusal", "slug"]
