"""Canonical agent-identity resolution — the ONE DID parser + roster join.

H-007: agent identity showed up inconsistently across the dashboard —
sometimes a raw DID, sometimes a bare file/short name — because every screen
that needed a friendly label re-derived one from a raw DID string in React
(``did.split('/').pop()``, tail-stripping, ad-hoc maps). That is how
zero-count / mismatched-name bugs are born: two components disagree on how
to peel a DID apart.

This module is the single place that parses a DID into its display parts
and joins the roster's friendly name — **by DID**, never by matching agent
names or ids across independently-computed strings. Every route that lists
or describes an agent calls :func:`resolve_agent_identity` once and ships
the resulting :class:`AgentIdentity` shape; the frontend renders it with one
shared component (``AgentIdentity.tsx``) instead of re-parsing.

DID shape (see ``arctrust.identity``): ``did:{platform}:{host}:{type}/{short_id}``
e.g. ``did:arc:local:executor/e347da22`` -> host=local, platform=arc,
type=executor, short_id=e347da22. Role identities (``did:arc:ui:viewer``)
omit the ``/{short_id}`` suffix entirely — that is a valid, not malformed,
shape (there is no keypair behind a role DID), so ``short_id`` is simply
empty rather than "unknown".
"""

from __future__ import annotations

from pydantic import BaseModel

UNKNOWN = "unknown"


class AgentIdentity(BaseModel):
    """Canonical, display-ready agent identity.

    ``host``, ``platform``, ``type``, ``short_id`` are always populated
    (falling back to :data:`UNKNOWN` for a DID that cannot be parsed at
    all, or to ``""`` for a structurally valid DID missing that segment).
    ``name`` is the friendly label from the roster join — ``None`` when the
    DID has no matching roster row (an agent identity with no roster entry
    still renders its parsed parts; it just has no friendly name).
    """

    did: str
    host: str
    platform: str
    type: str
    short_id: str
    name: str | None = None


def parse_did(did: str) -> AgentIdentity:
    """Parse a DID into display parts. Never raises — malformed input degrades.

    A blank string, a non-``did:`` string, or anything with fewer than the
    ``did:<platform>:<host>:<rest>`` four segments returns an
    :class:`AgentIdentity` with every part set to :data:`UNKNOWN` rather than
    raising — the fleet view must keep rendering even for a legacy or
    corrupt identity string.
    """
    if not did or not did.startswith("did:") or len(did.split(":")) < 4:
        return AgentIdentity(
            did=did, host=UNKNOWN, platform=UNKNOWN, type=UNKNOWN, short_id=UNKNOWN
        )

    parts = did.split(":")

    platform = parts[1] or UNKNOWN
    host = parts[2] or UNKNOWN
    tail = ":".join(parts[3:])
    if "/" in tail:
        agent_type, _, short_id = tail.partition("/")
    else:
        # A role DID (e.g. "did:arc:ui:viewer") has no keypair hash — that's
        # a valid shape, not a malformed one, so short_id is empty, not UNKNOWN.
        agent_type, short_id = tail, ""

    return AgentIdentity(
        did=did,
        host=host,
        platform=platform,
        type=agent_type or UNKNOWN,
        short_id=short_id,
    )


def resolve_agent_identity(did: str, name: str | None) -> AgentIdentity:
    """Parse ``did`` and attach the friendly ``name`` from the roster join.

    ``name`` must already be resolved by the caller via the DID join (e.g.
    the roster row whose ``did`` equals this one) — this function does no
    matching of its own beyond attaching the value it is given.
    """
    identity = parse_did(did)
    identity.name = name or None
    return identity
