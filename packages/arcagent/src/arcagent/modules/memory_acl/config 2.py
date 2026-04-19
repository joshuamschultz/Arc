"""MemoryACLConfig — tier-driven defaults for cross-session visibility.

Federal tier default is the most restrictive (private) as required
by NIST 800-53 AC-3 least-privilege and CMMC access control practices.
Enterprise defaults to shared-within-agent-team; personal is relaxed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Type alias for the three visibility levels defined in SDD §3.3
CrossSessionVisibility = Literal[
    "private",
    "shared-with-agent",
    "shared-with-others-via-agent",
]


class MemoryACLConfig(BaseModel):
    """Configuration for the MemoryACL module.

    Tier defaults mirror SDD §3.3 Cross-Session Reads:
    - federal: private (most restrictive — blocks all cross-session reads)
    - enterprise: shared-with-agent within team
    - personal: shared-with-agent
    """

    tier: Literal["federal", "enterprise", "personal"] = "personal"

    # Default visibility applied when a session has no explicit ACL frontmatter
    federal_default: CrossSessionVisibility = "private"
    enterprise_default: CrossSessionVisibility = "shared-with-agent"
    personal_default: CrossSessionVisibility = "shared-with-agent"

    # When True, any veto also emits a telemetry audit event
    audit_on_veto: bool = True

    # When True, memory provider also re-checks capability (defense in depth)
    require_capability: bool = True

    def default_for_tier(self) -> CrossSessionVisibility:
        """Return the default visibility for the configured tier."""
        if self.tier == "federal":
            return self.federal_default
        if self.tier == "enterprise":
            return self.enterprise_default
        return self.personal_default

    model_config = {"frozen": True, "extra": "forbid"}


class TierDefaults(BaseModel):
    """Mapping from tier name to default cross-session visibility.

    Used to communicate tier defaults to external consumers without
    coupling them to the full MemoryACLConfig.
    """

    federal: CrossSessionVisibility = "private"
    enterprise: CrossSessionVisibility = Field(default="shared-with-agent")
    personal: CrossSessionVisibility = Field(default="shared-with-agent")

    model_config = {"frozen": True}
