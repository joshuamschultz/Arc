"""SPEC-082 COMP-003 / REQ-411 — dispatch through the ONE existing envelope.

A verified inbound ``tools/call`` is turned into
``AgentCapabilityProvider.invoke(name, args, caller_did=<external DID>)`` — the same
envelope a native call uses — so the core registry's pipeline (schema → first-DENY
policy → veto → audit) runs identically and is never re-implemented. There is no
second dispatch path: a private route would be the exact producers-unwired failure
mode REQ-411 forbids. The external caller's DID becomes the ``caller_did`` the
envelope authorizes and audits against.
"""

from __future__ import annotations

from typing import Any

import arcrun

from arcagent.capabilities.provider import AgentCapabilityProvider


async def dispatch_tools_call(
    capability_provider: AgentCapabilityProvider,
    *,
    name: str,
    arguments: dict[str, Any],
    caller_did: str,
) -> arcrun.CapabilityResult:
    """Route one ``tools/call`` through the provider's public ``invoke`` envelope.

    The receiver is named ``capability_provider`` (not ``provider``) so the
    ArcLLM-handle guard (``tests/architecture/test_no_provider_handle_calls.py``)
    can see this is a capability/tool-dispatch call, not a model call — the same
    category the guard already excuses for ``attachment`` / ``_delegate``.
    """
    return await capability_provider.invoke(name, arguments, caller_did=caller_did)


__all__ = ["dispatch_tools_call"]
