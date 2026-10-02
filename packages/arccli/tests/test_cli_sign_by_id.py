"""``arc workflow sign <id>`` signs the REGISTERED bundle (J3 F7/F13, G4).

Signing a path the operator typed could sign a copy the runner never reads: the
CLI said "signed" while the registered bundle stayed a draft. So the target is an
id (or a path that resolves to one) under the deployment's workflows directory,
and a signer handle does the signing, so a vault-held key works too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import Signer
from arctrust.paths import workflows_dir

from arccli.commands import workflow as wf_cmd
from arccli.commands.workflow import workflow_handler

_DEFINITION = """
[workflow]
id = "onboarding"
version = 1
owner = "@sales"

[[node]]
id = "greet"
kind = "agent"
agent = "@sales"
"""


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
    return tmp_path


def _bundle(root: Path, wid: str = "onboarding") -> Path:
    bundle = root / wid
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(_DEFINITION, encoding="utf-8")
    return bundle


class _HandleOnlySigner:
    """A signer with no seed accessor, like a vault or notary-held key."""

    def __init__(self, inner: Signer) -> None:
        self._inner = inner

    @property
    def public_key(self) -> bytes:
        return self._inner.public_key

    @property
    def algorithm(self) -> str:
        return self._inner.algorithm

    def sign(self, message: bytes) -> bytes:
        return self._inner.sign(message)


def test_sign_resolves_registered_bundle_and_rejects_outside_paths(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registered = _bundle(workflows_dir(arc_dir))
    elsewhere = _bundle(tmp_path / "scratch")

    workflow_handler(["sign", "onboarding", "--dir", str(arc_dir)])

    assert (registered / "workflow.toml.arcsig").is_file()
    assert "Signed" in capsys.readouterr().out

    with pytest.raises(SystemExit) as outside:
        workflow_handler(["sign", str(elsewhere), "--dir", str(arc_dir)])

    assert outside.value.code == 1
    assert not (elsewhere / "workflow.toml.arcsig").exists()
    assert "registered" in capsys.readouterr().err.lower()


def test_a_traversal_target_is_refused(arc_dir: Path, tmp_path: Path) -> None:
    outside = _bundle(tmp_path / "scratch")
    sneaky = workflows_dir(arc_dir) / ".." / ".." / "scratch" / "onboarding"
    sneaky.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["sign", str(sneaky), "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert not (outside / "workflow.toml.arcsig").exists()


def test_a_symlinked_bundle_pointing_outside_is_refused(arc_dir: Path, tmp_path: Path) -> None:
    outside = _bundle(tmp_path / "scratch")
    root = workflows_dir(arc_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "onboarding").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["sign", "onboarding", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert not (outside / "workflow.toml.arcsig").exists()


def test_an_unknown_id_says_so(arc_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["sign", "nope", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "workflow.toml not found" in capsys.readouterr().err


def test_a_vault_held_key_signs_through_the_signer_handle(
    arc_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No seed in this process, and no 'sign out-of-band' dead end (J3 F13)."""
    from arccli.commands.operator import load_operator_key

    seedless = _HandleOnlySigner(load_operator_key(arc_dir).into_signer())
    monkeypatch.setattr(wf_cmd, "resolve_operator_signer", lambda arc_dir=None: seedless)
    bundle = _bundle(workflows_dir(arc_dir))

    workflow_handler(["sign", "onboarding", "--dir", str(arc_dir)])

    assert (bundle / "workflow.toml.arcsig").is_file()
    workflow_handler(["verify", "onboarding", "--dir", str(arc_dir)])
    assert "VALID" in capsys.readouterr().out
