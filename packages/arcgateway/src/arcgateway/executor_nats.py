"""NATSExecutor — multi-instance scaling stub (deferred).

Extracted from executor.py to keep the core executor module within the
arcgateway core LOC budget (ADR-004 / G1.6).

This module is the single definition. ``arcgateway.executor`` does NOT
re-export it — import it from here.
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
