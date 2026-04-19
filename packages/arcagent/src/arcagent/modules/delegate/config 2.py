"""Configuration for the delegate module.

Tier-driven depth caps and concurrency limits per SDD §3.5 / T3.6.5.

Federal default: depth cap = 2 (hardened; configurable but bounded).
Enterprise default: depth cap = 3.
Personal default: depth cap = 4.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Tier-mapped depth cap defaults
_DEPTH_CAPS: dict[str, int] = {
    "federal": 2,
    "enterprise": 3,
    "personal": 4,
}

_CONCURRENCY_CAPS: dict[str, int] = {
    "federal": 3,
    "enterprise": 5,
    "personal": 5,
}

# Tools that MUST be stripped from any child's tool list.
# These capabilities must not be delegatable to prevent:
# - Recursive delegation (delegate) — ASI-08 cascading failures
# - Memory writes across trust boundaries (memory) — ASI-06 poisoning
# - Platform messaging from untrusted children (send_message) — ASI-09
# - Code execution in child context without explicit parent grant (execute_code)
# - Clarification dialogs from child contexts (clarify) — UX confusion + ASI-09
DELEGATE_BLOCKED_TOOLS: frozenset[str] = frozenset(
    {
        "delegate",
        "memory",
        "send_message",
        "execute_code",
        "clarify",
    }
)


class DelegateConfig(BaseModel):
    """Configuration for the DelegateModule.

    Loaded from the arcagent module configuration section:
        [modules.delegate]
        enabled = true
        tier = "federal"
        max_depth = 2
        max_concurrent = 3
        default_max_turns = 25
        default_timeout_s = 300
    """

    enabled: bool = True
    tier: str = "personal"
    max_depth: int = Field(default=2, ge=1, le=10)
    max_concurrent: int = Field(default=3, ge=1, le=20)
    default_max_turns: int = Field(default=25, ge=1, le=200)
    default_timeout_s: int = Field(default=300, ge=10, le=3600)

    @classmethod
    def for_tier(cls, tier: str) -> DelegateConfig:
        """Build a DelegateConfig with tier-appropriate defaults.

        Args:
            tier: One of "federal", "enterprise", "personal".

        Returns:
            DelegateConfig with tier-appropriate depth and concurrency caps.
        """
        tier = tier.lower()
        depth = _DEPTH_CAPS.get(tier, _DEPTH_CAPS["personal"])
        concurrency = _CONCURRENCY_CAPS.get(tier, _CONCURRENCY_CAPS["personal"])
        return cls(
            tier=tier,
            max_depth=depth,
            max_concurrent=concurrency,
        )
