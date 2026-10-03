"""``arcagent.scaffold.create_agent`` — the one agent builder every surface uses."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arctrust import ED25519, InProcessSigner, SignerError
from nacl.signing import SigningKey

from arcagent import scaffold


class _BrokenSigner:
    algorithm = ED25519
    public_key = bytes(32)

    def sign(self, content: bytes) -> bytes:
        raise SignerError("custody unreachable")


def _operator() -> scaffold.OperatorSigning:
    seed = bytes(SigningKey.generate())
    return scaffold.OperatorSigning(
        did="did:arc:operator:test/0001", signer=InProcessSigner(seed, ED25519)
    )


def _create(parent: Path, name: str = "helper", **kwargs: object) -> scaffold.CreatedAgent:
    options: dict[str, object] = {"operator": _operator()}
    options.update(kwargs)
    return scaffold.create_agent(parent, name, **options)  # type: ignore[arg-type]  # reason: test helper forwards typed kwargs


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)


def test_creates_the_three_files_a_did_and_signed_documents(tmp_path: Path) -> None:
    created = _create(tmp_path / "team", model="openai/gpt-4o")

    config = tomllib.loads((created.agent_dir / "arcagent.toml").read_text())
    assert config["identity"]["did"] == created.did
    assert tomllib.loads((created.agent_dir / "arcllm.toml").read_text())["llm"]["model"] == (
        "openai/gpt-4o"
    )
    assert (created.agent_dir / "arcrun.toml").is_file()
    assert set(created.signed) == {"identity.md", "calculator.py"}
    assert (created.agent_dir / "context" / "workspace" / "identity.md.arcsig").is_file()


@pytest.mark.parametrize("name", ["../up", "a/b", "..", "UP", "x", "a.b", "", "-lead"])
def test_unsafe_names_write_nothing(tmp_path: Path, name: str) -> None:
    with pytest.raises(scaffold.AgentNameError):
        _create(tmp_path / "team", name)
    assert not (tmp_path / "team").exists() or list((tmp_path / "team").iterdir()) == []


def test_a_model_that_could_inject_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="model"):
        _create(tmp_path / "team", model='x"\n[security]\ntier = "personal')
    assert not (tmp_path / "team" / "helper").exists()


def test_imported_documents_are_limited_to_persona_text(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be imported"):
        _create(tmp_path / "team", documents={"capabilities/x.py": "import os"})
    created = _create(tmp_path / "team", documents={"identity.md": "# Ada\n"})
    assert (created.agent_dir / "workspace" / "identity.md").read_text() == "# Ada\n"


def test_a_failed_signing_leaves_no_half_built_agent(tmp_path: Path) -> None:
    broken = scaffold.OperatorSigning(did="did:arc:operator:test/0001", signer=_BrokenSigner())  # type: ignore[arg-type]  # reason: a signer whose custody fails

    with pytest.raises(SignerError):
        _create(tmp_path / "team", operator=broken)

    assert not (tmp_path / "team" / "helper").exists()


def test_an_existing_agent_is_never_touched(tmp_path: Path) -> None:
    created = _create(tmp_path / "team")
    before = (created.agent_dir / "arcagent.toml").read_text()

    with pytest.raises(scaffold.AgentExistsError):
        _create(tmp_path / "team")

    assert (created.agent_dir / "arcagent.toml").read_text() == before
