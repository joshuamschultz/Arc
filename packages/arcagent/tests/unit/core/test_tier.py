"""Unit tests for arcagent.core.tier — Tier, PolicyContext."""

from __future__ import annotations

import pytest

from arcagent.core.tier import PolicyContext, Tier

# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------


class TestTierEnum:
    def test_federal_value(self) -> None:
        assert Tier.FEDERAL == "federal"

    def test_enterprise_value(self) -> None:
        assert Tier.ENTERPRISE == "enterprise"

    def test_personal_value(self) -> None:
        assert Tier.PERSONAL == "personal"

    def test_str_comparison(self) -> None:
        # StrEnum means direct comparison with string works
        assert Tier("federal") is Tier.FEDERAL

    def test_invalid_tier_raises(self) -> None:
        with pytest.raises(ValueError):
            Tier("invalid")


# ---------------------------------------------------------------------------
# PolicyContext
# ---------------------------------------------------------------------------


class TestPolicyContext:
    def test_is_federal_true(self) -> None:
        ctx = PolicyContext(tier=Tier.FEDERAL)
        assert ctx.is_federal is True
        assert ctx.is_enterprise is False
        assert ctx.is_personal is False

    def test_is_enterprise_true(self) -> None:
        ctx = PolicyContext(tier=Tier.ENTERPRISE)
        assert ctx.is_federal is False
        assert ctx.is_enterprise is True
        assert ctx.is_personal is False

    def test_is_personal_true(self) -> None:
        ctx = PolicyContext(tier=Tier.PERSONAL)
        assert ctx.is_federal is False
        assert ctx.is_enterprise is False
        assert ctx.is_personal is True

    def test_extras_default_empty(self) -> None:
        ctx = PolicyContext(tier=Tier.PERSONAL)
        assert ctx.extras == {}

    def test_extras_stored(self) -> None:
        ctx = PolicyContext(tier=Tier.FEDERAL, extras={"sandbox": "strict"})
        assert ctx.extras["sandbox"] == "strict"

    def test_frozen_immutable(self) -> None:
        ctx = PolicyContext(tier=Tier.PERSONAL)
        with pytest.raises((AttributeError, TypeError)):
            ctx.tier = Tier.FEDERAL  # type: ignore[misc]
