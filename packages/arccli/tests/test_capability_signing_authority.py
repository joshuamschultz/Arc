"""Items 53: capabilities are signed by the OPERATOR signer, never the agent key."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import arcagent
import pytest
from arctrust import AgentIdentity

from arccli.commands import blueprint as bp_cmd
from arccli.commands.agent import create as create_cmd
from arccli.commands.operator import load_operator_key


@pytest.fixture
def arc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arc = tmp_path / "arc-config"
    arc.mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arc-data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return arc


def _sidecar(path: Path) -> dict[str, str]:
    return json.loads(Path(f"{path}.arcsig").read_text(encoding="utf-8"))


def test_create_scaffold_signs_with_operator_key(arc_dir: Path, tmp_path: Path) -> None:
    create_cmd._create(argparse.Namespace(name="aria", parent_dir=str(tmp_path), no_register=True))

    agent_dir = tmp_path / "aria"
    sidecar = _sidecar(agent_dir / "capabilities" / "calculator.py")
    operator_pub = load_operator_key(arc_dir).public_key.hex()
    assert sidecar["public_key"] == operator_pub
    config = arcagent.load_config(agent_dir / "arcagent.toml")
    agent_pub = AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=agent_dir / "arcagent.toml",
    ).public_key.hex()
    assert sidecar["public_key"] != agent_pub
    assert sidecar["signer_did"] != config.identity.did


def test_blueprint_sign_uses_signer_handle_not_seed(
    arc_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signer with no readable seed (vault custody) must still sign."""
    from arccli.commands import operator as operator_mod

    real = operator_mod.resolve_operator_signer(arc_dir)

    class _SeedlessSigner:
        public_key = real.public_key
        algorithm = real.algorithm

        def sign(self, data: bytes) -> bytes:
            return real.sign(data)

    monkeypatch.setattr(operator_mod, "resolve_operator_signer", lambda _d=None: _SeedlessSigner())
    target = tmp_path / "bp.toml"
    target.write_text('name = "x"\n', encoding="utf-8")
    bp_cmd._sign(argparse.Namespace(path=str(target), config_dir=str(arc_dir)))
    assert _sidecar(target)["public_key"] == real.public_key.hex()
