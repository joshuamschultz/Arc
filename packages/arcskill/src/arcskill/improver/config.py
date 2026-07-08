"""Configuration for the arcskill.improver skill improver (SPEC-044).

Owned by ``arcskill.improver`` — provider-free, so it defines its own
``extra="forbid"`` base rather than inheriting arcagent's ``ModuleConfig``
(REQ-004). arcagent's thin ``[modules.skills]`` wiring forwards the
``[skills.improver]`` config block here on construction.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ImproverConfig(BaseModel):
    """Skill improver configuration.

    All fields have defaults so the improver works out-of-the-box.
    ``extra="forbid"`` catches misspelled config keys instead of silently
    ignoring them.
    """

    model_config = ConfigDict(extra="forbid")

    # Trace collection
    min_traces: int = Field(default=30, ge=1)
    trace_buffer_turns: int = Field(default=50, ge=0)
    trace_similarity_threshold: float = Field(default=0.85, gt=0.0, le=1.0)
    optimize_after_uses: int = Field(default=50, ge=1)

    # Optimization engine
    max_iterations: int = Field(default=10, ge=1, le=100)
    stagnation_limit: int = Field(default=5, ge=1, le=50)
    min_delta: float = Field(default=0.1, ge=0.0)
    failure_score_threshold: float = Field(default=3.0, ge=1.0, le=5.0)

    # Evaluation
    eval_dimensions: list[str] = Field(
        default=["accuracy", "efficiency", "error_handling", "clarity"],
    )
    eval_scale: int = Field(default=5, ge=2, le=10)

    # Golden-task gate (REQ-020/022). Minimum suite size to unlock code mutation (OQ-3).
    min_golden_cases: int = Field(default=3, ge=1)

    # Safety guardrails
    max_token_ratio: float = Field(default=1.5, gt=1.0, le=5.0)
    max_generations: int = Field(default=10, ge=1, le=100)
    anchor_distance_threshold: float = Field(default=0.15, gt=0.0, le=1.0)
    oscillation_distance_threshold: float = Field(default=0.05, gt=0.0, le=1.0)
    cooloff_turns: int = Field(default=200, ge=0)
    exempt_tags: list[str] = Field(
        default=["security-critical", "compliance", "auth"],
    )


__all__ = ["ImproverConfig"]
