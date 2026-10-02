"""``[security] skill_revision_anchor`` — tier floor for the skill revision authority."""

from __future__ import annotations

import pytest

from arcagent import SecurityConfig


@pytest.mark.parametrize("tier", ["personal", "enterprise"])
def test_personal_and_enterprise_default_to_the_local_file_journal(tier: str) -> None:
    assert SecurityConfig(tier=tier).skill_revision_anchor == "file"


def test_federal_floors_an_unset_anchor_to_vault() -> None:
    assert SecurityConfig(tier="federal").skill_revision_anchor == "vault"


@pytest.mark.parametrize("anchor", ["vault", "queue"])
def test_federal_keeps_an_external_anchor(anchor: str) -> None:
    assert SecurityConfig(tier="federal", skill_revision_anchor=anchor).skill_revision_anchor == (
        anchor
    )


def test_federal_refuses_an_explicit_local_file_anchor() -> None:
    with pytest.raises(ValueError, match="skill_revision_anchor"):
        SecurityConfig(tier="federal", skill_revision_anchor="file")


def test_unknown_anchor_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        SecurityConfig(skill_revision_anchor="sqlite")
