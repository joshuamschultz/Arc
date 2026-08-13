"""``arc trust`` — CLI surface for gated-capability approval (SPEC-021, SPEC-066).

``approve`` is the operator action that makes a gated capability LOAD: it signs
the artifact with the deployment operator key, pins that key as a trusted
capability-verification key, and pins the source hash. A hash pin alone never
reaches the signature floor, so all three land together or the command fails.

These cases drive the real thing end to end — a real operator key under a tmp
``~/.arc``, the real ``arcagent`` signing seam, and the real inventory seam for
the post-approval verdict. Nothing about the trust path is stubbed, so a verdict
here is the verdict a live agent gets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcagent.capabilities import artifact_signing
from arcstore.ingest import WORM_ACTIVE_FILENAME
from arctrust.audit import verify_chain
from arctrust.identity import AgentIdentity
from arctrust.keypair import generate_keypair
from arctrust.operator import OperatorKey
from arctrust.policy import OperatorApprovalAuthority
from arctrust.signer import ECDSA_P256, FileNotaryTransit, Signer, VaultSigner
from arctrust.validators import load_validators

from arccli.commands.registry import COMMAND_REGISTRY
from arccli.commands.render import gateway_help_lines, slack_subcommand_map, telegram_bot_commands
from arccli.commands.trust import trust_handler

_VALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 2.0.0\n"
    "description: does {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every operator-key AND data path at ``tmp_path`` — never the real ``~/.arc``.

    Two env vars are the WHOLE isolation, deliberately: every path the command
    resolves goes through ``arctrust.arc_home()`` or ``resolve_data_dir``, so no
    module constant needs monkeypatching. A surface that still needs one is a
    surface that would ignore ``ARC_CONFIG_DIR`` in a real isolated deployment —
    see ``test_operator_custody_follows_arc_config_dir_without_monkeypatching``.

    ``ARCSTORE_DATA_DIR`` matters as much as the key dir: approval appends to the
    deployment's WORM chain under the data dir, so without it a test run would
    write audit records into the developer's own chain.
    """
    arc_dir = tmp_path / "arc-config"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_dir))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arc-data"))


def _operator_public_key(tmp_path: Path) -> bytes:
    """The public half of the operator key the CLI auto-bootstrapped."""
    return OperatorKey.load(tmp_path / "arc-config" / "operator" / "operator.key").public_key


def _operator_key(tmp_path: Path) -> OperatorKey:
    return OperatorKey.load(tmp_path / "arc-config" / "operator" / "operator.key")


def _chain_path(tmp_path: Path) -> Path:
    """The operator-signed WORM chain the CLI appends its trust changes to."""
    return tmp_path / "arc-data" / "worm" / WORM_ACTIVE_FILENAME


