"""Configuration for the arcskill.improver skill improver (SPEC-044).

Owned by ``arcskill.improver`` — provider-free, so it defines its own
``extra="forbid"`` base rather than inheriting arcagent's ``ModuleConfig``
(REQ-004). arcagent's thin ``[modules.skills]`` wiring forwards the
``[skills.improver]`` config block here on construction.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChangeBoundConfig(BaseModel):
    """Per-deployment change-bound overrides (SPEC-044 §7, SkillOpt-pinned).

    Every field is optional: ``None`` means "use the tier default" from
    :data:`~arcskill.improver.guardrails.TIER_BOUNDS`. A supplied value can only
    **tighten** a bound (the resolver takes ``min`` with the tier ceiling), so the
    federal floor is non-relaxable by construction (REQ-031). ``max_ast_distance`` is a
    convergence **regularizer, not a security gate** (§8 — the sandbox decides safety).
    """

    model_config = ConfigDict(extra="forbid")

    max_edits: int | None = Field(default=None, ge=1)  # SkillOpt Lt (edit ops/step)
    edit_schedule: str | None = None  # constant | linear | cosine
    min_edits_floor: int | None = Field(default=None, ge=1)
    max_files_touched: int | None = Field(default=None, ge=1)
    max_lines_changed: int | None = Field(default=None, ge=1)  # [DEEPEN-pinned §7]
    max_ast_distance: float | None = Field(default=None, ge=0.0, le=1.0)  # regularizer


class LifecycleConfig(BaseModel):
    """Curator usage-sweep + retire/revive settings (SPEC-044 REQ-043, Josh-locked).

    Every sweep setting is adjustable in ``config.toml`` (via ``[modules.skills.improver.
    lifecycle]``). Default inactivity window = **30 days** (Josh's call). Retire is
    reversible: disable + retain lineage, never a destructive delete (D-8).
    """

    model_config = ConfigDict(extra="forbid")

    inactivity_window_days: float = Field(default=30.0, gt=0.0)
    failure_floor: float = Field(default=0.5, ge=0.0, le=1.0)  # success-rate floor
    improve_attempts_before_retire: int = Field(default=3, ge=1)
    min_uses_before_retire: int = Field(default=5, ge=1)


class SuiteConfig(BaseModel):
    """Suite auto-generation settings (SPEC-054 REQ-112, COMP-003).

    Governs golden-suite bootstrap: whether suites are auto-generated, how many
    cases a suite carries, and the candidate/flake budgets for the adoption
    cascade (COMP-001 pins ``flake_runs=5``). All fields default so the
    zero-config experience matches the sibling blocks.
    """

    model_config = ConfigDict(extra="forbid")

    autogen: bool = True
    min_cases: int = Field(default=3, ge=1)
    max_cases: int = Field(default=10, ge=1)
    generate_on_create: bool = True
    extend_after_mutation: bool = True
    candidate_budget: int = Field(default=20, ge=1)
    flake_runs: int = Field(default=5, ge=1)


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
    # Persist scrubbed tool-call args in traces (SPEC-054 REQ-117, COMP-007).
    # Default OFF: hash-only. Federal stays hash-only regardless (TraceStore enforces).
    capture_args: bool = False

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

    # SkillOpt bounded-edit step (REQ-030/031). Tier defaults live in guardrails.TIER_BOUNDS;
    # these fields only tighten them (federal floor non-relaxable).
    change_bound: ChangeBoundConfig = Field(default_factory=ChangeBoundConfig)

    # Curator usage-sweep + retire/revive lifecycle (REQ-041..045).
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)

    # Golden-suite auto-generation (SPEC-054 REQ-112).
    suite: SuiteConfig = Field(default_factory=SuiteConfig)


__all__ = ["ChangeBoundConfig", "ImproverConfig", "LifecycleConfig", "SuiteConfig"]
