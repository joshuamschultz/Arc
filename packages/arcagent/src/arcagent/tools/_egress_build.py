"""EgressProxy construction — the single per-agent outbound-network gate.

Split out of ``core/agent_lifecycle`` (LOC budgets signal wrong-home code):
building the proxy is a tools concern, not core lifecycle. The nucleus just
calls :func:`build_egress_proxy` with the config + session ledger it owns.

The proxy is deny-by-default (origin allowlist from
``tools.policy.egress_allowlist``); every allow/deny is audited, a successful
``egress.allowed`` records the ``external_comms`` trifecta leg into the session
ledger (SPEC-035 REQ-013), and the no-exfil check compares the destination
clearance against the session's max-read classification (SPEC-038 REQ-025/F2).
"""

from __future__ import annotations

from typing import Any

from arcagent.core.config import ArcAgentConfig
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    SessionCapabilityLedger,
    current_session_id,
)
from arcagent.tools._egress import EgressProxy


async def _httpx_send(url: str, method: str, **kwargs: Any) -> Any:
    """Default egress transport — a real async HTTP request via httpx."""
    import httpx

    async with httpx.AsyncClient() as client:
        return await client.request(method, url, **kwargs)


def build_egress_proxy(
    *,
    config: ArcAgentConfig,
    ledger: SessionCapabilityLedger | None,
    telemetry: Any,
) -> EgressProxy:
    """Instantiate the single per-agent EgressProxy."""

    def _egress_audit(event: str, payload: dict[str, Any]) -> None:
        if telemetry is not None:
            telemetry.audit_event(event, payload)
        if event == "egress.allowed" and ledger is not None:
            # Key the external-comms leg to the session that made the request so it
            # composes with that session's reads/untrusted-input, not a global bucket.
            ledger.record(current_session_id(), frozenset({EXTERNAL_COMMS}))

    def _session_data_classification() -> str:
        # SPEC-038 F2 — the label the no-exfil gate protects against is the max
        # classification READ by the requesting session; no ledger → UNCLASSIFIED.
        if ledger is None:
            return "UNCLASSIFIED"
        return ledger.max_read_classification(current_session_id()).name

    return EgressProxy(
        allowlist=set(config.tools.policy.egress_allowlist),
        send_fn=_httpx_send,
        audit_sink=_egress_audit,
        origin_clearances=dict(config.tools.policy.egress_clearances),
        data_classifier=_session_data_classification,
        strict=config.security.tier == "federal",
    )


__all__ = ["build_egress_proxy"]
