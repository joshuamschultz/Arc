"""D-580 — the one predicate deciding whether a tool may send data out.

Two signals already exist and no third is invented. The **egress** signal is the
``external_comms`` trifecta leg, resolved from a tool's declared
``capability_tags`` by
:func:`~arcagent.core.session_internal.capability_ledger.legs_for_tags` — the
same function the runtime trifecta gate reads, so a tool cannot be egress to one
control and inert to the other. The **origin** signal is
:attr:`~arcagent.tools._transport.RegisteredTool.source` together with the
capability scan root the code loaded from, which is what makes a third-party
bundle distinguishable from an operator's own capability.

===========  ==========================================================
Tier         May a tool egress?
===========  ==========================================================
personal     Yes, any origin.
enterprise   Only when named in ``tools.policy.egress_allow``.
federal      Only an operator-signed capability. Never an extension.
===========  ==========================================================

Federal does not consult ``egress_allow``, and that is deliberate rather than an
omission: at federal the operator's act of authorization is *signing the
capability into the agent's own folder*, which is a strictly stronger act than
adding a name to a config file. A config list must never buy at federal what
only a signature buys.

**The verdict is taken at INSTALL and at LOAD, never at call time.** A tool
refused mid-turn is precisely the silent failure this rule exists to prevent, so
a bundle declaring a forbidden send is refused before a credential is collected,
and a forbidden tool never enters the registry the model reads its catalog from.
Nothing in this module runs on the dispatch path.

The gate can only SUBTRACT. ``egress_allow`` is not a second allowlist able to
re-admit what ``ToolConfig.deny`` excluded: :meth:`ToolRegistry.register` checks
deny first and this verdict last, so deny still wins.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from arcagent.capabilities.capability_loader import EXTENSION_ROOT_PREFIX
from arcagent.core.session_internal.capability_ledger import EXTERNAL_COMMS, legs_for_tags
from arcagent.core.tier import Tier


@dataclass(frozen=True)
class EgressVerdict:
    """One tool's egress decision, carrying the sentence an operator will read.

    Attributes:
        allowed: Whether this tool may register and be installed.
        tool: The verb the verdict is about. Empty on an allow.
        reason: The machine-readable refusal code, for audit payloads.
        message: The operator-facing sentence. Names the tool, the tier, and the
            remedy, because a refusal an operator cannot act on is a bug report
            waiting to happen.
    """

    allowed: bool
    tool: str = ""
    reason: str = ""
    message: str = ""


#: The one allow value. Immutable and shared — an allow carries no detail.
_ALLOWED = EgressVerdict(allowed=True)


def is_egress(capability_tags: Iterable[str]) -> bool:
    """Whether these declared tags light the ``external_comms`` leg.

    Reads the deployment tag map rather than matching tag names here, so adding a
    sending capability to that map arms this gate at the same moment it arms the
    trifecta gate.
    """
    return EXTERNAL_COMMS in legs_for_tags(capability_tags)


def extension_root(name: str) -> str:
    """The capability-root form of an extension identity. Idempotent on either form."""
    return f"{EXTENSION_ROOT_PREFIX}{name.removeprefix(EXTENSION_ROOT_PREFIX)}"


def is_extension_origin(*, source: str = "", scan_root: str = "") -> bool:
    """Whether this tool's code came from a third-party extension bundle.

    Either signal alone is sufficient: attachment-supplied verbs carry the
    extension identity in ``source``, while a bundle's own ``.py`` capabilities
    carry it in the scan root they registered through. Requiring both would let a
    bundle escape the federal ban by arriving down the path that sets only one.
    """
    return source.startswith(EXTENSION_ROOT_PREFIX) or scan_root.startswith(EXTENSION_ROOT_PREFIX)


def egress_verdict(
    *,
    tool_name: str,
    capability_tags: Iterable[str],
    tier: Tier,
    from_extension: bool,
    egress_allow: Sequence[str],
) -> EgressVerdict:
    """Decide whether ``tool_name`` may send data out of this deployment.

    Args:
        tool_name: The verb as the operator reads it in a catalog or a config file.
        capability_tags: The tool's declared tags — a manifest declaration at
            install time, a :class:`RegisteredTool`'s own tags at load time.
        tier: The deployment tier.
        from_extension: Whether the code came from a third-party bundle.
        egress_allow: ``tools.policy.egress_allow`` — the operator's per-tool
            egress permissions. Consulted at enterprise only.

    Returns:
        An allow, or a refusal naming the tool, the tier, and the remedy. A tool
        that does not egress is always allowed; this predicate speaks only to
        sending, never to reading.
    """
    if not is_egress(capability_tags):
        return _ALLOWED
    if tier is Tier.PERSONAL:
        return _ALLOWED
    if tier is Tier.FEDERAL:
        if not from_extension:
            return _ALLOWED
        return EgressVerdict(
            allowed=False,
            tool=tool_name,
            reason="egress_extension_forbidden",
            message=(
                f"tool {tool_name!r} sends data out and comes from an extension, which "
                f"{Tier.FEDERAL.value} tier never permits. Sending at this tier means "
                f"writing the send as a capability, signing it with the operator key, "
                f"and placing it in the agent's capabilities folder."
            ),
        )
    if tool_name in egress_allow:
        return _ALLOWED
    return EgressVerdict(
        allowed=False,
        tool=tool_name,
        reason="egress_not_allowlisted",
        message=(
            f"tool {tool_name!r} sends data out, and {Tier.ENTERPRISE.value} tier permits "
            f"egress only for tools the operator named in tools.policy.egress_allow. Add "
            f"{tool_name!r} to that list, or connect a bundle that only reads."
        ),
    )


def first_forbidden_egress(
    declared: Iterable[tuple[str, Sequence[str]]],
    *,
    tier: Tier,
    from_extension: bool,
    egress_allow: Sequence[str],
) -> EgressVerdict | None:
    """The first declared ``(name, capability_tags)`` this tier refuses, or ``None``.

    First rather than all: a refusal is a stop, and naming one forbidden verb is
    what an operator needs to act. The pairs are plain tuples so this module stays
    free of any dependency on the manifest and attachment types above it.
    """
    for name, tags in declared:
        verdict = egress_verdict(
            tool_name=name,
            capability_tags=tags,
            tier=tier,
            from_extension=from_extension,
            egress_allow=egress_allow,
        )
        if not verdict.allowed:
            return verdict
    return None


__all__ = [
    "EgressVerdict",
    "egress_verdict",
    "extension_root",
    "first_forbidden_egress",
    "is_egress",
    "is_extension_origin",
]
