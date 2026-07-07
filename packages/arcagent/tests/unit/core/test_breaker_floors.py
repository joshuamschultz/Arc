"""SPEC-043 REQ-024 / AC-Sec5 — federal circuit-breaker floors are non-relaxable."""

from __future__ import annotations

import pytest

from arcagent.core.config import SecurityConfig


class TestFederalBreakerFloors:
    def test_federal_pins_unset_breakers_to_floors(self) -> None:
        sec = SecurityConfig(tier="federal")
        assert sec.runaway_max_repeat == 8
        assert sec.error_cascade_max == 5

    def test_federal_rejects_disabled_breaker(self) -> None:
        with pytest.raises(ValueError, match="runaway_max_repeat"):
            SecurityConfig(tier="federal", runaway_max_repeat=None)

    def test_federal_rejects_looser_cascade(self) -> None:
        with pytest.raises(ValueError, match="error_cascade_max"):
            SecurityConfig(tier="federal", error_cascade_max=999)

    def test_federal_accepts_tighter_value(self) -> None:
        sec = SecurityConfig(tier="federal", runaway_max_repeat=4, error_cascade_max=3)
        assert sec.runaway_max_repeat == 4
        assert sec.error_cascade_max == 3

    def test_personal_leaves_breakers_disabled(self) -> None:
        sec = SecurityConfig(tier="personal")
        assert sec.runaway_max_repeat is None
        assert sec.error_cascade_max is None
