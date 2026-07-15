"""Tier primitives — uniform deployment-tier enforcement across all modules.

Every module with tier-dependent behaviour must extract a ``policy.py`` that
imports ``Tier`` and ``PolicyContext`` from here.  Business logic calls into
the policy module; the core loop never branches on tier strings directly.

See docs/architecture/policy-modules.md for the full pattern.

Design rationale
----------------
Five different tier-enforcement idioms existed across six modules (SPEC-018).
``browser/policy.py`` was the gold standard.  This module gives every module
a shared, typed vocabulary so tier branches are always ``if policy.tier ==
Tier.FEDERAL`` rather than ``if tier == "federal"`` strings scattered through
business logic.

Usage example::

    from arcagent.core.tier import Tier

    def resolve(context: PolicyContext) -> str:
        if context.tier == Tier.FEDERAL:
            ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Tier(StrEnum):
    """Deployment tier — drives security policy across all modules.

    FEDERAL:    Highest-security environment (DOE, SCIF).  Deterministic-only
                paths, no LLM fallbacks, strict signature requirements.
    ENTERPRISE: Organisational deployment.  Enhanced security; optional
                signatures and audit features.
    PERSONAL:   Developer or personal use.  Permissive defaults; signatures
                optional or ignored.
    """

    FEDERAL = "federal"
    ENTERPRISE = "enterprise"
    PERSONAL = "personal"


@dataclass(frozen=True)
class PolicyContext:
    """Immutable policy context passed to all tier-sensitive module functions.

    Replaces ad-hoc ``federal: bool`` or ``tier: str`` parameters that varied
    per module.  Centralising tier here means a single, uniform signature
    across modules (SPEC-018 TASK-2).

    Attributes:
        tier:   The deployment tier driving security decisions.
        extras: Optional free-form metadata for module-specific policy
                extensions (e.g. ``{"sandbox": "strict"}``).  Kept out of
                the frozen dataclass via a dict so callers can pass arbitrary
                context without subclassing.
    """

    tier: Tier
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_federal(self) -> bool:
        """Convenience predicate — True when tier is FEDERAL."""
        return self.tier == Tier.FEDERAL

    @property
    def is_enterprise(self) -> bool:
        """Convenience predicate — True when tier is ENTERPRISE."""
        return self.tier == Tier.ENTERPRISE

    @property
    def is_personal(self) -> bool:
        """Convenience predicate — True when tier is PERSONAL."""
        return self.tier == Tier.PERSONAL


__all__ = [
    "PolicyContext",
    "Tier",
]
