"""SecurityChangeApproval (SPEC-085 COMP-007, REQ-465).

A security-relevant settings change (policy, identity, credentials) must not
apply silently. This module binds a proposed field+value change to a PENDING
row on the existing mechanical operator-approval subsystem
(``arcstore.approvals``) and verifies the operator's eventual grant — callable
at the arcagent layer without importing arcui/arccli.

Three functions, no new storage or crypto primitives:
  - :func:`is_security_relevant` classifies a dotted config id.
  - :func:`request_settings_approval` creates the PENDING row, binding the
    exact field+value change to a ``call_hash`` the way every other
    trifecta-completing approval in this codebase does (see
    ``arcagent.tools.human_gate``).
  - :func:`verify_settings_grant` is the fail-closed gate: only a still
    ``approved`` row carrying a grant that ``arctrust.policy.verify_approval``
    accepts AND whose ``approver_did`` matches the given operator returns
    True. Any other shape -- pending, denied, expired, missing grant, a
    tampered call_hash, or an unexpected/malformed grant payload -- returns
    False (ASI09: pin to the deployment operator, not merely "not the agent").
"""

from __future__ import annotations

from uuid import uuid4

from arcstore.approvals import ApprovalStore, PendingApproval
from arctrust.policy import ToolCall, grant_from_wire, verify_approval

_SECURITY_PREFIXES = ("security.", "identity.", "vault.", "tools.policy.")
_SETTINGS_TOOL_NAME = "settings.change"
_UNCLASSIFIED = "unclassified"


def is_security_relevant(field: str) -> bool:
    """Whether a dotted config id (e.g. ``security.audit_mode``) is security-relevant.

    True iff ``field`` starts with ``security.``, ``identity.``, ``vault.``, or
    ``tools.policy.`` -- the settings families that gate ASI01/ASI03 (goal and
    identity integrity) and credential handling.
    """
    return field.startswith(_SECURITY_PREFIXES)


async def request_settings_approval(
    *,
    field: str,
    value: object,
    agent_did: str,
    store: ApprovalStore,
    session_id: str = "",
) -> PendingApproval:
    """Create a PENDING approval row bound to the exact ``field``/``value`` change.

    Builds the same ``ToolCall`` -> ``call_hash`` binding every other
    trifecta-completing approval in this codebase uses, so a grant minted
    against this row's ``call_hash`` can only ever unlock this one change --
    different values for the same field hash differently.
    """
    from arctrust.policy import _hash_call

    arguments = {"field": field, "value": str(value)}
    call = ToolCall(
        tool_name=_SETTINGS_TOOL_NAME,
        arguments=arguments,
        agent_did=agent_did,
        session_id=session_id,
        classification=_UNCLASSIFIED,
    )
    pending = PendingApproval(
        id=str(uuid4()),
        agent_did=agent_did,
        tool=_SETTINGS_TOOL_NAME,
        call_hash=_hash_call(call),
        session_id=session_id,
        arguments=arguments,
    )
    return await store.create(pending)


def verify_settings_grant(pending: PendingApproval, *, operator_did: str) -> bool:
    """Fail-closed check that ``pending`` carries a valid grant from ``operator_did``.

    Returns True only when ALL hold:
      1. ``pending.status == "approved"`` and ``pending.grant`` is present.
      2. The grant parses (a malformed/tampered payload fails closed, not raises).
      3. The grant's ``approver_did`` is exactly ``operator_did`` -- a validly
         signed grant from a different (e.g. impostor) operator still fails.
      4. Reconstructing the same ``ToolCall`` this row was created from and
         calling ``arctrust.policy.verify_approval`` accepts the grant -- this
         is what catches a grant signed over a different row's ``call_hash``.

    Any other outcome, including an inconsistent "approved" row with no
    grant, returns False.
    """
    if pending.status != "approved" or pending.grant is None:
        return False
    try:
        grant = grant_from_wire(pending.grant)
    except (KeyError, ValueError, TypeError):
        return False
    if grant.approver_did != operator_did:
        return False
    call = ToolCall(
        tool_name=pending.tool,
        arguments=dict(pending.arguments),
        agent_did=pending.agent_did,
        session_id=pending.session_id,
        classification=_UNCLASSIFIED,
    )
    return verify_approval(call, grant)


__all__ = [
    "is_security_relevant",
    "request_settings_approval",
    "verify_settings_grant",
]
