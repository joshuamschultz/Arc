"""Unified proactive-execution module — SPEC-017 Phase 6.

Exports the public surface so callers can ``from arcagent.modules.proactive
import ProactiveEngine, Schedule, CircuitBreaker, LeaderElection``.

The legacy ``pulse`` and ``scheduler`` modules remain on disk for now;
they will be deleted in a dedicated migration commit once persisted
schedule state has been migrated.
"""

from __future__ import annotations

from arcagent.modules.proactive.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
)
from arcagent.modules.proactive.engine import (
    HeartbeatContext,
    ProactiveEngine,
    Schedule,
    ScheduleKind,
    evaluate_heartbeat,
)
from arcagent.modules.proactive.leader import (
    InMemoryElection,
    LeaderElection,
    NoOpLeaderElection,
)
from arcagent.modules.proactive.timezone import ActiveHours, next_occurrence

__all__ = [
    "ActiveHours",
    "CircuitBreaker",
    "CircuitState",
    "HeartbeatContext",
    "InMemoryElection",
    "LeaderElection",
    "NoOpLeaderElection",
    "ProactiveEngine",
    "Schedule",
    "ScheduleKind",
    "evaluate_heartbeat",
    "next_occurrence",
]
