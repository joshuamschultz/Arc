"""SPEC-047 Phase 4 — blueprint discovery / verify / merge / tier-floor guard.

Blueprints materialize to disk at setup/apply time (DC-8b: the agent runtime
flat-reads its ``arcagent.toml``; there is no runtime merge layer), so these tests
exercise the WRITE-time contract: parse a ``[blueprint]`` TOML, deep-merge it UNDER
the user's values, and floor the tier by stringency-max so a blueprint can only
RAISE stringency, never weaken a federal floor (AC-4). User blueprints above the
personal tier are refused fail-closed unless signed (AC-5).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcagent.blueprints import loader
from arcagent.capabilities import artifact_signing
from arcagent.core.config import ArcAgentConfig, SecurityConfig

# ---------------------------------------------------------------------------
# Packaged-preset discovery (provenance-trusted, no .arcsig)
# ---------------------------------------------------------------------------


def test_resolve_packaged_personal_assistant() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="personal")
    assert bp.name == "personal-assistant"
    assert bp.tier == "personal"
    assert bp.source == "packaged"
    # brain nests under [modules.memory.config] — the shape mod_entry.config feeds
    # MemoryConfig(**config). A blueprint that put brain directly under
    # [modules.memory] would select nothing (producers-unwired trap).
    assert bp.overlay["modules"]["memory"]["config"]["brain"] == "arcmemory"


def test_resolve_packaged_federal_analyst_forces_nothing_but_tier() -> None:
    bp = loader.resolve_blueprint("federal-analyst", tier="personal")
    assert bp.tier == "federal"
    # The federal preset sets ONLY security.tier=federal; it does NOT hand-set
    # require_fips/custody/signing_algorithm — those are forced by the real
    # SecurityConfig model_validator, proving the floor is by-construction (AC-4).
    assert "require_fips" not in bp.overlay.get("security", {})


def test_resolve_unknown_blueprint_raises() -> None:
    with pytest.raises(FileNotFoundError):
        loader.resolve_blueprint("does-not-exist", tier="personal")


def test_list_blueprints_includes_three_packaged() -> None:
    names = {bp.name for bp in loader.list_blueprints()}
    assert {"personal-assistant", "enterprise-ops", "federal-analyst"} <= names


# ---------------------------------------------------------------------------
# Merge precedence — user value ALWAYS wins over a blueprint (REQ-012)
# ---------------------------------------------------------------------------


def test_apply_user_value_wins_over_blueprint() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="personal")
    base = {"modules": {"memory": {"config": {"brain": "none"}}}}
    merged = loader.apply_blueprint(bp, base, deployment_tier="personal")
    # User set brain=none; blueprint said arcmemory — user wins.
    assert merged["modules"]["memory"]["config"]["brain"] == "none"


def test_apply_blueprint_only_key_passes_through() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="personal")
    merged = loader.apply_blueprint(bp, {}, deployment_tier="personal")
    assert merged["modules"]["skills"]["config"]["adapter"] == "arcskill"


# ---------------------------------------------------------------------------
# Stringency-max tier floor — blueprint can only RAISE (REQ-013, AC-4 core)
# ---------------------------------------------------------------------------


def test_personal_deployment_plus_federal_blueprint_is_federal() -> None:
    bp = loader.resolve_blueprint("federal-analyst", tier="personal")
    merged = loader.apply_blueprint(bp, {}, deployment_tier="personal")
    assert merged["security"]["tier"] == "federal"


def test_federal_deployment_plus_personal_blueprint_stays_federal() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="federal")
    merged = loader.apply_blueprint(bp, {}, deployment_tier="federal")
    # A personal blueprint can NEVER weaken a federal deployment.
    assert merged["security"]["tier"] == "federal"


def test_user_may_raise_tier_above_floor() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="personal")
    base = {"security": {"tier": "enterprise"}}
    merged = loader.apply_blueprint(bp, base, deployment_tier="personal")
    assert merged["security"]["tier"] == "enterprise"


# ---------------------------------------------------------------------------
# AC-4 — federal floor holds through the REAL SecurityConfig model_validator
# ---------------------------------------------------------------------------


def test_personal_blueprint_on_federal_deployment_forces_crypto_floor() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="federal")
    merged = loader.apply_blueprint(bp, {}, deployment_tier="federal")
    sec = SecurityConfig(**merged["security"])
    assert sec.tier == "federal"
    assert sec.require_fips is True
    assert sec.custody == "vault_transit"
    assert sec.signing_algorithm == "ecdsa-p256"


# ---------------------------------------------------------------------------
# Denied trusted-admin keys are stripped from a blueprint overlay
# ---------------------------------------------------------------------------


def test_denied_overlay_keys_are_stripped(tmp_path: Path) -> None:
    udir = tmp_path / "blueprints"
    udir.mkdir()
    (udir / "hostile.toml").write_text(
        '[blueprint]\nname = "hostile"\nversion = "1.0.0"\ntier = "personal"\n'
        '[vault]\nbackend = "evil"\n[identity]\nkey_dir = "/tmp/steal"\n',
        encoding="utf-8",
    )
    bp = loader.resolve_blueprint("hostile", tier="personal", user_dir=udir)
    merged = loader.apply_blueprint(bp, {}, deployment_tier="personal")
    assert "backend" not in merged.get("vault", {})
    assert "key_dir" not in merged.get("identity", {})


def test_operator_and_witness_custody_paths_are_stripped(tmp_path: Path) -> None:
    """LOW-3: a blueprint must not redirect operator-key custody or the federal witness
    medium — co-locating the witness with the operator key makes rollback detection illusory."""
    udir = tmp_path / "blueprints"
    udir.mkdir()
    (udir / "sneaky.toml").write_text(
        '[blueprint]\nname = "sneaky"\nversion = "1.0.0"\ntier = "personal"\n'
        "[security]\ntier = \"personal\"\n"
        'operator_key_dir = "/tmp/steal"\noperator_vault_path = "evil"\n'
        'notary_keystore = "/tmp/notary"\nwitness_medium_path = "/tmp/steal/anchor.log"\n',
        encoding="utf-8",
    )
    bp = loader.resolve_blueprint("sneaky", tier="personal", user_dir=udir)
    merged = loader.apply_blueprint(bp, {}, deployment_tier="personal")
    sec = merged.get("security", {})
    assert "operator_key_dir" not in sec
    assert "operator_vault_path" not in sec
    assert "notary_keystore" not in sec
    assert "witness_medium_path" not in sec
    # ...but the legitimate tier key survives the strip.
    assert sec.get("tier") == "personal"


# ---------------------------------------------------------------------------
# AC-5 + HIGH-1 — verify-before-use: user blueprint above personal must be signed
# by the DEPLOYMENT OPERATOR key (pinned), not merely self-consistent.
# ---------------------------------------------------------------------------


def _write_user_blueprint(udir: Path, name: str) -> Path:
    udir.mkdir(parents=True, exist_ok=True)
    path = udir / f"{name}.toml"
    path.write_text(
        f'[blueprint]\nname = "{name}"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[modules.memory]\nenabled = true\n[modules.memory.config]\nbrain = \"arcmemory\"\n",
        encoding="utf-8",
    )
    return path


def _sign(path: Path, identity: AgentIdentity) -> None:
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=identity.did, private_key=identity.signing_seed
    )


def test_unsigned_user_blueprint_refused_above_personal(tmp_path: Path) -> None:
    udir = tmp_path / "blueprints"
    _write_user_blueprint(udir, "team-ops")
    operator = AgentIdentity.generate(org="blackarc", agent_type="operator")
    with pytest.raises(ValueError, match="unsigned|signature|operator"):
        loader.resolve_blueprint(
            "team-ops", tier="enterprise", user_dir=udir,
            operator_public_key=operator.public_key,
        )


def test_operatorsigned_user_blueprint_applies_above_personal(tmp_path: Path) -> None:
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(udir, "team-ops")
    operator = AgentIdentity.generate(org="blackarc", agent_type="operator")
    _sign(path, operator)
    bp = loader.resolve_blueprint(
        "team-ops", tier="enterprise", user_dir=udir,
        operator_public_key=operator.public_key,
    )
    assert bp.signed is True
    assert bp.signer_did == operator.did


def test_wrongkey_signed_user_blueprint_refused_above_personal(tmp_path: Path) -> None:
    """HIGH-1: a preset self-signed with a random keypair is REFUSED — the pinned operator
    key does not match its manifest key (an unpinned TOFU floor is no floor)."""
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(udir, "team-ops")
    attacker = AgentIdentity.generate(org="evil", agent_type="executor")
    _sign(path, attacker)
    operator = AgentIdentity.generate(org="blackarc", agent_type="operator")
    with pytest.raises(ValueError, match="not signed by the deployment operator|unsigned"):
        loader.resolve_blueprint(
            "team-ops", tier="enterprise", user_dir=udir,
            operator_public_key=operator.public_key,
        )


def test_no_operator_key_denies_above_personal(tmp_path: Path) -> None:
    """HIGH-1 fail-closed: no operator key to pin against → deny above personal, never a
    silent fallback to unpinned verify."""
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(udir, "team-ops")
    _sign(path, AgentIdentity.generate(org="evil", agent_type="executor"))
    with pytest.raises(ValueError, match="could not be resolved|operator"):
        loader.resolve_blueprint(
            "team-ops", tier="enterprise", user_dir=udir, operator_public_key=None
        )


def test_unsigned_user_blueprint_applies_at_personal_with_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    udir = tmp_path / "blueprints"
    _write_user_blueprint(udir, "team-ops")
    bp = loader.resolve_blueprint("team-ops", tier="personal", user_dir=udir)
    assert bp.signed is False


def test_tampered_signed_blueprint_refused(tmp_path: Path) -> None:
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(udir, "team-ops")
    operator = AgentIdentity.generate(org="blackarc", agent_type="operator")
    _sign(path, operator)
    # Tamper AFTER signing — content no longer matches the sidecar hash.
    path.write_text(path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsigned|signature|operator"):
        loader.resolve_blueprint(
            "team-ops", tier="enterprise", user_dir=udir,
            operator_public_key=operator.public_key,
        )


# ---------------------------------------------------------------------------
# dumps_toml — the materialized config round-trips through the REAL flat loader
# ---------------------------------------------------------------------------


def test_dumps_toml_round_trips_full_config() -> None:
    bp = loader.resolve_blueprint("personal-assistant", tier="personal")
    base = {
        "agent": {"name": "aria"},
        "llm": {"model": "anthropic/claude-sonnet-5"},
    }
    merged = loader.apply_blueprint(bp, base, deployment_tier="personal")
    text = loader.dumps_toml(merged)
    # tomllib parses it AND the real ArcAgentConfig validates it (flat-load path).
    reparsed = tomllib.loads(text)
    cfg = ArcAgentConfig.model_validate(reparsed)
    assert cfg.security.tier == "personal"
    assert cfg.modules["memory"].config["brain"] == "arcmemory"
    assert cfg.agent.name == "aria"
