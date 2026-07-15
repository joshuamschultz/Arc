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

import asyncio
import contextvars
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from arctrust.classification import Classification


def _now() -> str:
    """ISO-8601 UTC timestamp for a provenance entry."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProvenanceEntry:
    """One ALLOWED call that contributed trifecta legs to a session (SPEC-035).

    Surfaced to the operator on a trifecta block so the completed composition is
    triageable: WHICH prior tool call lit each leg, a short redacted argument
    summary, and WHEN. ``legs`` is sorted for stable display.
    """

    legs: tuple[str, ...]
    tool_name: str
    arg_summary: str
    at: str

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for approval requests and audit payloads."""
        return {
            "legs": list(self.legs),
            "tool": self.tool_name,
            "args": self.arg_summary,
            "at": self.at,
        }

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
    # web/browser reads INGEST untrusted content but are NOT an egress channel.
    # A search hits a fixed provider with a (federally PII-redacted) query; a
    # fetch/navigation is a GET whose destination is governed by the web/browser
    # module's own URL policy, not the trifecta. external_comms is reserved for
    # tools that PUSH agent-chosen content to an agent-chosen sink (messaging,
    # notify, post, raw network egress) — the leg that actually exfiltrates.
    # Tagging a read as egress double-counts and bricks ordinary research+write.
    "web": frozenset({UNTRUSTED_INPUT}),
    "browser": frozenset({UNTRUSTED_INPUT}),
    "browser_navigate": frozenset({UNTRUSTED_INPUT}),
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


# The owner's own paired channel — a TRUSTED delivery sink. The lethal trifecta
# guards against exfiltration to an ATTACKER (ASI09); delivering a result to the
# operator themselves is not exfiltration, so an owner-directed send does NOT
# produce the external_comms leg. Any channel connected to the owner may deliver
# back to the owner. Narrow by design: only this explicit alias is trusted —
# never a broader "any user://" match — so a third-party recipient can never be
# smuggled past the gate.
OWNER_CHANNEL = "user://operator"

# Egress tools that ALWAYS deliver to the owner's own connected channel: the
# whole tool is an owner sink — it takes only a message body and routes to the
# operator's paired DM, with no reachable third-party destination. Their
# external_comms leg is dropped unconditionally (Telegram + Slack notify).
_OWNER_DIRECTED_EGRESS: frozenset[str] = frozenset({"notify_user", "slack_notify_user"})

# Egress tools whose external_comms leg is destination-scoped, mapped to the
# argument that names the recipient(s). The leg drops only when EVERY recipient
# is the owner channel; any non-owner (or mixed) recipient keeps it so the
# forbidden-composition rule still fires on third-party destinations.
_OWNER_SCOPED_EGRESS: dict[str, str] = {"messaging_send": "to"}


def _targets_only_owner(recipients: object) -> bool:
    """True iff ``recipients`` names at least one target and ALL are the owner."""
    if not isinstance(recipients, str):
        return False
    targets = [t.strip() for t in recipients.split(",") if t.strip()]
    return bool(targets) and all(t == OWNER_CHANNEL for t in targets)


def _is_owner_directed(tool_name: str, arguments: Mapping[str, object]) -> bool:
    """True iff THIS call delivers only to the owner's own connected channel.

    Two forms: an unconditionally owner-directed tool (the whole channel is the
    owner's), or a destination-scoped tool whose every recipient is the owner.
    """
    if tool_name in _OWNER_DIRECTED_EGRESS:
        return True
    dest_arg = _OWNER_SCOPED_EGRESS.get(tool_name)
    return dest_arg is not None and _targets_only_owner(arguments.get(dest_arg))


