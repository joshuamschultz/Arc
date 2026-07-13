"""MemoryConfig — tiered dynamics constants and budgets.

The memory write/decay/confidence dynamics (SDD §4, Research R-9) are governed by
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
# The session-conversation event kinds distillation learns from — the user's turns
# and the agent's responses. Everything else the agent emits (``tool`` frames and any
# other operational/runtime plumbing) is dropped before the LLM call (see curate.py).
_DEFAULT_CONVERSATION_KINDS: frozenset[str] = frozenset({"user", "respond"})


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

    # Entity de-duplication (write-time) — cosine at/above which a distiller-proposed
    # candidate name folds onto an existing SAME-TYPE card in search-before-write
    # (``resolve_entity``). Conservative so only near-identical names auto-fold.
    entity_merge_threshold: float = Field(
        default=0.93, description="min cosine for same-type write-time entity fold"
    )
    # Entity de-duplication (slow-path hygiene) — cosine at/above which two SAME-TYPE
    # cards are treated as POSSIBLE duplicates and clustered into a CANDIDATE group.
    # Intentionally WIDER (lower) than the write-time fold: candidates are not merged
    # on embedding alone — each cluster of >= 2 is sent to one bounded LLM call that
    # conservatively confirms which cards are the same real-world entity, and only the
    # confirmed sub-groups fold. A card with no same-type neighbor above this bar forms
    # no cluster, so no LLM call is made for it. Federal is stricter (higher) because a
    # false merge is worse there.
    entity_merge_candidate_threshold: float = Field(
        default=0.80, description="min cosine for a same-type candidate-duplicate cluster"
    )
    # Search-before-write disambiguation band: a same-type candidate whose name
    # cosine falls in ``[entity_disambiguate_min, entity_merge_threshold)`` is too
    # close to mint blindly yet too far to fold automatically — it is an "ambiguous
    # near match" worth one bounded LLM disambiguation call (only when a distiller is
    # wired). Below this floor the candidate is treated as genuinely new.
    entity_disambiguate_min: float = Field(
        default=0.60, description="min cosine for a same-type candidate to be LLM-disambiguated"
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

    # Consolidation engine — how the DISTILL step turns a window into durable
    # memory. ``agentic`` (default) runs a bounded ReAct loop over the memory
    # tools (search-before-write, merge, link); ``pipeline`` runs the
    # deterministic single-shot distiller. Agentic degrades to pipeline on
    # arcrun-absence, loop breach, or timeout (no data loss).
    consolidate_engine: Literal["agentic", "pipeline"] = Field(
        default="agentic", description="DISTILL engine; agentic degrades to pipeline"
    )
    consolidate_agent_max_turns: int = Field(
        default=16, description="max ReAct turns for one agentic consolidation (LLM10)"
    )
    consolidate_agent_max_tokens: int = Field(
        default=20_000, description="max tokens for one agentic consolidation (LLM10)"
    )
    consolidate_agent_timeout_seconds: float = Field(
        default=180.0, description="wall-clock cap for one agentic consolidation (LLM10)"
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

    # Input curation — feed the LLM distillation ONLY the session conversation (the
    # user's turns + the agent's responses), dropping tool frames and every other
    # operational kind. Pure/deterministic kind filter, zero extra LLM/embedding: the
    # model never sees — so cannot distill — the agent's own machinery.
    curate_input: bool = Field(
        default=True, description="feed distillation only the session conversation"
    )
    curate_conversation_kinds: frozenset[str] = Field(
        default=_DEFAULT_CONVERSATION_KINDS,
        description="event kinds distillation keeps (the conversation); all others are dropped",
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
                entity_merge_candidate_threshold=0.85,
                # Federal caps the agentic loop harder — a non-relaxable floor on
                # bounded consumption (LLM10) for the sleep-path sub-agent.
                consolidate_agent_max_turns=12,
                consolidate_agent_max_tokens=14_000,
                consolidate_agent_timeout_seconds=120.0,
            )
        if tier == "enterprise":
            return cls(tier="enterprise", alpha=0.2)
        return cls(tier="personal")


__all__ = ["MemoryConfig", "Tier"]
