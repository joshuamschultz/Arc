"""``capabilities.isolation_relax`` — tier-gated execution-backend relaxation.

Signing proves *who wrote* an agent-authored tool; it does not choose the
backend that *runs* it. Personal-tier operators may relax the container floor to
a bare host subprocess (sandbox off) so signed+approved capability-folder tools
run on a host without Docker; enterprise/federal may not. ``resolve_trust_posture``
is the single place that decision is resolved and fails closed.
"""

from __future__ import annotations

import pytest
from arctrust import ValidatorsConfig

from arcagent.capabilities.inventory import resolve_trust_posture
from arcagent.core.config import CapabilitiesConfig, SecurityConfig


def _posture(tier: str, relax: str | None):
    return resolve_trust_posture(
        SecurityConfig(tier=tier, validators=ValidatorsConfig()),
        CapabilitiesConfig(isolation_relax=relax),
        trusted_public_key=None,
    )


def test_personal_unset_keeps_the_container_floor() -> None:
    assert _posture("personal", None).isolation_relax is None


def test_personal_explicit_container_is_the_default_none() -> None:
    # "container" is the floor, expressed to the router as None (default routing).
    assert _posture("personal", "container").isolation_relax is None


@pytest.mark.parametrize("value", ["off", "local", "none"])
def test_personal_may_relax_sandbox_off(value: str) -> None:
    assert _posture("personal", value).isolation_relax == value


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
@pytest.mark.parametrize("value", ["off", "local", "none"])
def test_hardened_tiers_refuse_to_drop_below_container(tier: str, value: str) -> None:
    with pytest.raises(ValueError):
        _posture(tier, value)


def test_hardened_tiers_still_accept_the_container_floor() -> None:
    assert _posture("enterprise", "container").isolation_relax is None
    assert _posture("federal", None).isolation_relax is None


def test_unknown_relax_value_fails_closed() -> None:
    with pytest.raises(ValueError):
        _posture("personal", "docker-lite")
