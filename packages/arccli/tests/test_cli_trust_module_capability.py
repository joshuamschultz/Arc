"""``arc trust`` over a MODULE capability (SPEC-066 T-972).

Josh's requirement in full: *"should be same as skills and tools so we know not
modified and signed, but need a way to re-sign on new changes, either by creator
or by user who modifies"*. The creator re-signs by rebuilding the bundle; the
person who modifies a module on the box re-signs with ``arc trust approve``.

That second half is what this file proves, through the real command:

1. a module arrives as a signed bundle and loads;
2. somebody edits its ``capabilities.py`` on the box, and it stops loading;
3. ``arc trust list`` shows it as gated — the module root has to be in the
   inventory, or the only surface that can unblock it never mentions it;
4. ``arc trust approve`` re-signs it and it loads again.

Nothing is stubbed. A real operator key under a tmp ``~/.arc``, a real signed
bundle, the real ``arcagent`` signing seam, and the real inventory seam for the
verdict — so a pass here is what an operator on a box gets.

Two things about the deployment root make this more than a repeat of
``test_cli_trust.py``: it is hardened to ``0444`` inside ``0555``, so re-signing
has to be able to write a sidecar into a read-only directory; and every module
ships a file called ``capabilities.py``, so the name an operator approves has to
be qualified by its module.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import arcagent
import arcbundle
import pytest
from arctrust import arc_home, generate_keypair
from arctrust.identity import AgentIdentity
from arctrust.operator import OperatorKey
from arctrust.validators import load_validators

from arccli.commands.trust import trust_handler

_ISSUER = "did:arc:module-issuer"
_MODULE = "probe"
_PIN_NAME = f"{_MODULE}/capabilities"

_CAPABILITIES = """\
import os

from arcagent.tools._decorator import tool


@tool(description="probe", version="1.0.0", classification="read_only")
async def probe_separator() -> str:
    return os.sep
"""

_RUNTIME = "def state() -> None:\n    return None\n"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every operator-key and data path inside ``tmp_path`` — never the real ``~/.arc``.

    ``ARC_CONFIG_DIR`` also moves the deployment MODULE root, which is what puts
    the module under test on a path this process may harden and then re-sign.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-config"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arc-data"))


def _team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    team_root = tmp_path / "team"
    team_root.mkdir()
    monkeypatch.chdir(tmp_path)
    return team_root


def _build_agent(team_root: Path, name: str, *, tier: str) -> Path:
    """A real agent directory with a real identity and the module enabled."""
    agent_dir = team_root / name
    (agent_dir / "workspace").mkdir(parents=True)
    key_dir = team_root / f"{name}-keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        f'[security]\ntier = "{tier}"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n'
        f"[modules.{_MODULE}]\nenabled = true\n",
        encoding="utf-8",
    )
    return agent_dir


def _install_module(tmp_path: Path, agent_dir: Path) -> Path:
    """Install a signed module the way ``arc module install`` does; return its dir.

    Built, verified, materialized, and then trusted — the last step being the
    per-agent config write that pins the issuer's key and approves the bundled
    bytes. Skipping it would leave the module gated for a reason unrelated to
    what this file is testing.
    """
    source = tmp_path / "src" / _MODULE
    source.mkdir(parents=True)
    (source / "capabilities.py").write_text(_CAPABILITIES, encoding="utf-8")
    (source / "_runtime.py").write_text(_RUNTIME, encoding="utf-8")

    keypair = generate_keypair()
    bundle = arcbundle.build_bundle(
        source,
        module=_MODULE,
        version="1.0.0",
        private_key=keypair.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / f"{_MODULE}.arcbundle",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: keypair.public_key}
    )
    installed = arcbundle.materialize(verified, arc_home() / "modules")
    arcagent.trust_bundled_capabilities(
        installed,
        config_path=agent_dir / "arcagent.toml",
        issuer_key=verified.issuer_key,
        issuer_did=_ISSUER,
    )
    return installed


def _edit_on_the_box(artifact: Path) -> None:
    """What a person with a shell does: ``chmod u+w``, then change the file."""
    artifact.chmod(0o644)
    artifact.write_text(_CAPABILITIES + "\nEDITED_ON_THE_BOX = True\n", encoding="utf-8")


def _operator_public_key(tmp_path: Path) -> bytes:
    return OperatorKey.load(tmp_path / "arc-config" / "operator" / "operator.key").public_key


async def _statuses(agent_dir: Path) -> dict[str, str]:
    """Every capability's verdict, keyed by the name ``arc trust`` shows."""
    items = await arcagent.list_gated(agent_dir, agent_id="olivia", include_loaded=True)
    return {item.name: item.status for item in items}


