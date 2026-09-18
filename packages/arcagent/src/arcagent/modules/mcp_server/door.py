"""SPEC-082 COMP-002/003/005 — the composed inbound ``tools/call`` path.

This is the one ordered pipeline every inbound call runs through:

1. :func:`~arcagent.modules.mcp_server.identity.verify_inbound` — identity,
   signature, replay, tier (fail-closed, audits its own denials);
2. :func:`~arcagent.modules.mcp_server.enrollment.require_enrolled` — a verified
   caller must be an operator-enrolled fleet member at enterprise/federal (audited
   deny; a no-op at personal, where enrollment is optional);
3. :meth:`~arcagent.modules.mcp_server.allowlist.ExposureAllowlist.check_call` —
   the verb is refused *before* dispatch if it is not exposed (audited deny);
4. :func:`~arcagent.modules.mcp_server.dispatch.dispatch_tools_call` — the single
   existing ``AgentCapabilityProvider.invoke`` envelope (policy + audit run inside);
5. :func:`~arcagent.modules.mcp_server.audit.emit_door_event` — one ``allow`` event.

The verb name and arguments are read from the verified request's ``content`` (a
JSON-RPC ``tools/call`` message: ``params.name`` / ``params.arguments``).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import arcrun
from arcteam.crypto import ReplayCache
from arctrust import AuditSink

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import AllowlistRefused, ExposureAllowlist
from arcagent.modules.mcp_server.audit import emit_door_event
from arcagent.modules.mcp_server.dispatch import dispatch_tools_call
from arcagent.modules.mcp_server.enrollment import require_enrolled
from arcagent.modules.mcp_server.identity import InboundRequest, verify_inbound

#: The verb recorded for a dispatched call.
_CALL_VERB = "tools/call"


def _call_params(content: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract ``(name, arguments)`` from a ``tools/call`` JSON-RPC message body."""
    params = content.get("params", {})
    name = params.get("name", "")
    arguments = params.get("arguments", {})
    return name, arguments


async def authorize_and_dispatch(
    request: InboundRequest,
    *,
    provider: AgentCapabilityProvider,
    allowlist: ExposureAllowlist,
    replay_cache: ReplayCache,
    audit_sink: AuditSink,
    tier: str = "personal",
    enrolled: Collection[str] | None = None,
) -> arcrun.CapabilityResult:
    """Verify, authorize, dispatch, and audit one inbound ``tools/call``.

    ``enrolled`` is the roster of operator-enrolled fleet-member DIDs (each proven
    by a verified :class:`arctrust.policy.EnrollmentGrant` upstream of the door). It
    gates only at enterprise/federal, where a verified caller absent from the roster
    is refused; personal tier ignores it (enrollment optional).

    Raises :class:`~arcagent.modules.mcp_server.identity.InboundRejected` if the
    envelope fails verification or the caller is not enrolled at a tier that
    requires it, or
    :class:`~arcagent.modules.mcp_server.allowlist.AllowlistRefused` if the verb is
    not exposed — all fail closed with a ``deny`` audit event.
    """
    caller_did = verify_inbound(
        request, replay_cache=replay_cache, audit_sink=audit_sink, tier=tier
    )
    require_enrolled(caller_did, enrolled, tier=tier, audit_sink=audit_sink)
    name, arguments = _call_params(request.content)

    try:
        allowlist.check_call(name)
    except AllowlistRefused:
        emit_door_event(
            audit_sink,
            caller_did=caller_did,
            verb=_CALL_VERB,
            outcome="deny",
            tier=tier,
            arguments=arguments,
        )
        raise

    result = await dispatch_tools_call(
        provider, name=name, arguments=arguments, caller_did=caller_did
    )
    emit_door_event(
        audit_sink,
        caller_did=caller_did,
        verb=_CALL_VERB,
        outcome="allow",
        tier=tier,
        arguments=arguments,
    )
    return result


__all__ = ["authorize_and_dispatch"]
