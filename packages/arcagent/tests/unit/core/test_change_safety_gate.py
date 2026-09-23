"""Tests for ChangeSafetyGate (SPEC-085 COMP-006, REQ-464).

The gate re-validates the WHOLE ``ArcAgentConfig`` with a proposed single-field
change applied, catching cross-field breakage that the per-field schema check
in ``SignedSettingsWriter`` (COMP-004) cannot see. It is a pure dry-run: it
never writes a file and never mutates the ``config`` it is handed.

Pinned API (module does not exist yet — RED):

    from arcagent.core.change_safety_gate import check_change_safe

    result = check_change_safe(field="security.custody", value="in_process", config=cfg)
    result.safe    # bool
    result.reason  # str, empty when safe

The key "unsafe" case (cross-field, not caught by per-field schema alone):
``security.tier == "federal"`` with an explicit ``security.custody = "in_process"``.
Each field is independently valid per its own ``Field`` constraints — ``tier`` and
``custody`` are both plain unconstrained strings — but
``SecurityConfig._enforce_tier_crypto_floor`` (via ``arcagent.tiers.resolve_tier_floor``)
raises when federal tier sees an explicitly-set, weaker-than-floor ``custody``
(SC-5/SC-13/IA-7, fail-closed). A per-field schema check cannot see this because
it never re-runs the cross-field ``model_validator``.
"""

from __future__ import annotations

import copy
from pathlib import Path

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig, SecurityConfig


def _base_config(*, tier: str = "personal") -> ArcAgentConfig:
    """A minimal, valid ``ArcAgentConfig`` — mirrors test_config.py fixtures."""
    return ArcAgentConfig(
        agent=AgentConfig(name="test-agent"),
        llm=LLMConfig(model="anthropic/claude-sonnet-4-5-20250929"),
        security=SecurityConfig(tier=tier),
    )


class TestCheckChangeSafe:
    def test_valid_benign_change_is_safe(self) -> None:
        from arcagent.core.change_safety_gate import check_change_safe

        config = _base_config()

        result = check_change_safe(field="agent.type", value="worker", config=config)

        assert result.safe is True
        assert result.reason == ""

    def test_cross_field_breakage_is_unsafe(self) -> None:
        """The core test: a change that passes per-field schema but trips the
        federal tier/custody crypto-floor model_validator.

        ``security.custody`` accepts any string at the field level (no per-field
        constraint rejects "in_process"). Only the whole-config re-validation —
        which re-runs ``SecurityConfig._enforce_tier_crypto_floor`` — catches that
        this is disallowed once ``security.tier == "federal"``.
        """
        from arcagent.core.change_safety_gate import check_change_safe

        config = _base_config(tier="federal")

        result = check_change_safe(field="security.custody", value="in_process", config=config)

        assert result.safe is False
        assert result.reason != ""
        assert "custody" in result.reason

    def test_invalid_resulting_config_is_unsafe(self) -> None:
        """A change that fails whole-config Pydantic validation is unsafe."""
        from arcagent.core.change_safety_gate import check_change_safe

        config = _base_config()

        # arcrun.max_turns is declared `gt=0` — 0 fails the field constraint.
        result = check_change_safe(field="arcrun.max_turns", value=0, config=config)

        assert result.safe is False
        assert result.reason != ""

    def test_gate_is_side_effect_free(self, tmp_path: Path) -> None:
        """Calling the gate must not mutate the passed config or write any files."""
        from arcagent.core.change_safety_gate import check_change_safe

        config = _base_config(tier="federal")
        before = copy.deepcopy(config.model_dump(mode="python"))

        check_change_safe(field="security.custody", value="in_process", config=config)
        check_change_safe(field="agent.type", value="worker", config=config)

        after = config.model_dump(mode="python")
        assert after == before

        # The gate takes no path input and must not touch the filesystem.
        assert list(tmp_path.iterdir()) == []
