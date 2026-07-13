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

# Tools whose OUTPUT is durable knowledge regardless of length — the agent
# gathering (web/search/research/retrieval) or producing (writes/creation) real
# information. Kept even when short. Extend this via ``MemoryConfig`` when a new
# knowledge tool is added; ``read``/``grep``/``bash`` are deliberately absent —
# their worth is judged by result length, so a bare ``read -> ok`` still drops.
_DEFAULT_KEEP_TOOLS: frozenset[str] = frozenset(
    {
        "web_search",
        "research",
        "browser_read_page",
        "browser_navigate",
        "session_search",
        "write",
        "edit",
        "create_skill",
        "create_tool",
        "store_team_file",
        "synthesize",
        "transcribe",
    }
)


class MemoryConfig(BaseModel):
    """Immutable dynamics constants + budgets for one deployment tier."""

    model_config = ConfigDict(frozen=True)

    tier: Tier = "personal"

    # Hebbian write (w <- w + alpha*m*(1 - w/W))
    alpha: float = Field(default=0.3, description="write-rate; slower where poisoning risk higher")
    saturation: float = Field(default=1.0, description="W — normalized edge-weight ceiling")

    # Decay (w * e^(-lambda*dt)), salience-slowed (lambda_eff = lambda*(1 - beta*s))
    lambda_fast: float = Field(default=0.15, description="recent-context edge decay (per day)")
    beta: float = Field(default=0.6, description="salience damping on decay")
    forget_floor: float = Field(default=0.02, description="edge weight below this is forgotten")

    # Confidence (1 - e^(-gamma*hits))
    gamma: float = Field(default=0.536, description="confidence growth; 3 hits -> ~0.8")
    known_threshold: float = Field(default=0.8, description="confidence at/above which -> known")

    # Spreading activation (ACT-R fan effect)
    fan_strength: float = Field(default=1.6, description="S in S_ji = S - ln(fan)")
    max_hops: int = Field(default=3, description="spreading-activation hop cap")

    # Entity de-duplication — cosine at/above which two SAME-TYPE entity cards
    # are treated as the same real-world thing and merged (the slow-path hygiene
    # step, mirroring cue-merge). Conservative by default: only near-identical
    # names ("Austin, Texas" vs "Austin, TX") fold together, and only when the
    # entity_type matches, so a place is never merged into a person. Needs an
    # embedder — degrades to no-op when none is wired.
    entity_merge_threshold: float = Field(
        default=0.93, description="min cosine for same-type entity-card merge"
    )

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

    # Consolidation cadence — the slow "sleep" path runs at most this often (an
    # interval gate reads a persisted last-run stamp), never once per turn.
    consolidate_interval_minutes: float = Field(
        default=60.0, description="minimum minutes between consolidation runs"
    )

    # Distillation input budget — the max estimated tokens of raw events fed to a
    # single eval/distill call. A window over budget is split into sequential
    # chunks (assembled before writing), so a large window never overflows the
    # model context. ``None`` disables chunking (single call, model context is the
    # only limit). Default is a concrete cap so overflow is prevented by default;
    # raise it for larger-context models.
    distill_max_input_tokens: int | None = Field(
        default=100_000, description="max estimated tokens per distill call before chunking"
    )

    # Input curation — drop mechanical tool plumbing before distillation, KEEP
    # substantive content (user turns, agent conclusions, agent-gathered/created
    # knowledge). Pure/deterministic (reuses capture-time entity tags), zero extra
    # LLM/embedding. A tool event survives if it references an entity, is a
    # knowledge tool, carries a substantive-length result, or clears the salience
    # floor; only short mechanical frames (``tool:read -> ok``) are stripped.
    curate_input: bool = Field(
        default=True, description="drop mechanical tool plumbing before distillation"
    )
    curate_keep_tools: frozenset[str] = Field(
        default=_DEFAULT_KEEP_TOOLS,
        description="tool names whose output is always kept (knowledge-producing); extensible",
    )
    curate_min_substantive_chars: int = Field(
        default=200, description="a tool result at/above this length is kept as real content"
    )
    curate_tool_requires_entity: bool = Field(
        default=True, description="drop a tool event that clears none of the keep gates"
    )
    curate_tool_keep_salience: float = Field(
        default=0.0,
        description="keep a tool event at/above this salience; 0 disables the salience escape",
    )

    @classmethod
    def for_tier(cls, tier: Tier) -> MemoryConfig:
        """Return the R-9 constant set for ``tier`` (federal is strictest)."""
        if tier == "federal":
            return cls(
                tier="federal",
                alpha=0.15,
                beta=0.5,
                gamma=0.7,
                forget_floor=0.05,
                entity_merge_threshold=0.97,
            )
        if tier == "enterprise":
            return cls(tier="enterprise", alpha=0.2)
        return cls(tier="personal")


__all__ = ["MemoryConfig", "Tier"]
