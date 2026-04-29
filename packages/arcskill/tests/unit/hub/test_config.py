"""Tests for arcskill.hub.config — TOML config model roundtrip."""

from __future__ import annotations

import pytest
from arcskill.hub.config import (
    FindingsAllowed,
    HubConfig,
    HubPolicy,
    RevocationConfig,
    SkillSource,
    TierPolicy,
)

# ---------------------------------------------------------------------------
# TierPolicy
# ---------------------------------------------------------------------------


def test_tier_policy_defaults() -> None:
    tier = TierPolicy()
    assert tier.level == "personal"


def test_tier_policy_federal() -> None:
    tier = TierPolicy(level="federal")
    assert tier.level == "federal"


@pytest.mark.parametrize("level", ["federal", "enterprise", "personal"])
def test_tier_policy_all_levels(level: str) -> None:
    tier = TierPolicy(level=level)  # type: ignore[arg-type]
    assert tier.level == level


def test_tier_policy_invalid_raises() -> None:
    with pytest.raises(ValueError):
        TierPolicy(level="superadmin")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FindingsAllowed
# ---------------------------------------------------------------------------


def test_findings_allowed_defaults() -> None:
    f = FindingsAllowed()
    assert f.critical == 0
    assert f.high == 0
    assert f.medium == 2


def test_findings_allowed_custom() -> None:
    f = FindingsAllowed(critical=0, high=1, medium=5)
    assert f.high == 1


def test_findings_allowed_negative_rejected() -> None:
    with pytest.raises(ValueError):
        FindingsAllowed(critical=-1)


# ---------------------------------------------------------------------------
# HubPolicy
# ---------------------------------------------------------------------------


def test_hub_policy_defaults() -> None:
    p = HubPolicy()
    assert p.require_signature is True
    assert p.require_slsa_level == 3
    assert p.require_scan_pass is True
    assert p.install_path == "cli_only"


def test_hub_policy_slsa_level_range() -> None:
    p = HubPolicy(require_slsa_level=2)
    assert p.require_slsa_level == 2


def test_hub_policy_slsa_out_of_range() -> None:
    with pytest.raises(ValueError):
        HubPolicy(require_slsa_level=4)


# ---------------------------------------------------------------------------
# SkillSource
# ---------------------------------------------------------------------------


def test_skill_source_github() -> None:
    src = SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        trust="builtin",
        signer_identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
        signer_issuer="https://token.actions.githubusercontent.com",
    )
    assert src.name == "arc-official"
    assert src.type == "github"
    assert src.trust == "builtin"


def test_skill_source_registry() -> None:
    src = SkillSource(
        name="arc-trusted-partners",
        type="registry",
        url="https://skills.arcagent.dev/v1/index.json",
        trust="trusted",
        allowed_publishers=["anthropics", "openai"],
    )
    assert src.allowed_publishers == ["anthropics", "openai"]


def test_skill_source_local() -> None:
    src = SkillSource(name="local-dev", type="local", path="/tmp/skills")  # noqa: S108 — test fixture
    assert src.path == "/tmp/skills"  # noqa: S108 — test fixture


def test_skill_source_invalid_type() -> None:
    with pytest.raises(ValueError):
        SkillSource(name="bad", type="ftp")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RevocationConfig
# ---------------------------------------------------------------------------


def test_revocation_config_defaults() -> None:
    r = RevocationConfig()
    assert r.fail_closed_if_unreachable is True
    assert r.crl_refresh_interval_seconds == 3600


def test_revocation_config_interval_minimum() -> None:
    with pytest.raises(ValueError):
        RevocationConfig(crl_refresh_interval_seconds=30)  # below 60s minimum


# ---------------------------------------------------------------------------
# HubConfig
# ---------------------------------------------------------------------------


def test_hub_config_defaults_disabled() -> None:
    cfg = HubConfig()
    assert cfg.enabled is False


def test_hub_config_enabled() -> None:
    cfg = HubConfig(enabled=True)
    assert cfg.enabled is True


def test_hub_config_is_federal() -> None:
    cfg = HubConfig(tier=TierPolicy(level="federal"))
    assert cfg.is_federal is True


def test_hub_config_is_not_federal_personal() -> None:
    cfg = HubConfig(tier=TierPolicy(level="personal"))
    assert cfg.is_federal is False


def test_hub_config_source_by_name_found() -> None:
    src = SkillSource(name="arc-official", type="github", repo="arc-foundation/skills")
    cfg = HubConfig(enabled=True, sources=[src])
    found = cfg.source_by_name("arc-official")
    assert found is not None
    assert found.name == "arc-official"


def test_hub_config_source_by_name_not_found() -> None:
    cfg = HubConfig(enabled=True, sources=[])
    assert cfg.source_by_name("nonexistent") is None


def test_hub_config_full_roundtrip() -> None:
    """Full config roundtrip via model_dump / model_validate."""
    cfg = HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        policy=HubPolicy(require_slsa_level=3),
        sources=[
            SkillSource(
                name="arc-official",
                type="github",
                repo="arc-foundation/skills",
                trust="builtin",
            )
        ],
        revocation=RevocationConfig(crl_refresh_interval_seconds=1800),
    )
    dumped = cfg.model_dump()
    restored = HubConfig.model_validate(dumped)
    assert restored.enabled is True
    assert restored.tier.level == "federal"
    assert restored.policy.require_slsa_level == 3
    assert len(restored.sources) == 1
    assert restored.sources[0].name == "arc-official"
    assert restored.revocation.crl_refresh_interval_seconds == 1800