def legs_for_call(
    tool_name: str,
    capability_tags: Iterable[str],
    arguments: Mapping[str, object],
) -> frozenset[str]:
    """Resolve THIS call's trifecta legs, honoring the owner-channel exemption.

    Identical to :func:`legs_for_tags` except that an egress delivering ONLY to
    the owner's own connected channel does not contribute the ``external_comms``
    leg — the operator's own sink is trusted, not an exfiltration path (ASI09).
    Any non-owner recipient keeps the leg, so the forbidden-composition rule
    still fires on third-party destinations. Destination-blind tools resolve
    exactly as before.
    """
    legs = legs_for_tags(capability_tags)
    if EXTERNAL_COMMS in legs and _is_owner_directed(tool_name, arguments):
        return legs - {EXTERNAL_COMMS}
    return legs


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
        # SPEC-035 approval enrichment — ordered provenance of leg-contributing
        # calls per session, so a later trifecta block can be explained leg-by-leg
        # to the operator (which prior call lit each leg, and when).
        self._provenance_by_session: dict[str, list[ProvenanceEntry]] = {}
        # SPEC-038 F2 — the highest classification of data READ this session,
        # keyed by session id. The no-exfil egress gate reads this so a SECRET
        # read this session bars an UNCLASSIFIED-cleared destination.
        self._read_class_by_session: dict[str, Classification] = {}
        # SPEC-043 REQ-032 — per-session admission lock. Concurrent tool dispatch
        # interleaves the ``snapshot → await evaluate → record`` critical section
        # (a TOCTOU window); this lock serializes only that O(1) decision so two
        # calls whose union completes a forbidden composition are evaluated in
        # sequence and the second sees the completed union. tool.execute and any
        # human-approval await stay OUTSIDE the lock (held to microseconds).
        self._locks: dict[str, asyncio.Lock] = {}

    def admission_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session admission lock (created on first use)."""
        return self._locks.setdefault(session_id, asyncio.Lock())

    def snapshot(self, session_id: str) -> frozenset[str]:
        """Return the accumulated legs for a session (empty if none yet)."""
        return frozenset(self._by_session.get(session_id, set()))

    def record(
        self,
        session_id: str,
        legs: frozenset[str],
        *,
        tool_name: str = "",
        arg_summary: str = "",
    ) -> None:
        """Accumulate the legs of an ALLOWED call into the session's union.

        Also appends a :class:`ProvenanceEntry` recording which tool lit these legs
        (with a short redacted argument summary and a UTC timestamp) so a later
        trifecta block can be explained leg-by-leg to the operator. The caller is
        responsible for redacting/bounding ``arg_summary`` before it reaches here.
        """
        if not legs:
            return
        self._by_session.setdefault(session_id, set()).update(legs)
        self._provenance_by_session.setdefault(session_id, []).append(
            ProvenanceEntry(
                legs=tuple(sorted(legs)),
                tool_name=tool_name,
                arg_summary=arg_summary,
                at=_now(),
            )
        )

    def provenance(self, session_id: str) -> list[ProvenanceEntry]:
        """Return the ordered provenance of leg-contributing calls for a session."""
        return list(self._provenance_by_session.get(session_id, []))

    def record_read(self, session_id: str, classification: Classification) -> None:
        """Raise the session's max-read classification (monotone, SPEC-038 F2).

        Called when an ALLOWED tool touches labeled data; the running maximum is
        the data classification the egress gate must protect against exfil.
        """
        current = self._read_class_by_session.get(session_id, Classification.UNCLASSIFIED)
        if classification > current:
            self._read_class_by_session[session_id] = classification

    def max_read_classification(self, session_id: str) -> Classification:
        """Return the highest classification read this session (default UNCLASSIFIED)."""
        return self._read_class_by_session.get(session_id, Classification.UNCLASSIFIED)

    def reset(self, session_id: str) -> None:
        """Drop a session's accumulated legs (e.g. on session close)."""
        self._by_session.pop(session_id, None)
        self._provenance_by_session.pop(session_id, None)
        self._read_class_by_session.pop(session_id, None)


__all__ = [
    "EXTERNAL_COMMS",
    "LETHAL_TRIFECTA",
    "OWNER_CHANNEL",
    "PRIVATE_DATA",
    "TAG_TO_LEGS",
    "UNTRUSTED_INPUT",
    "ProvenanceEntry",
    "SessionCapabilityLedger",
    "bind_session_id",
    "current_session_id",
    "legs_for_call",
    "legs_for_tags",
    "reset_session_id",
]
