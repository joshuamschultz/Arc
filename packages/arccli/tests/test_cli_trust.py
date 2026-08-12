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

from pathlib import Path

import arcagent
import pytest
from arcagent.capabilities import artifact_signing
from arctrust.identity import AgentIdentity
from arctrust.operator import OperatorKey
from arctrust.validators import load_validators

from arccli.commands import operator as operator_cmd
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
    """Point every operator-key path at ``tmp_path`` — never the real ``~/.arc``."""
    arc_dir = tmp_path / "arc-config"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_dir))
    monkeypatch.setattr(operator_cmd, "DEFAULT_OPERATOR_DIR", arc_dir / "operator")
    monkeypatch.setattr(operator_cmd, "_MACHINE_CONFIG", arc_dir / "arcagent.toml")


def _operator_public_key(tmp_path: Path) -> bytes:
    """The public half of the operator key the CLI auto-bootstrapped."""
    return OperatorKey.load(tmp_path / "arc-config" / "operator" / "operator.key").public_key


def _use_vault_transit_custody(tmp_path: Path) -> None:
    """Configure the machine for out-of-process custody (the federal posture)."""
    arc_dir = tmp_path / "arc-config"
    arc_dir.mkdir(parents=True, exist_ok=True)
    (arc_dir / "arcagent.toml").write_text(
        '[security]\ncustody = "vault_transit"\n', encoding="utf-8"
    )


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


def test_vault_transit_custody_refuses_to_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seed never enters this process under transit custody — fail closed."""
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    _use_vault_transit_custody(tmp_path)

    with pytest.raises(SystemExit) as exc:
        trust_handler(["approve", "reporter"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "vault_transit" in err
    assert "custody" in err
    # Nothing was signed or pinned.
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
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)

    with pytest.raises(SystemExit) as exc:
        trust_handler(["approve", "nonesuch"])

    assert exc.value.code == 1
    assert "no gated capability named 'nonesuch'" in capsys.readouterr().err


def test_multiple_agents_without_flag_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    _build_agent(team_root, "victor", tier="enterprise", sign=True)

    with pytest.raises(SystemExit) as exc:
        trust_handler(["list"])
    assert exc.value.code == 1


def test_approve_resolves_named_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    _build_agent(team_root, "victor", tier="enterprise", sign=False)

    trust_handler(["approve", "reporter", "--agent", "victor"])

    assert "on victor" in capsys.readouterr().out
    assert not arcagent.sidecar_path(_skill_md(team_root, "olivia")).exists()
