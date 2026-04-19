"""NATSExecutor — multi-instance scaling stub (deferred).

Extracted from executor.py to keep the core executor module within the
arcgateway core LOC budget (ADR-004 / G1.6).

Public API is re-exported from arcgateway.executor so existing imports
``from arcgateway.executor import NATSExecutor`` continue to work unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from arcgateway.executor import Delta, InboundEvent


class NATSExecutor:
    """NATS-backed executor for multi-instance gateway deployments.

    Routes agent execution to worker nodes via NATS subject addressing.
    Required when a single bot token serves multiple gateway replicas behind
    a load balancer.

    Implementation deferred — no ETA. See SDD §6 open question on
    NATS-vs-in-process queue for >1 instance (SPEC-018).
    """

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Dispatch event to NATS worker and stream response.

        Raises:
            NotImplementedError: Multi-instance scaling is deferred.
        """
        raise NotImplementedError(
            "NATSExecutor: multi-instance NATS-based scaling is deferred. "
            "No implementation ETA in SPEC-018. See SDD §6."
        )