def test_a_bundled_module_capability_loads_without_any_operator_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the install alone is enough. A later denial is caused by the edit.

    Without this, every assertion below would also pass for a module that never
    loaded in the first place.
    """
    team_root = _team(tmp_path, monkeypatch)
    agent_dir = _build_agent(team_root, "olivia", tier="enterprise")
    _install_module(tmp_path, agent_dir)

    assert asyncio.run(_statuses(agent_dir))["probe_separator"] == "loaded"


def test_list_shows_an_edited_module_capability_as_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The module root is in the inventory, so the operator surface can see it.

    Named by module (``probe/capabilities``) rather than by bare file stem: every
    module ships a ``capabilities.py``, and an operator has to be able to approve
    one of them without meaning all of them.
    """
    team_root = _team(tmp_path, monkeypatch)
    agent_dir = _build_agent(team_root, "olivia", tier="enterprise")
    installed = _install_module(tmp_path, agent_dir)
    _edit_on_the_box(installed / "capabilities.py")

    trust_handler(["list"])

    out = capsys.readouterr().out
    assert _PIN_NAME in out, f"the edited module capability was not listed:\n{out}"
    # Edited bytes no longer match the sidecar, so above personal the signature
    # floor is what refuses them — reported as ``unsigned``, before TOFU is
    # consulted at all.
    assert "unsigned" in out


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_approve_re_signs_an_edited_module_capability_and_it_loads_again(
    tier: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The re-sign requirement, end to end through the command an operator runs.

    The deployment module root is ``0444`` inside ``0555``, so the sidecar write
    that ``approve`` performs has to borrow write permission and hand it back;
    a command that could not would fail here with a permission error rather than
    a verdict.

    Both keys must survive: the operator's new one is pinned, and the issuer's
    stays pinned, because re-signing one file must not gate every other module
    that issuer signed.
    """
    team_root = _team(tmp_path, monkeypatch)
    agent_dir = _build_agent(team_root, "olivia", tier=tier)
    installed = _install_module(tmp_path, agent_dir)
    config = agent_dir / "arcagent.toml"
    issuer_keys = set(load_validators(config).trusted_keys)
    artifact = installed / "capabilities.py"
    _edit_on_the_box(artifact)

    assert asyncio.run(_statuses(agent_dir)).get("probe_separator") != "loaded"

    trust_handler(["approve", _PIN_NAME])

    out = capsys.readouterr().out
    assert "loaded" in out, f"approve did not restore the capability:\n{out}"
    assert asyncio.run(_statuses(agent_dir))["probe_separator"] == "loaded"

    operator_key = _operator_public_key(tmp_path)
    assert arcagent.verify_file(
        artifact, artifact.read_bytes(), trusted_public_key=operator_key
    ), "the sidecar was not rewritten under the operator's key"
    validators = load_validators(config)
    assert operator_key.hex() in validators.trusted_keys
    assert issuer_keys <= set(validators.trusted_keys), "re-signing evicted the issuer's key"
    assert _PIN_NAME in {entry.name for entry in validators.approved}


def test_approve_leaves_the_module_root_read_only_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Borrowed write permission is handed back.

    The deployment root's modes are a real control against an accidental
    in-place edit. A re-sign that left the directory writable would quietly
    retire it for every module on the box.
    """
    team_root = _team(tmp_path, monkeypatch)
    agent_dir = _build_agent(team_root, "olivia", tier="enterprise")
    installed = _install_module(tmp_path, agent_dir)
    sidecar = arcagent.sidecar_path(installed / "capabilities.py")
    dir_mode = stat.S_IMODE(installed.stat().st_mode)
    sidecar_mode = stat.S_IMODE(sidecar.stat().st_mode)
    assert not dir_mode & stat.S_IWUSR, "the module root was not hardened; nothing to hand back"
    _edit_on_the_box(installed / "capabilities.py")

    trust_handler(["approve", _PIN_NAME])

    assert stat.S_IMODE(installed.stat().st_mode) == dir_mode
    assert stat.S_IMODE(sidecar.stat().st_mode) == sidecar_mode
