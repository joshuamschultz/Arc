"""SPEC-047 Phase 3 — the config-relaxable tier surface (arcagent/tiers.py)."""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.tiers import (
    RELAXABLE_KNOBS,
    SECURITY_CONFIG_KNOBS,
    RelaxableKnob,
    resolve_tier_floor,
    stricter_tier,
    tier_rank,
)

_FIPS = next(k for k in RELAXABLE_KNOBS if k.name == "require_fips")
_CUSTODY = next(k for k in RELAXABLE_KNOBS if k.name == "custody")
_RUNAWAY = next(k for k in RELAXABLE_KNOBS if k.name == "runaway_max_repeat")


# --- tier stringency ordering ------------------------------------------------


def test_tier_rank_orders_federal_highest() -> None:
    assert tier_rank("federal") > tier_rank("enterprise") > tier_rank("personal")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("personal", "federal", "federal"),
        ("federal", "personal", "federal"),
        ("enterprise", "personal", "enterprise"),
        ("enterprise", "federal", "federal"),
    ],
)
def test_stricter_tier(a: str, b: str, expected: str) -> None:
    assert stricter_tier(a, b) == expected


# --- exact knobs: federal forces the floor -----------------------------------


def test_federal_forces_exact_floor_when_unset() -> None:
    assert resolve_tier_floor(_CUSTODY, "federal", "in_process", was_set=False) == "vault_transit"


def test_federal_rejects_explicit_weaker_exact() -> None:
    with pytest.raises(ValueError, match="vault_transit"):
        resolve_tier_floor(_CUSTODY, "federal", "in_process", was_set=True)


def test_federal_accepts_exact_floor_value() -> None:
    assert resolve_tier_floor(_CUSTODY, "federal", "vault_transit", was_set=True) == "vault_transit"


# --- smaller knobs: federal pins floor, rejects looser/disabled ---------------


def test_federal_pins_smaller_floor_when_unset() -> None:
    assert resolve_tier_floor(_RUNAWAY, "federal", None, was_set=False) == 8


def test_federal_rejects_disabled_smaller() -> None:
    with pytest.raises(ValueError, match="runaway_max_repeat"):
        resolve_tier_floor(_RUNAWAY, "federal", None, was_set=True)


def test_federal_rejects_looser_smaller() -> None:
    with pytest.raises(ValueError, match="runaway_max_repeat"):
        resolve_tier_floor(_RUNAWAY, "federal", 16, was_set=True)


def test_federal_honors_stricter_smaller() -> None:
    assert resolve_tier_floor(_RUNAWAY, "federal", 4, was_set=True) == 4


# --- relaxation at personal/enterprise + audit -------------------------------


def test_personal_relaxation_returns_requested_and_audits() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def audit(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    resolved = resolve_tier_floor(_FIPS, "personal", False, was_set=True, audit=audit)
    assert resolved is False
    assert events and events[0][0] == "tier.relaxation_granted"
    assert events[0][1]["knob"] == "require_fips"
    assert events[0][1]["tier"] == "personal"


def test_no_audit_when_value_matches_floor() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    resolve_tier_floor(
        _CUSTODY, "enterprise", "vault_transit", was_set=True, audit=lambda n, p: events.append((n, p))
    )
    assert events == []


def test_non_relaxable_knob_at_tier_raises() -> None:
    frozen = RelaxableKnob("x", federal_floor=True, relax_personal=False, relax_enterprise=False, stricter_is="exact")
    with pytest.raises(ValueError, match="x"):
        resolve_tier_floor(frozen, "personal", False, was_set=True)


def test_security_config_knobs_are_the_five_enforced() -> None:
    assert {k.name for k in SECURITY_CONFIG_KNOBS} == {
        "require_fips",
        "custody",
        "signing_algorithm",
        "runaway_max_repeat",
        "error_cascade_max",
    }
