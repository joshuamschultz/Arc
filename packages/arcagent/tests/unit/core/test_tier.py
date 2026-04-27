"""Unit tests for arcagent.core.tier — Tier, PolicyContext, tier_from_config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from arcagent.core.tier import PolicyContext, Tier, tier_from_config

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


# ---------------------------------------------------------------------------
# tier_from_config
# ---------------------------------------------------------------------------


@dataclass
class _Security:
    tier: str


@dataclass
class _Cfg:
    security: Any


class TestTierFromConfig:
    def test_returns_federal(self) -> None:
        cfg = _Cfg(security=_Security(tier="federal"))
        assert tier_from_config(cfg) is Tier.FEDERAL

    def test_returns_enterprise(self) -> None:
        cfg = _Cfg(security=_Security(tier="enterprise"))
        assert tier_from_config(cfg) is Tier.ENTERPRISE

    def test_returns_personal(self) -> None:
        cfg = _Cfg(security=_Security(tier="personal"))
        assert tier_from_config(cfg) is Tier.PERSONAL

    def test_case_insensitive(self) -> None:
        cfg = _Cfg(security=_Security(tier="FEDERAL"))
        assert tier_from_config(cfg) is Tier.FEDERAL

    def test_mixed_case(self) -> None:
        cfg = _Cfg(security=_Security(tier="Enterprise"))
        assert tier_from_config(cfg) is Tier.ENTERPRISE

    def test_missing_security_attr_falls_back_to_personal(self) -> None:
        # Object with no .security attribute
        class _NoCfg:
            pass

        assert tier_from_config(_NoCfg()) is Tier.PERSONAL

    def test_missing_tier_attr_falls_back_to_personal(self) -> None:
        # Object with .security but no .security.tier
        class _NoTier:
            pass

        class _CfgNoTier:
            security = _NoTier()

        assert tier_from_config(_CfgNoTier()) is Tier.PERSONAL

    def test_invalid_tier_string_raises_value_error(self) -> None:
        cfg = _Cfg(security=_Security(tier="superuser"))
        with pytest.raises(ValueError, match="Invalid security.tier"):
            tier_from_config(cfg)

    def test_invalid_tier_error_message_lists_valid_options(self) -> None:
        cfg = _Cfg(security=_Security(tier="xyz"))
        with pytest.raises(ValueError) as exc_info:
            tier_from_config(cfg)
        msg = str(exc_info.value)
        assert "federal" in msg
        assert "enterprise" in msg
        assert "personal" in msg
