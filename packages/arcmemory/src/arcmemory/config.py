"""MemoryConfig — tiered dynamics constants and budgets.

The FERNme write/decay/confidence dynamics (SDD §4, Research R-9) are governed by
a handful of scalars whose *stringency* varies by tier: federal writes slower,
decays slower, and demands more corroboration before trusting a memory. Tier is
stringency metadata, not a gate — every tier still captures, decays, and gates.

Defaults are the R-9 recommended table; ``for_tier`` returns the personal /
enterprise / federal variants without any branching in the algorithms themselves
(they read the numbers off this frozen model).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["personal", "enterprise", "federal"]


class MemoryConfig(BaseModel):
    """Immutable dynamics constants + budgets for one deployment tier."""

    model_config = ConfigDict(frozen=True)

    tier: Tier = "personal"

    # Hebbian write (w <- w + alpha*m*(1 - w/W))
    alpha: float = Field(default=0.3, description="write-rate; slower where poisoning risk higher")
    saturation: float = Field(default=1.0, description="W — normalized edge-weight ceiling")

    # Decay (w * e^(-lambda*dt)), salience-slowed (lambda_eff = lambda*(1 - beta*s))
    lambda_fast: float = Field(default=0.15, description="recent-context edge decay (per day)")
    lambda_slow: float = Field(default=0.01, description="durable edge decay (per day)")
    beta: float = Field(default=0.6, description="salience damping on decay")
    forget_floor: float = Field(default=0.02, description="edge weight below this is forgotten")

    # Confidence (1 - e^(-gamma*hits))
    gamma: float = Field(default=0.536, description="confidence growth; 3 hits -> ~0.8")
    known_threshold: float = Field(default=0.8, description="confidence at/above which -> known")

    # Spreading activation (ACT-R)
    act_r_decay: float = Field(default=0.5, description="ACT-R base-level decay d")
    retrieval_threshold: float = Field(default=3.5, description="tau — activation to surface")
    fan_strength: float = Field(default=1.6, description="S in S_ji = S - ln(fan)")
    max_hops: int = Field(default=3, description="spreading-activation hop cap")

    # Structural / analogical retrieval (the centerpiece)
    struct_trigger_min: float = Field(
        default=0.25, description="min trigger-embedding cosine for the (a) channel"
    )
    struct_activation_min: float = Field(
        default=0.0, description="min cue-graph activation (>) for the (b) channel"
    )
    rerank_margin: float = Field(
        default=0.05, description="personal-tier rerank only when top1/top2 margin < this"
    )
    enrich_stream_radius: int = Field(
        default=1, description="raw-stream events kept either side of each instance"
    )

    # Capture
    dedup_window: int = Field(default=128, description="windowed dedup — recent hashes kept")
    max_event_chars: int = Field(default=2000, description="sanitize size cap per event")

    @classmethod
    def for_tier(cls, tier: Tier) -> MemoryConfig:
        """Return the R-9 constant set for ``tier`` (federal is strictest)."""
        if tier == "federal":
            return cls(
                tier="federal",
                alpha=0.15,
                lambda_slow=0.008,
                beta=0.5,
                gamma=0.7,
                retrieval_threshold=4.5,
                forget_floor=0.05,
            )
        if tier == "enterprise":
            return cls(tier="enterprise", alpha=0.2, retrieval_threshold=4.0)
        return cls(tier="personal")


__all__ = ["MemoryConfig", "Tier"]
