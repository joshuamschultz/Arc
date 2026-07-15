"""``arc trust`` — CLI surface for gated-capability approval (SPEC-021).

Exercises the thin CLI: list gated capabilities via the arcagent inventory seam,
approve one (pin persists via arctrust + re-scan loads at enterprise), remove a
pin, and the agent-resolution errors. The operator identity is stubbed — the
real operator-key crypto is covered by ``test_cli_operator_key`` /
``test_approve_command``; here we assert CLI behaviour, not signing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.capabilities import artifact_signing
from arctrust.identity import AgentIdentity

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
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))
    # Stub the operator DID so the CLI never bootstraps the real ~/.arc key.
    monkeypatch.setattr(
        "arccli.commands.trust._operator_did", lambda: "did:arc:test:operator"
    )


def _build_agent(team_root: Path, name: str, *, tier: str, sign: bool) -> None:
    agent_dir = team_root / name
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    key_dir = team_root / f"{name}-keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)

    folder = skills / "reporter"
    folder.mkdir()
    skill_md = folder / "SKILL.md"
    content = _VALID_SKILL.format(name="reporter").encode("utf-8")
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


def _team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    team_root = tmp_path / "team"
    team_root.mkdir()
    monkeypatch.chdir(tmp_path)
    return team_root


def test_list_shows_gated_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)

    trust_handler(["list"])  # single agent -> no --agent needed

    out = capsys.readouterr().out
    assert "reporter" in out
    assert "new_sighting" in out


def test_approve_then_disapprove_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)

    trust_handler(["approve", "reporter"])
    approve_out = capsys.readouterr().out
    assert "Approved reporter" in approve_out
    assert "loaded" in approve_out
    assert "did:arc:test:operator" in approve_out

    # After approval the capability is no longer gated.
    trust_handler(["list"])
    assert "No gated capabilities." in capsys.readouterr().out

    trust_handler(["disapprove", "reporter"])
    assert "Removed approval for reporter" in capsys.readouterr().out


def test_approve_at_personal_prints_tier_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    team_root = _team(tmp_path, monkeypatch)
    _build_agent(team_root, "olivia", tier="personal", sign=False)

    trust_handler(["approve", "reporter"])
    out = capsys.readouterr().out
    assert "Note:" in out  # pins are not consulted at personal tier


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
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    _build_agent(team_root, "victor", tier="enterprise", sign=True)

    trust_handler(["approve", "reporter", "--agent", "victor"])
    assert "on victor" in capsys.readouterr().out