def _chain_events(tmp_path: Path) -> list[dict[str, Any]]:
    """Every ``AuditEvent`` on that chain, read back off disk.

    Read from the file rather than from a captured sink: what proves the wiring
    is a record that survived a real ``WormSink.write``.
    """
    chain = _chain_path(tmp_path)
    if not chain.exists():
        return []
    return [
        json.loads(line)["event"]
        for line in chain.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _chain_event(tmp_path: Path, action: str) -> dict[str, Any]:
    matched = [event for event in _chain_events(tmp_path) if event["action"] == action]
    assert len(matched) == 1, (
        f"expected one {action}; chain has {[e['action'] for e in _chain_events(tmp_path)]}"
    )
    return matched[0]


def _use_vault_transit_custody(tmp_path: Path) -> Signer:
    """Put the machine on the real federal posture: ECDSA-P256 out-of-process.

    ``tier = "federal"`` is what selects ``custody = "vault_transit"`` and
    ``signing_algorithm = "ecdsa-p256"`` — writing them by hand would test a
    posture no deployment produces. The notary keystore is provisioned with the
    REAL :class:`FileNotaryTransit`, so the seed lives in the notary and the
    command signs by reference exactly as a federal box does.

    Returns the signer the command is expected to sign with.
    """
    arc_dir = tmp_path / "arc-config"
    arc_dir.mkdir(parents=True, exist_ok=True)
    keystore = arc_dir / "notary"
    seed = generate_keypair().private_key
    FileNotaryTransit.provision(keystore, "operator", seed, algorithm=ECDSA_P256)
    (arc_dir / "arcagent.toml").write_text(
        f'[security]\ntier = "federal"\nnotary_keystore = "{keystore}"\n', encoding="utf-8"
    )
    return VaultSigner(FileNotaryTransit(keystore, algorithm=ECDSA_P256), "operator", ECDSA_P256)


def _build_agent(
    team_root: Path, name: str, *, tier: str, sign: bool, skill_body: str | None = None
) -> AgentIdentity:
    agent_dir = team_root / name
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    key_dir = team_root / f"{name}-keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)

    folder = skills / "reporter"
    folder.mkdir()
    skill_md = folder / "SKILL.md"
    body = skill_body if skill_body is not None else _VALID_SKILL.format(name="reporter")
    content = body.encode("utf-8")
    skill_md.write_bytes(content)
    if sign:
        artifact_signing.write_signature(
            skill_md, content, signer_did=identity.did, private_key=identity.signing_seed
        )
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        f'[security]\ntier = "{tier}"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    return identity


def _team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    team_root = tmp_path / "team"
    team_root.mkdir()
    monkeypatch.chdir(tmp_path)
    return team_root


def _skill_md(team_root: Path, agent: str = "olivia") -> Path:
    return team_root / agent / "workspace" / "capabilities" / "skills" / "reporter" / "SKILL.md"


def _config(team_root: Path, agent: str = "olivia") -> Path:
    return team_root / agent / "arcagent.toml"


def test_list_shows_gated_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)

    trust_handler(["list"])  # single agent -> no --agent needed

    out = capsys.readouterr().out
    assert "reporter" in out
    assert "new_sighting" in out


def test_approve_signs_pins_key_and_pins_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One command, all three effects — and the capability actually loads."""
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    skill = _skill_md(team_root)
    config = _config(team_root)

    trust_handler(["approve", "reporter"])

    operator_key = _operator_public_key(tmp_path)
    assert artifact_signing.verify_file(skill, skill.read_bytes(), trusted_public_key=operator_key)
    validators = load_validators(config)
    assert operator_key.hex() in validators.trusted_keys
    # TOFU gates a skill under its FOLDER name.
    assert [entry.name for entry in validators.approved] == ["reporter"]
    assert "loaded" in capsys.readouterr().out


def test_approve_then_disapprove_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    skill = _skill_md(team_root)
    config = _config(team_root)

    trust_handler(["approve", "reporter"])
    assert "loaded" in capsys.readouterr().out
    trust_handler(["list"])
    assert "No gated capabilities." in capsys.readouterr().out

    trust_handler(["disapprove", "reporter"])

    assert "reporter" in capsys.readouterr().out
    assert not arcagent.sidecar_path(skill).exists()
    validators = load_validators(config)
    assert _operator_public_key(tmp_path).hex() not in validators.trusted_keys
    assert validators.approved == ()
    trust_handler(["list"])
    assert "reporter" in capsys.readouterr().out  # gated again


def test_disapprove_clears_a_pin_whose_artifact_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deleting a capability file never touches the agent's config.

    If disapprove refused whenever the artifact is missing, the hash pin — and
    the trusted verify key minted with it — would be stranded in that config
    with no command able to remove them: a trust anchor outliving the code it
    was approved for. The operator must always be able to withdraw trust.
    """
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    skill = _skill_md(team_root)
    config = _config(team_root)

    trust_handler(["approve", "reporter"])
    assert [entry.name for entry in load_validators(config).approved] == ["reporter"]
    capsys.readouterr()

    # The operator deletes the skill, then withdraws its approval.
    arcagent.sidecar_path(skill).unlink()
    skill.unlink()
    skill.parent.rmdir()

    trust_handler(["disapprove", "reporter"])

    assert load_validators(config).approved == ()
    assert "reporter" in capsys.readouterr().out


def test_approve_loads_at_personal_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Personal tier allows signed sources, so a signed capability loads there too."""
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="personal", sign=False)

    trust_handler(["approve", "reporter"])

    assert "loaded" in capsys.readouterr().out


def test_approve_reports_the_real_verdict_when_still_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Signing a broken capability must not be reported as a success."""
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(
        team_root, "olivia", tier="enterprise", sign=False, skill_body="not a skill at all\n"
    )

    trust_handler(["approve", "reporter"])

    out = capsys.readouterr().out
    assert "loaded" not in out
    assert "Still gated" in out


