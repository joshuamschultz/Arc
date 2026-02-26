"""Tests for SkillImproverConfig — Pydantic validation and defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.skill_improver.config import SkillImproverConfig


class TestSkillImproverConfigDefaults:
    """All fields have sensible defaults — zero-config experience."""

    def test_default_min_traces(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.min_traces == 30

    def test_default_trace_buffer_turns(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.trace_buffer_turns == 50

    def test_default_trace_similarity_threshold(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.trace_similarity_threshold == 0.85

    def test_default_optimize_after_uses(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.optimize_after_uses == 50

    def test_default_max_iterations(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.max_iterations == 10

    def test_default_stagnation_limit(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.stagnation_limit == 5

    def test_default_min_delta(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.min_delta == 0.1

    def test_default_eval_dimensions(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.eval_dimensions == ["accuracy", "efficiency", "error_handling", "clarity"]

    def test_default_eval_scale(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.eval_scale == 5

    def test_default_max_token_ratio(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.max_token_ratio == 1.5

    def test_default_max_generations(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.max_generations == 10

    def test_default_anchor_distance_threshold(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.anchor_distance_threshold == 0.15

    def test_default_cooloff_turns(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.cooloff_turns == 200

    def test_default_exempt_tags(self) -> None:
        cfg = SkillImproverConfig()
        assert cfg.exempt_tags == ["security-critical", "compliance", "auth"]


class TestSkillImproverConfigOverrides:
    """Custom values are respected."""

    def test_custom_trace_settings(self) -> None:
        cfg = SkillImproverConfig(
            min_traces=50,
            trace_buffer_turns=100,
            optimize_after_uses=75,
        )
        assert cfg.min_traces == 50
        assert cfg.trace_buffer_turns == 100
        assert cfg.optimize_after_uses == 75

    def test_custom_engine_settings(self) -> None:
        cfg = SkillImproverConfig(
            max_iterations=20,
            stagnation_limit=3,
            min_delta=0.2,
        )
        assert cfg.max_iterations == 20
        assert cfg.stagnation_limit == 3
        assert cfg.min_delta == 0.2

    def test_custom_safety_settings(self) -> None:
        cfg = SkillImproverConfig(
            max_token_ratio=2.0,
            max_generations=5,
            anchor_distance_threshold=0.10,
            cooloff_turns=500,
            exempt_tags=["auth"],
        )
        assert cfg.max_token_ratio == 2.0
        assert cfg.max_generations == 5
        assert cfg.anchor_distance_threshold == 0.10
        assert cfg.cooloff_turns == 500
        assert cfg.exempt_tags == ["auth"]

    def test_custom_eval_dimensions(self) -> None:
        cfg = SkillImproverConfig(eval_dimensions=["accuracy", "clarity"])
        assert cfg.eval_dimensions == ["accuracy", "clarity"]


class TestSkillImproverConfigValidation:
    """Inherits extra=forbid from ModuleConfig — typos caught."""

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkillImproverConfig(unknown_field="oops")  # type: ignore[call-arg]

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkillImproverConfig(min_traces="not_an_int")  # type: ignore[arg-type]

    def test_wrong_type_for_float_field(self) -> None:
        with pytest.raises(ValidationError):
            SkillImproverConfig(max_token_ratio="bad")  # type: ignore[arg-type]  # noqa: S106 — not a password
