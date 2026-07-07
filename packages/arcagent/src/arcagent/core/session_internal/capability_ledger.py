"""Session-scoped capability accumulation — the lethal-trifecta ledger.

SPEC-035 REQ-011/012. Private-data-read + external-comms + untrusted-input
must never *co-occur* within a session without human approval (Simon Willison's
"lethal trifecta"). The exfiltration path unfolds across multiple tool calls, so
we accumulate the capability *legs* of every ALLOWED call and feed the running
union back into policy as ``PolicyContext.session_capabilities``. When the
accumulated union completes a forbidden set, arctrust's ``GlobalLayer`` denies
(and arcagent's ``HumanGate`` may pause for approval).

Concern boundary: the tag→leg mapping is *deployment knowledge* and lives here
in arcagent; arctrust receives only the resolved frozensets. The ledger is
per-agent-instance and keyed by session id — shared-nothing across sessions and
across agents (REQ-012 AC2).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable

# The three legs of the lethal trifecta (REQ-011).
PRIVATE_DATA = "private_data"
EXTERNAL_COMMS = "external_comms"
UNTRUSTED_INPUT = "untrusted_input"

LETHAL_TRIFECTA: frozenset[str] = frozenset({PRIVATE_DATA, EXTERNAL_COMMS, UNTRUSTED_INPUT})

# Deployment mapping: a built-in/registered tool's capability_tags → trifecta
# legs. OQ-1: the untrusted-input leg is proxied by "reads of web/browser/
# externally-fetched content this session" (full data-provenance taint tracking
# is deferred — PRD Could). A tag may contribute more than one leg (e.g. a web
# fetch both egresses and ingests untrusted content).
TAG_TO_LEGS: dict[str, frozenset[str]] = {
    # private-data reads
    "file_read": frozenset({PRIVATE_DATA}),
    "user_profile": frozenset({PRIVATE_DATA}),
    "memory": frozenset({PRIVATE_DATA}),
    "recall": frozenset({PRIVATE_DATA}),
    # external comms / egress
    "network_egress": frozenset({EXTERNAL_COMMS}),
    "slack_notify": frozenset({EXTERNAL_COMMS}),
    "audio": frozenset({EXTERNAL_COMMS}),
    # web/browser reads egress AND ingest untrusted content (both legs)
    "web": frozenset({EXTERNAL_COMMS, UNTRUSTED_INPUT}),
    "browser": frozenset({EXTERNAL_COMMS, UNTRUSTED_INPUT}),
    "extract": frozenset({UNTRUSTED_INPUT}),
    # a shell ingests untrusted content (command output, fetched files, curl
    # responses). NOT external_comms: at ent/fed bash runs --network=none, so
    # tagging egress would spuriously trip the trifecta gate (SPEC-035 scope).
    "subprocess": frozenset({UNTRUSTED_INPUT}),
}


def legs_for_tags(capability_tags: Iterable[str]) -> frozenset[str]:
    """Resolve a tool's capability tags into trifecta legs (deployment map)."""
    legs: set[str] = set()
    for tag in capability_tags:
        legs |= TAG_TO_LEGS.get(tag, frozenset())
    return frozenset(legs)


# The session id of the running dispatch. A ContextVar (not a plain global) so
# concurrent per-session runs each see their own id — the tool-dispatch wrapper
# and the per-agent egress proxy both read it to key the ledger correctly. The
# accumulator is genuinely per-session; ``reset(session_id)`` clears one bucket.
_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arc_current_session_id", default=""
)


def current_session_id() -> str:
    """Return the session id bound to the running dispatch (empty outside a run)."""
    return _current_session_id.get()


def bind_session_id(session_id: str) -> contextvars.Token[str]:
    """Bind the current-dispatch session id; returns a token for :func:`reset_session_id`."""
    return _current_session_id.set(session_id)


def reset_session_id(token: contextvars.Token[str]) -> None:
    """Restore the previous session-id binding."""
    _current_session_id.reset(token)


class SessionCapabilityLedger:
    """Per-session accumulator of capability legs from allowed tool calls.

    Shared-nothing: each agent instance owns one ledger; entries are keyed by
    session id so concurrent sessions never bleed legs into one another.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, set[str]] = {}

    def snapshot(self, session_id: str) -> frozenset[str]:
        """Return the accumulated legs for a session (empty if none yet)."""
        return frozenset(self._by_session.get(session_id, set()))

    def record(self, session_id: str, legs: frozenset[str]) -> None:
        """Accumulate the legs of an ALLOWED call into the session's union."""
        if not legs:
            return
        self._by_session.setdefault(session_id, set()).update(legs)

    def reset(self, session_id: str) -> None:
        """Drop a session's accumulated legs (e.g. on session close)."""
        self._by_session.pop(session_id, None)


__all__ = [
    "EXTERNAL_COMMS",
    "LETHAL_TRIFECTA",
    "PRIVATE_DATA",
    "TAG_TO_LEGS",
    "UNTRUSTED_INPUT",
    "SessionCapabilityLedger",
    "bind_session_id",
    "current_session_id",
    "legs_for_tags",
    "reset_session_id",
]
