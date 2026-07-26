"""arccli.blueprints loader — discover / verify / merge, folder-layout + persona.

Blueprints are folders (``<dir>/<name>/blueprint.toml`` + optional ``persona.md``).
Built-ins live under the repo-root ``blueprints/``; user/shared presets under a
``user_dir`` or a direct ``--blueprint <path>`` folder. These exercise the WRITE-time
contract: resolve a preset, deep-merge UNDER the user's values, floor the tier by
stringency-max (a blueprint can only RAISE stringency), and carry a persona.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arccli import blueprints as bp

# ---------------------------------------------------------------------------
# Built-in discovery (repo-root blueprints/, provenance-trusted)
# ---------------------------------------------------------------------------


def test_resolve_packaged_personal_assistant() -> None:
    r = bp.resolve_blueprint("personal-assistant", tier="personal")
    assert r.name == "personal-assistant"
    assert r.source == "packaged"
    assert r.overlay["modules"]["memory"]["config"]["brain"] == "arcmemory"


def test_resolve_packaged_federal_forces_only_tier() -> None:
    r = bp.resolve_blueprint("federal-analyst", tier="personal")
    assert r.tier == "federal"
    assert "require_fips" not in r.overlay.get("security", {})


def test_resolve_unknown_raises() -> None:
    with pytest.raises(FileNotFoundError):
        bp.resolve_blueprint("does-not-exist", tier="personal")


def test_list_includes_the_four_builtins() -> None:
    names = {b.name for b in bp.list_blueprints()}
    assert {"coding", "personal-assistant", "enterprise-ops", "federal-analyst"} <= names


# ---------------------------------------------------------------------------
# Persona (from persona.md)
# ---------------------------------------------------------------------------


def test_coding_blueprint_carries_persona_from_md() -> None:
    r = bp.resolve_blueprint("coding", tier="personal")
    assert r.persona is not None
    assert "senior software engineer" in r.persona


def test_config_only_blueprint_has_no_persona() -> None:
    r = bp.resolve_blueprint("personal-assistant", tier="personal")
    assert r.persona is None


# ---------------------------------------------------------------------------
# Shareable folder — resolve by path
# ---------------------------------------------------------------------------


def test_resolve_by_path_reads_a_shared_folder(tmp_path: Path) -> None:
    shared = tmp_path / "shared-coding"
    shared.mkdir()
    (shared / "blueprint.toml").write_text(
        '[blueprint]\nname = "shared-coding"\nversion = "1.0.0"\ntier = "personal"\n'
        '[security]\ntier = "personal"\n',
        encoding="utf-8",
    )
    (shared / "persona.md").write_text("You are a shared coder.", encoding="utf-8")
    r = bp.resolve_blueprint(str(shared), tier="personal")
    assert r.name == "shared-coding"
    assert r.persona == "You are a shared coder."
    assert r.source == "user"  # a shared folder is untrusted provenance, not a built-in


def test_resolve_by_path_missing_toml_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        bp.resolve_blueprint(str(empty), tier="personal")


# ---------------------------------------------------------------------------
# Merge + tier floor
# ---------------------------------------------------------------------------


def test_apply_user_value_wins_over_blueprint() -> None:
    r = bp.resolve_blueprint("personal-assistant", tier="personal")
    base = {"agent": {"name": "mine"}, "modules": {"memory": {"config": {"brain": "custom"}}}}
    merged = bp.apply_blueprint(r, base, deployment_tier="personal")
    assert merged["agent"]["name"] == "mine"
    assert merged["modules"]["memory"]["config"]["brain"] == "custom"


def test_apply_stringency_max_raises_tier() -> None:
    r = bp.resolve_blueprint("federal-analyst", tier="personal")
    merged = bp.apply_blueprint(r, {}, deployment_tier="personal")
    assert merged["security"]["tier"] == "federal"


def test_personal_blueprint_cannot_weaken_federal_deployment() -> None:
    r = bp.resolve_blueprint("personal-assistant", tier="federal")
    merged = bp.apply_blueprint(r, {}, deployment_tier="federal")
    assert merged["security"]["tier"] == "federal"


# ---------------------------------------------------------------------------
# SEC-18: a blueprint must not self-grant the sandbox boundary
# ---------------------------------------------------------------------------


def _bp(overlay: dict) -> bp.ResolvedBlueprint:
    return bp.ResolvedBlueprint(
        name="x", version="1", tier="personal", overlay=overlay,
        source="user", signed=False, sha256="", signer_did="",
    )


def test_blueprint_cannot_self_grant_allowed_paths() -> None:
    # A shared/unsigned preset setting allowed_paths would widen the sandbox — it must be
    # stripped before merge (the escalation `working_dir` made reachable).
    merged = bp.apply_blueprint(
        _bp({"tools": {"policy": {"allowed_paths": ["/"]}}}), {}, deployment_tier="personal"
    )
    assert "allowed_paths" not in merged.get("tools", {}).get("policy", {})


def test_blueprint_may_set_operate_in_launch_dir() -> None:
    # The flag itself grants nothing (the working_dir gate still requires a trusted dir),
    # and the coding blueprint needs it — so it is NOT stripped.
    merged = bp.apply_blueprint(
        _bp({"tools": {"operate_in_launch_dir": True}}), {}, deployment_tier="personal"
    )
    assert merged["tools"]["operate_in_launch_dir"] is True
