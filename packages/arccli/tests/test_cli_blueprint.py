"""SPEC-047 Phase 5 — `arc blueprint` surface + the reusable apply-to-disk core.

Drives the REAL apply path (``apply_to_disk``) with a capturing audit callback so the
audit producers — ``tier.relaxation_granted`` (REQ-023) and ``blueprint.applied``
(REQ-015) — are proven on the production path, not a rigged fixture.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arcagent.capabilities import artifact_signing
from arctrust.identity import AgentIdentity

from arccli.commands import blueprint as bp_cmd


def _write_agent(tmp_path: Path, tier: str = "personal") -> Path:
    target = tmp_path / "arcagent.toml"
    target.write_text(
        "[agent]\nname = \"aria\"\n[llm]\nmodel = \"x/y\"\n"
        f"[identity]\ndid = \"did:arc:test:aria\"\n[security]\ntier = \"{tier}\"\n",
        encoding="utf-8",
    )
    return target


def test_apply_to_disk_merges_under_existing_preserving_identity(tmp_path: Path) -> None:
    target = _write_agent(tmp_path)
    _, merged = bp_cmd.apply_to_disk(
        "personal-assistant",
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda *_: None,
    )
    written = tomllib.loads(target.read_text(encoding="utf-8"))
    # Blueprint added the brain; the agent's identity + name survive the merge.
    assert written["modules"]["memory"]["config"]["brain"] == "arcmemory"
    assert written["identity"]["did"] == "did:arc:test:aria"
    assert written["agent"]["name"] == "aria"
    assert merged["security"]["tier"] == "personal"


def test_apply_to_disk_emits_blueprint_applied(tmp_path: Path) -> None:
    target = _write_agent(tmp_path)
    events: list[tuple[str, dict]] = []
    bp_cmd.apply_to_disk(
        "personal-assistant",
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda name, details: events.append((name, details)),
    )
    applied = [d for n, d in events if n == "blueprint.applied"]
    assert len(applied) == 1
    assert applied[0]["name"] == "personal-assistant"
    assert applied[0]["effective_tier"] == "personal"
    assert "sha256" in applied[0]


def test_apply_stringency_max_raises_tier(tmp_path: Path) -> None:
    target = _write_agent(tmp_path, tier="personal")
    _, merged = bp_cmd.apply_to_disk(
        "federal-analyst",
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda *_: None,
    )
    assert merged["security"]["tier"] == "federal"


def test_apply_relaxation_audit_fires_at_enterprise(tmp_path: Path) -> None:
    """A signed enterprise blueprint that relaxes a breaker floor emits tier.relaxation_granted."""
    udir = tmp_path / "blueprints"
    udir.mkdir()
    path = udir / "loose-ops.toml"
    # runaway_max_repeat is a "smaller-is-stricter" knob (federal floor 8); a LARGER
    # value is the relaxation. 20 > 8 → weaker than the floor, permitted at enterprise.
    path.write_text(
        '[blueprint]\nname = "loose-ops"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[security]\nrunaway_max_repeat = 20\n",
        encoding="utf-8",
    )
    identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=identity.did, private_key=identity.signing_seed
    )
    target = _write_agent(tmp_path, tier="enterprise")

    events: list[tuple[str, dict]] = []
    bp_cmd.apply_to_disk(
        "loose-ops",
        target=target,
        deployment_tier="enterprise",
        arc_dir=tmp_path,
        user_dir=udir,
        audit=lambda name, details: events.append((name, details)),
    )
    relaxations = [d for n, d in events if n == "tier.relaxation_granted"]
    assert any(d["knob"] == "runaway_max_repeat" for d in relaxations)
    assert any(n == "blueprint.applied" for n, _ in events)


def test_apply_unsigned_user_blueprint_above_personal_errors(tmp_path: Path) -> None:
    udir = tmp_path / "blueprints"
    udir.mkdir()
    (udir / "team.toml").write_text(
        '[blueprint]\nname = "team"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[modules.memory]\nenabled = true\n",
        encoding="utf-8",
    )
    target = _write_agent(tmp_path, tier="enterprise")
    with pytest.raises(ValueError, match=r"unsigned|signature"):
        bp_cmd.apply_to_disk(
            "team",
            target=target,
            deployment_tier="enterprise",
            arc_dir=tmp_path,
            user_dir=udir,
            audit=lambda *_: None,
        )


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    target = _write_agent(tmp_path)
    before = target.read_text(encoding="utf-8")
    bp_cmd.apply_to_disk(
        "personal-assistant",
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda *_: None,
        dry_run=True,
    )
    assert target.read_text(encoding="utf-8") == before
