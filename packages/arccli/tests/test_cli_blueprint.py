"""SPEC-047 Phase 5 — `arc blueprint` surface + the reusable apply-to-disk core.

Drives the REAL apply path (``apply_to_disk``) with a capturing audit callback so the
audit producers — ``tier.relaxation_granted`` (REQ-023) and ``blueprint.applied``
(REQ-015) — are proven on the production path, not a rigged fixture.

A blueprint is a folder: ``<dir>/<name>/blueprint.toml`` (+ optional persona.md), so
user presets here are written under a per-name subdirectory, not as a flat file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from arcagent.capabilities import artifact_signing
from arctrust import validators
from arctrust.identity import AgentIdentity

from arccli.commands import blueprint as bp_cmd


def _write_agent(tmp_path: Path, tier: str = "personal") -> Path:
    target = tmp_path / "arcagent.toml"
    target.write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n'
        f'[identity]\ndid = "did:arc:test:aria"\n[security]\ntier = "{tier}"\n',
        encoding="utf-8",
    )
    return target


def _write_user_blueprint(udir: Path, name: str, body: str) -> Path:
    """Write a user blueprint at ``<udir>/<name>/blueprint.toml`` and return that path."""
    bp_dir = udir / name
    bp_dir.mkdir(parents=True, exist_ok=True)
    path = bp_dir / "blueprint.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _operator_sign(arc_dir: Path, path: Path) -> bytes:
    """Bootstrap ``arc_dir``'s operator key and operator-sign ``path``. Returns its pubkey.

    Mirrors ``arc blueprint sign`` — the deployment operator's Ed25519 key, the same one
    ``operator_public_key(arc_dir)`` pins verification against (SPEC-047 HIGH-1).
    """
    from arccli.commands.operator import load_operator_key

    op = load_operator_key(arc_dir)
    artifact_signing.write_signature(
        path,
        path.read_bytes(),
        signer_did=f"operator:{op.public_key.hex()[:16]}",
        private_key=op.seed,
    )
    return op.public_key


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


def _packaged_blueprint_names() -> list[str]:
    """Every blueprint under the repo-root ``blueprints/`` — the shipped presets."""
    from arccli.blueprints import builtin_blueprints_dir

    return sorted(p.parent.name for p in builtin_blueprints_dir().glob("*/blueprint.toml"))


@pytest.mark.parametrize("name", _packaged_blueprint_names())
def test_every_packaged_blueprint_applies_over_a_tofu_approved_agent(
    tmp_path: Path, name: str
) -> None:
    """A live agent carries ``[[security.validators.approved]]`` from ``arc trust approve``.

    That array of tables is part of the base the apply re-serializes, so an emitter
    without array-of-tables support fails EVERY blueprint on any agent that ever
    approved a capability — and it must survive the round trip, not just not crash:
    a dropped pin silently re-gates the agent's own tools.
    """
    target = _write_agent(tmp_path)
    approvals = [
        {
            "name": "calculator",
            "hash": "sha256:d287bb7f",
            "approver": "operator",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ]
    validators.persist_validators(
        target,
        validators.ValidatorsConfig.model_validate(
            {"auto_run_agent_code": False, "approved": approvals}
        ),
    )

    _, merged = bp_cmd.apply_to_disk(
        name,
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda *_: None,
    )
    written = tomllib.loads(target.read_text(encoding="utf-8"))
    assert written == merged
    assert written["security"]["validators"]["approved"] == approvals


def test_apply_to_disk_emits_blueprint_applied(tmp_path: Path) -> None:
    target = _write_agent(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
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
    # runaway_max_repeat is a "smaller-is-stricter" knob (federal floor 8); a LARGER
    # value is the relaxation. 20 > 8 → weaker than the floor, permitted at enterprise.
    path = _write_user_blueprint(
        udir,
        "loose-ops",
        '[blueprint]\nname = "loose-ops"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[security]\nrunaway_max_repeat = 20\n",
    )
    _operator_sign(tmp_path, path)
    target = _write_agent(tmp_path, tier="enterprise")

    events: list[tuple[str, dict[str, Any]]] = []
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
    _write_user_blueprint(
        udir,
        "team",
        '[blueprint]\nname = "team"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[modules.memory]\nenabled = true\n",
    )
    # An operator key exists — so this genuinely exercises the unsigned-preset refusal
    # (verify against the pinned operator key fails), not the no-operator-key branch.
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
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


def test_apply_wrongkey_signed_blueprint_refused_above_personal(tmp_path: Path) -> None:
    """HIGH-1: an attacker self-signs a preset with a RANDOM keypair — the pinned operator
    key does not match it, so apply is refused at enterprise (an unpinned floor is no floor)."""
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(
        udir,
        "evil",
        '[blueprint]\nname = "evil"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[modules.memory]\nenabled = true\n",
    )
    # The deployment operator key exists...
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
    # ...but the attacker signs with their OWN random key, not the operator's.
    attacker = AgentIdentity.generate(org="evil", agent_type="executor")
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=attacker.did, private_key=attacker.signing_seed
    )
    target = _write_agent(tmp_path, tier="enterprise")
    with pytest.raises(ValueError, match=r"unsigned|not signed by the deployment operator"):
        bp_cmd.apply_to_disk(
            "evil",
            target=target,
            deployment_tier="enterprise",
            arc_dir=tmp_path,
            user_dir=udir,
            audit=lambda *_: None,
        )


def test_apply_operatorsigned_blueprint_accepted_above_personal(tmp_path: Path) -> None:
    """HIGH-1 companion: the SAME preset, signed by the operator key, IS accepted."""
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(
        udir,
        "team",
        '[blueprint]\nname = "team"\nversion = "1.0.0"\ntier = "enterprise"\n'
        '[modules.memory]\nenabled = true\n[modules.memory.config]\nbrain = "arcmemory"\n',
    )
    _operator_sign(tmp_path, path)
    target = _write_agent(tmp_path, tier="enterprise")
    _, merged = bp_cmd.apply_to_disk(
        "team",
        target=target,
        deployment_tier="enterprise",
        arc_dir=tmp_path,
        user_dir=udir,
        audit=lambda *_: None,
    )
    assert merged["modules"]["memory"]["config"]["brain"] == "arcmemory"


def test_no_operator_key_denies_above_personal(tmp_path: Path) -> None:
    """HIGH-1 fail-closed: with NO operator key to pin against, an above-personal apply is
    denied rather than falling back to an unpinned (any-signature-accepted) verify."""
    udir = tmp_path / "blueprints"
    path = _write_user_blueprint(
        udir,
        "team",
        '[blueprint]\nname = "team"\nversion = "1.0.0"\ntier = "enterprise"\n'
        "[modules.memory]\nenabled = true\n",
    )
    # Sign it (self-consistent) but provide NO operator key on disk → cannot pin → deny.
    attacker = AgentIdentity.generate(org="evil", agent_type="executor")
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=attacker.did, private_key=attacker.signing_seed
    )
    target = _write_agent(tmp_path, tier="enterprise")
    with pytest.raises(ValueError, match=r"could not be resolved|operator"):
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


def test_dry_run_emits_no_audit_record(tmp_path: Path) -> None:
    """MED-2: --dry-run writes NO file AND emits NO audit record; a real apply emits one."""
    target = _write_agent(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []

    bp_cmd.apply_to_disk(
        "personal-assistant",
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda name, details: events.append((name, details)),
        dry_run=True,
    )
    assert events == []  # a dry run leaves no trace on the WORM chain

    bp_cmd.apply_to_disk(
        "personal-assistant",
        target=target,
        deployment_tier="personal",
        arc_dir=tmp_path,
        audit=lambda name, details: events.append((name, details)),
    )
    assert [n for n, _ in events].count("blueprint.applied") == 1