def test_approve_signs_through_vault_transit_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Signing must WORK on the tier this gate exists for (D-066-2).

    Under ``custody = "vault_transit"`` the seed never enters this process, so
    the command signs by reference through the notary. The capability still
    loads, and the pinned trust anchor is the notary's ECDSA-P256 verify key —
    not some in-process key the deployment deliberately moved to a vault.
    """
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    signer = _use_vault_transit_custody(tmp_path)
    skill = _skill_md(team_root)

    trust_handler(["approve", "reporter"])

    assert "loaded" in capsys.readouterr().out
    manifest = artifact_signing.load_signature(skill)
    assert manifest is not None
    assert manifest.algorithm == ECDSA_P256
    assert manifest.public_key == signer.public_key.hex()
    assert artifact_signing.verify_file(
        skill, skill.read_bytes(), trusted_public_key=signer.public_key
    )
    validators = load_validators(_config(team_root))
    assert signer.public_key.hex() in validators.trusted_keys
    assert [entry.name for entry in validators.approved] == ["reporter"]


def test_vault_transit_without_a_provisioned_notary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transit that cannot serve the operator key must never fall back.

    Falling back to whatever key is on disk would mint a trust anchor the
    deployment moved to a vault — so the command stops before touching anything.
    """
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    arc_dir = tmp_path / "arc-config"
    arc_dir.mkdir(parents=True, exist_ok=True)
    (arc_dir / "arcagent.toml").write_text(
        f'[security]\ntier = "federal"\nnotary_keystore = "{arc_dir / "absent"}"\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        trust_handler(["approve", "reporter"])

    assert exc.value.code == 1
    assert "vault_transit" in capsys.readouterr().err
    assert not arcagent.sidecar_path(_skill_md(team_root)).exists()
    validators = load_validators(_config(team_root))
    assert validators.trusted_keys == ()
    assert validators.approved == ()


def test_approver_is_the_operator_key_not_the_agent_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REQ-321 — only an operator-key holder can mint an approval.

    The signer is always the on-box operator key; there is no flag to supply an
    identity, so an agent-key holder cannot produce an operator approval.
    """
    team_root = _team(tmp_path, monkeypatch)
    identity = _build_agent(team_root, "olivia", tier="enterprise", sign=False)

    trust_handler(["approve", "reporter"])
    capsys.readouterr()

    manifest = artifact_signing.load_signature(_skill_md(team_root))
    assert manifest is not None
    assert manifest.public_key == _operator_public_key(tmp_path).hex()
    assert manifest.public_key != identity.public_key.hex()
    assert manifest.signer_did != identity.did


def test_trust_is_not_exposed_beyond_the_cli() -> None:
    """REQ-321 — approval never reaches a chat/gateway surface a model can drive."""
    trust = next(cmd for cmd in COMMAND_REGISTRY if cmd.name == "trust")
    assert trust.cli_only is True
    assert "trust" not in slack_subcommand_map()
    assert "trust" not in {entry["command"] for entry in telegram_bot_commands()}
    assert not [line for line in gateway_help_lines() if "trust" in line]


def test_capability_signing_is_not_reachable_as_an_agent_tool() -> None:
    """REQ-321 — the signing seam is wired to no tool and no module.

    Every ``@tool`` a model can call is loaded from a builtin capability or a
    module; if signing is reachable from either, an agent could approve its own
    code and the operator gate is decorative.
    """
    roots = [Path(arcagent.builtin_capabilities_path()), Path(arcagent.modules_path())]
    offenders = [
        str(path)
        for root in roots
        for path in root.rglob("*.py")
        if "capability_signing" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_unknown_capability_name_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo still fails loudly now that approve searches the FULL inventory.

    The message no longer says "gated" because approve is no longer gated-only —
    it reaches any discoverable capability — so the error names what was searched
    and how to list it.
    """
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)

    with pytest.raises(SystemExit) as exc:
        trust_handler(["approve", "nonesuch"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no capability named 'nonesuch'" in err
    assert "trust list --all" in err


def test_multiple_agents_without_flag_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    _build_agent(team_root, "victor", tier="enterprise", sign=True)

    with pytest.raises(SystemExit) as exc:
        trust_handler(["list"])
    assert exc.value.code == 1


def test_approve_and_disapprove_record_on_the_operator_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REQ-323 — both trust changes land on the REAL deployment chain.

    Nothing is patched: the records are read back off the signed hash chain the
    command opened, so an emission nothing receives fails here. The hash is
    asserted against the TOFU pin the same command wrote — an audit line that
    disagrees with the pin cannot answer which bytes the operator approved.
    """
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    skill = _skill_md(team_root)
    config = _config(team_root)

    trust_handler(["approve", "reporter"])
    capsys.readouterr()

    # Resolved after the command: `arc trust` bootstraps the operator key itself.
    operator_did = OperatorApprovalAuthority(_operator_key(tmp_path).into_signer()).did
    pinned_hash = load_validators(config).approved[0].hash
    signed = _chain_event(tmp_path, "capability.signed")
    assert signed["actor_did"] == operator_did
    assert signed["target"] == str(skill)
    assert signed["outcome"] == "signed"
    assert signed["payload_hash"] == pinned_hash

    trust_handler(["disapprove", "reporter"])
    capsys.readouterr()

    revoked = _chain_event(tmp_path, "capability.signature_revoked")
    assert revoked["actor_did"] == operator_did
    assert revoked["target"] == str(skill)
    assert revoked["outcome"] == "revoked"
    assert revoked["payload_hash"] == pinned_hash


def test_chain_records_are_signed_and_carry_no_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chain verifies under the operator key, and holds no secret.

    ``verify_chain`` is what distinguishes a real sink from a list that happened
    to collect dicts: it only passes if every record was Ed25519-signed and
    hash-linked on the way to disk.
    """
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)

    trust_handler(["approve", "reporter"])
    trust_handler(["disapprove", "reporter"])
    capsys.readouterr()

    key = _operator_key(tmp_path)
    assert verify_chain(_chain_path(tmp_path), key.public_key)
    raw = _chain_path(tmp_path).read_text(encoding="utf-8")
    assert key.seed.hex() not in raw
    assert key.public_key.hex() not in raw


def test_a_locked_audit_chain_never_blocks_a_trust_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AU-5 — auditing must not interrupt the action it audits.

    A ``WormSink`` holds an exclusive flock, so a running agent or dashboard can
    legitimately hold the chain. An operator must still be able to withdraw
    trust; the command says so on stderr rather than failing.
    """
    from arctrust import WormSink

    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    trust_handler(["approve", "reporter"])
    capsys.readouterr()

    _chain_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    holder = WormSink(_chain_path(tmp_path), _operator_key(tmp_path).into_signer())
    try:
        trust_handler(["disapprove", "reporter"])
    finally:
        holder.close()

    captured = capsys.readouterr()
    assert "audit chain unavailable" in captured.err
    assert not arcagent.sidecar_path(_skill_md(team_root)).exists()
    assert load_validators(_config(team_root)).approved == ()


def test_approve_resolves_named_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    _build_agent(team_root, "victor", tier="enterprise", sign=False)

    trust_handler(["approve", "reporter", "--agent", "victor"])

    assert "on victor" in capsys.readouterr().out
    assert not arcagent.sidecar_path(_skill_md(team_root, "olivia")).exists()


def test_operator_custody_follows_arc_config_dir_without_monkeypatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ARC_CONFIG_DIR`` alone must relocate the operator key AND machine config.

    ``arc trust`` used to read both from module constants frozen at import from a
    literal ``~/.arc``, so an isolated deployment signed with the invoking user's
    own operator key while arcui — which resolves through ``arctrust.arc_home()``
    — used the deployment's. Two surfaces, two different signers, same box.

    This case deliberately does NOT monkeypatch the module constants (the
    ``_hermetic`` fixture no longer needs to): the env var is the only isolation,
    which is exactly what a real isolated deployment has.
    """
    from arctrust import arc_home

    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)

    trust_handler(["approve", "reporter"])

    assert "loaded" in capsys.readouterr().out
    relocated_key = arc_home() / "operator" / "operator.key"
    assert relocated_key.exists(), "operator key was not bootstrapped under ARC_CONFIG_DIR"
    assert arc_home() == tmp_path / "arc-config"
    signed_with = OperatorKey.load(relocated_key).public_key.hex()
    assert signed_with in load_validators(_config(team_root)).trusted_keys
