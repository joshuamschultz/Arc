"""`read_agent_tier` belongs to arctrust, the leaf that owns the tier literal.

It lived in ``arcagent.core.prompt_context``, which forced arcui's prompts route
to import arcagent directly — breaking the rule that arcui reaches arcagent only
through ``arcagent.capabilities.inventory``. arctrust is where ``_Tier`` and
``PolicyContext`` are defined and is the leaf foundation arcui may import
freely, so the value's home is next to the type it produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arctrust.policy import read_agent_tier


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "arcagent.toml").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_reads_each_known_tier(tmp_path: Path, tier: str) -> None:
    assert read_agent_tier(_write(tmp_path, f'[security]\ntier = "{tier}"\n')) == tier


class TestDegradesToLeastPrivilege:
    """A parse miss must never weaken a configured gate."""

    def test_missing_file(self, tmp_path: Path) -> None:
        assert read_agent_tier(tmp_path) == "personal"

    def test_missing_security_block(self, tmp_path: Path) -> None:
        assert read_agent_tier(_write(tmp_path, '[agent]\nname = "aria"\n')) == "personal"

    def test_malformed_toml(self, tmp_path: Path) -> None:
        assert read_agent_tier(_write(tmp_path, '[security]\ntier = "')) == "personal"

    def test_unknown_tier_label(self, tmp_path: Path) -> None:
        """An unrecognized label is not a licence to run unrestricted."""
        assert read_agent_tier(_write(tmp_path, '[security]\ntier = "godmode"\n')) == "personal"

    def test_non_string_tier(self, tmp_path: Path) -> None:
        assert read_agent_tier(_write(tmp_path, "[security]\ntier = 7\n")) == "personal"


def test_result_is_accepted_by_policy_context(tmp_path: Path) -> None:
    """The point of returning the literal: no cast at the call site."""
    from arctrust.policy import PolicyContext

    tier = read_agent_tier(_write(tmp_path, '[security]\ntier = "federal"\n'))
    assert PolicyContext(tier=tier, policy_version="", bundle_age_seconds=0.0).tier == "federal"
