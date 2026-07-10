"""SPEC-054 Phase 1 — SuiteConfig sub-block (REQ-112, COMP-003).

Suite-generation settings live in a ``[modules.skills.improver.suite]`` Pydantic
sub-block, sibling of ChangeBoundConfig/LifecycleConfig inside ImproverConfig, with
``extra="forbid"`` so misspelled TOML keys fail loudly instead of silently no-oping.
"""

from __future__ import annotations

import pytest
from arcskill.improver.config import ImproverConfig, SuiteConfig
from pydantic import ValidationError


class TestSuiteConfigDefaults:
    """All fields have defaults — zero-config experience, matching sibling blocks."""

    def test_default_autogen_enabled(self) -> None:
        cfg = SuiteConfig()
        assert cfg.autogen is True

    def test_default_min_cases(self) -> None:
        cfg = SuiteConfig()
        assert cfg.min_cases == 3

    def test_default_max_cases(self) -> None:
        cfg = SuiteConfig()
        assert cfg.max_cases == 10

    def test_default_generate_on_create(self) -> None:
        cfg = SuiteConfig()
        assert cfg.generate_on_create is True

    def test_default_extend_after_mutation(self) -> None:
        cfg = SuiteConfig()
        assert cfg.extend_after_mutation is True

    def test_default_candidate_budget(self) -> None:
        cfg = SuiteConfig()
        assert cfg.candidate_budget == 20

    def test_default_flake_runs(self) -> None:
        # COMP-001 pins N=5 sandbox runs in the adoption cascade.
        cfg = SuiteConfig()
        assert cfg.flake_runs == 5


class TestSuiteConfigValidation:
    """extra='forbid' + field bounds — typos and nonsense caught at parse time."""

    def test_misspelled_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SuiteConfig(autogenn=True)  # type: ignore[call-arg]

    def test_min_cases_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SuiteConfig(min_cases=0)

    def test_max_cases_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SuiteConfig(max_cases=0)

    def test_candidate_budget_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SuiteConfig(candidate_budget=0)

    def test_flake_runs_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SuiteConfig(flake_runs=0)

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SuiteConfig(min_cases="not_an_int")  # type: ignore[arg-type]


class TestSuiteConfigNesting:
    """SuiteConfig nests inside ImproverConfig as ``suite`` (TOML forwarding path)."""

    def test_improver_config_has_suite_default(self) -> None:
        cfg = ImproverConfig()
        assert isinstance(cfg.suite, SuiteConfig)
        assert cfg.suite.autogen is True

    def test_constructible_from_plain_dict(self) -> None:
        # arcagent forwards the [modules.skills.improver] TOML block verbatim as a
        # dict — the nested suite table must coerce through Pydantic.
        cfg = ImproverConfig(suite={"autogen": False, "min_cases": 5, "flake_runs": 7})
        assert cfg.suite.autogen is False
        assert cfg.suite.min_cases == 5
        assert cfg.suite.flake_runs == 7

    def test_misspelled_nested_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ImproverConfig(suite={"autogenn": True})
