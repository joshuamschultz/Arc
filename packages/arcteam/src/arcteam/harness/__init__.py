"""The fleet-member seam (H-040): one ``HarnessAdapter`` contract, many harnesses.

Native ``arcagent`` and every foreign harness are addressed through the same
Protocol. The Protocol and its envelope types live here (arcteam + arctrust types
only — no arcagent, no arcllm); the native ``ArcAgentHarness`` implementation
lives in arcgateway, which legally imports arcagent.
"""

from __future__ import annotations

from arcteam.harness.agent_type import (
    AgentType,
    discover_agent_types,
    find_agent_type,
    validate_type_name,
)
from arcteam.harness.enrollment import (
    EnrollmentDenied,
    OperatorKeyResolver,
    admit_registration,
    default_operator_key_resolver,
    guard_dispatch,
    is_eligible,
    member_admitted,
)
from arcteam.harness.protocol import (
    Degraded,
    HarnessAdapter,
    InboundEnvelope,
    MemberOutput,
    MemberStatus,
    MemoryPort,
)
from arcteam.harness.trust import (
    NATIVE_HARNESS,
    Placement,
    is_trusted,
    sandbox_placement,
)

__all__ = [
    "NATIVE_HARNESS",
    "AgentType",
    "Degraded",
    "EnrollmentDenied",
    "HarnessAdapter",
    "InboundEnvelope",
    "MemberOutput",
    "MemberStatus",
    "MemoryPort",
    "OperatorKeyResolver",
    "Placement",
    "admit_registration",
    "default_operator_key_resolver",
    "discover_agent_types",
    "find_agent_type",
    "guard_dispatch",
    "is_eligible",
    "is_trusted",
    "member_admitted",
    "sandbox_placement",
    "validate_type_name",
]
